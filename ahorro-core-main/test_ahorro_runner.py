#!/usr/bin/env python3
"""test_ahorro_runner.py — Tests del CLI unificado con subprocess y dobles.

SIN RED REAL en tests:
  * los tests con subprocess apuntan HOME a un tmp (sin vault) y eliminan las
    variables de claves del entorno: submit-dry cae en el camino de cuentas
    sintéticas con clave simulada (jamás impresa, jamás enviada);
  * verify-balances se prueba EN PROCESO con dobles de
    secrets_loader.load_deepseek_keys / verify_deepseek (nunca toca la red);
  * NO se invoca `battery` desde aquí (descubriría este mismo fichero y
    recurriría); su exit code lo valida la orden de aceptación.

GUARD DE SEGURIDAD (100% offline, a prueba de regresiones):
  * EN PROCESO: setUpModule parchea urllib.request.urlopen con un stub que
    FALLA si se invoca (NETWORK_GUARD); cualquier test que olvide un doble
    revienta con AssertionError en vez de tocar la red.
  * EN SUBPROCESOS: run_cli inyecta un sitecustomize.py vía PYTHONPATH que
    aplica el mismo stub dentro del proceso hijo del CLI.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import ahorro_runner  # noqa: E402
import secrets_loader  # noqa: E402
from budget_weekly import WeeklyBudget  # noqa: E402

RUNNER = str(HERE / "ahorro_runner.py")


# --- guard de red: stub que FALLA si algo intenta tocar la red --------------

def _network_forbidden(*args, **kwargs):
    target = args[0] if args else "?"
    raise AssertionError(
        f"RED REAL BLOQUEADA EN TESTS: urllib.request.urlopen({target!r}) "
        "invocado; todo test debe ser offline")


# Mock global: registra la llamada y revienta (ver TestNetworkGuard).
NETWORK_GUARD = mock.Mock(side_effect=_network_forbidden)
_guard_patcher = None


def setUpModule():
    global _guard_patcher
    _guard_patcher = mock.patch("urllib.request.urlopen", new=NETWORK_GUARD)
    _guard_patcher.start()


def tearDownModule():
    global _guard_patcher
    if _guard_patcher is not None:
        _guard_patcher.stop()
        _guard_patcher = None


# sitecustomize inyectado en CADA subproceso del CLI: el hijo tampoco puede
# tocar la red, aunque un camino futuro del runner lo intentara.
SITECUSTOMIZE_GUARD = """\
import urllib.request


def _network_forbidden(*args, **kwargs):
    target = args[0] if args else "?"
    raise AssertionError(
        "RED REAL BLOQUEADA EN TESTS (subproceso): "
        "urllib.request.urlopen(%r) invocado" % (target,))


urllib.request.urlopen = _network_forbidden
"""


def run_cli(home: str, args: list[str]) -> subprocess.CompletedProcess:
    """CLI en subproceso hermético: HOME sin vault, sin claves en el entorno,
    y sitecustomize que bloquea urllib dentro del hijo."""
    os.makedirs(home, exist_ok=True)
    Path(home, "sitecustomize.py").write_text(SITECUSTOMIZE_GUARD,
                                              encoding="utf-8")
    env = dict(os.environ)
    env["HOME"] = home
    for var in ("DEEPSEEK_API_KEY", "DEEPSEEK_FALLBACK_API_KEY",
                "GEMINI_API_KEY", "GEMINI_2001_API_KEY"):
        env.pop(var, None)
    pythonpath = home + (os.pathsep + env["PYTHONPATH"]
                         if env.get("PYTHONPATH") else "")
    env["PYTHONPATH"] = pythonpath
    return subprocess.run(
        [sys.executable, RUNNER, *args], cwd=str(ROOT), env=env,
        capture_output=True, text=True, timeout=120)


class TestCliSubprocess(unittest.TestCase):

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name)
        self.home = str(self.tmp / "home")  # sin vault

    def test_help_lists_subcommands(self):
        proc = run_cli(self.home, ["--help"])
        self.assertEqual(proc.returncode, 0)
        for name in ("cache-stats", "budget-snapshot", "pool-snapshot",
                     "submit-dry", "verify-balances", "battery"):
            self.assertIn(name, proc.stdout)

    def test_budget_snapshot_shows_units(self):
        state = self.tmp / "state.json"
        budget = WeeklyBudget(state)
        budget.set_weekly("unit-a", 3.5)
        budget.try_reserve("unit-a", 100, 50, 1.0)
        proc = run_cli(self.home, ["budget-snapshot", str(state)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        snap = json.loads(proc.stdout)
        self.assertIn("week", snap)
        self.assertAlmostEqual(snap["units"]["unit-a"]["weekly_usd"], 3.5)
        self.assertIn("remaining", snap["units"]["unit-a"])

    def test_budget_snapshot_missing_file_fails_closed(self):
        proc = run_cli(self.home,
                       ["budget-snapshot", str(self.tmp / "no-existe.json")])
        self.assertEqual(proc.returncode, 1)
        self.assertIn("ERROR", proc.stderr)

    def test_pool_snapshot_shows_accounts_without_key_refs(self):
        cuentas = self.tmp / "cuentas.json"
        cuentas.write_text(json.dumps([
            {"label": "c1", "key_ref": "ref-1",
             "budget_usd_diario": 5.0, "max_concurrent": 2},
            {"label": "c2", "key_ref": "ref-2",
             "budget_usd_diario": 3.0, "max_concurrent": 1},
        ]), encoding="utf-8")
        proc = run_cli(self.home, ["pool-snapshot", str(cuentas)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        snap = json.loads(proc.stdout)
        self.assertEqual(set(snap["accounts"]), {"c1", "c2"})
        self.assertAlmostEqual(snap["total_remaining_usd"], 8.0)
        # NINGUNA referencia de clave puede aparecer en la salida.
        self.assertNotIn("key_ref", proc.stdout)
        self.assertNotIn("ref-1", proc.stdout)
        self.assertNotIn("ref-2", proc.stdout)

    def test_pool_snapshot_invalid_json_fails(self):
        bad = self.tmp / "bad.json"
        bad.write_text("{no es json", encoding="utf-8")
        proc = run_cli(self.home, ["pool-snapshot", str(bad)])
        self.assertEqual(proc.returncode, 1)
        self.assertIn("ERROR", proc.stderr)

    def test_cache_stats_from_state_file(self):
        state = self.tmp / "cache.json"
        state.write_text(json.dumps({
            "entries": {"pregunta a": {"tokens_saved": 5},
                        "pregunta b": {"tokens_saved": 7, "reuses": 2}},
            "hits": 3, "misses": 1, "evictions": 0,
        }), encoding="utf-8")
        proc = run_cli(self.home, ["cache-stats", "--state", str(state)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["stats"]["entries"], 2)
        self.assertEqual(out["stats"]["tokens_saved"], 12)
        self.assertEqual(out["stats"]["hits"], 3)
        self.assertEqual(out["stats"]["misses"], 1)

    def test_cache_stats_default_state_missing_is_empty(self):
        proc = run_cli(self.home, ["cache-stats"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["stats"]["entries"], 0)
        self.assertIsNone(out["source"])

    def test_submit_dry_double_transport_no_keys_printed(self):
        proc = run_cli(self.home, ["submit-dry", "hola", "mundo"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertTrue(out["dry"])
        self.assertTrue(out["no_network"])
        self.assertEqual(out["transport"], "double")
        self.assertEqual(out["vault_keys_used"], 0)  # hermético: sin vault
        self.assertTrue(out["key_format_ok"])
        self.assertEqual(out["receipt"]["status"], "ok")
        self.assertIn("usage", out["receipt"])
        self.assertIn("cost", out["receipt"])
        # Nada que parezca una clave (real o simulada) puede salir por stdout.
        self.assertNotIn("sk-", proc.stdout)
        self.assertNotIn("dry-ref", proc.stdout)
        self.assertNotIn("key_ref", proc.stdout)

    def test_submit_dry_exits_nonzero_on_dispatcher_error(self):
        # Prompts vacíos no cambian el flujo; con dobles del dispatcher en
        # proceso se cubre el error real. Aquí se verifica el camino CLI.
        proc = run_cli(self.home, ["submit-dry"])  # sin prompt => uso
        self.assertEqual(proc.returncode, 2)  # argparse: argumento requerido
        self.assertIn("prompt", proc.stderr.lower())


class TestVerifyBalancesInProcess(unittest.TestCase):
    """verify-balances con dobles inyectados: NUNCA toca la red."""

    def _run(self, keys: dict, verify_side_effect) -> tuple[int, str, str]:
        out, err = StringIO(), StringIO()
        with mock.patch.object(secrets_loader, "load_deepseek_keys",
                               return_value=keys), \
             mock.patch.object(secrets_loader, "verify_deepseek",
                               side_effect=verify_side_effect), \
             redirect_stdout(out), redirect_stderr(err):
            rc = ahorro_runner.main(["verify-balances"])
        return rc, out.getvalue(), err.getvalue()

    def test_prints_only_label_http_available(self):
        def fake_verify(key, label):
            # Doble: responde SIN tocar la red; incluye campos extra que el
            # runner debe filtrar (currency/has_funds jamás se imprimen).
            return {"label": label, "http": 200, "is_available": True,
                    "currency": "USD", "balance_entries": 1,
                    "has_funds": True}

        rc, out, err = self._run(
            {"alpha": "sk-VALOR-SECRETO-1", "beta": "sk-VALOR-SECRETO-2"},
            fake_verify)
        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        lines = [json.loads(l) for l in out.splitlines()]
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertEqual(set(line), {"label", "http", "is_available"})
        self.assertEqual(lines[0], {"label": "alpha", "http": 200,
                                    "is_available": True})
        # REGLA DE SEGURIDAD: ningún valor de clave en stdout ni stderr.
        self.assertNotIn("SECRETO", out)
        self.assertNotIn("sk-", out)
        self.assertNotIn("SECRETO", err)

    def test_deduplicates_equal_keys(self):
        rc, out, _ = self._run(
            {"k1": "sk-DUPLICADA", "k2": "sk-DUPLICADA"},
            lambda key, label: {"label": label, "http": 200,
                                "is_available": True})
        self.assertEqual(rc, 0)
        self.assertEqual(len(out.splitlines()), 1)  # una sola llamada

    def test_error_result_keeps_only_three_fields(self):
        rc, out, _ = self._run(
            {"k1": "sk-X"}, lambda key, label:
            {"label": label, "http": 401, "error": "Unauthorized"})
        self.assertEqual(rc, 0)
        line = json.loads(out)
        self.assertEqual(line, {"label": "k1", "http": 401,
                                "is_available": None})

    def test_no_keys_returns_nonzero(self):
        rc, out, err = self._run({}, lambda k, l: {})
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertIn("ERROR", err)


class TestNetworkGuard(unittest.TestCase):
    """Tripwire: si un test olvida el doble de verify_deepseek, el guard de
    red revienta la llamada ANTES de tocar api.deepseek.com."""

    def test_guard_blocks_real_urlopen(self):
        NETWORK_GUARD.reset_mock()
        out = StringIO()
        # Doble SOLO para las claves; verify_deepseek REAL se ejecutaría...
        with mock.patch.object(secrets_loader, "load_deepseek_keys",
                               return_value={"k1": "sk-X"}), \
             redirect_stdout(out):
            rc = ahorro_runner.main(["verify-balances"])
        # ...pero su urlopen cae en el stub: red bloqueada, jamás 401 real.
        self.assertTrue(NETWORK_GUARD.called,
                        "urllib.request.urlopen fue invocado sin bloqueo")
        self.assertEqual(rc, 0)  # verify_deepseek real captura el fallo
        line = json.loads(out.getvalue())
        self.assertEqual(line["http"], None)
        self.assertNotIn("sk-X", out.getvalue())  # la clave jamás se imprime


if __name__ == "__main__":
    unittest.main(verbosity=2)
