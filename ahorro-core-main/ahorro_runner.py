#!/usr/bin/env python3
"""ahorro_runner.py — Runner CLI unificado del software de ahorro.

Subcomandos:
  cache-stats             stats del cache semántico (estado opcional --state)
  budget-snapshot STATE   snapshot del presupuesto semanal (budget_weekly)
  pool-snapshot CUENTAS   snapshot del pool de cuentas (account_pool)
  submit-dry PROMPT...    dispatcher multi-cuenta con transporte DOBLE
                          (sin red, sin gasto) e imprime el receipt sin claves
  verify-balances         saldo DeepSeek: SOLO etiqueta + http + is_available
  battery                 unittest discover de ahorro-core-main y costo-token-real

REGLA DE SEGURIDAD: ninguna ruta de este runner imprime valores de claves.
Las claves solo se resuelven EN MEMORIA (secrets_loader) y viajan a la
cabecera HTTP o al transporte doble; jamás a stdout, logs ni argv.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import tempfile
from typing import Callable, Optional

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import secrets_loader  # noqa: E402
from account_pool import AccountPool  # noqa: E402
from budget_weekly import WeeklyBudget  # noqa: E402
from dispatcher_multicuenta import (DispatcherError,  # noqa: E402
                                    MultiAccountDispatcher)
from semantic_cache import SemanticCache, content_key  # noqa: E402

BALANCE_URL = "https://api.deepseek.com/user/balance"
DEFAULT_CACHE_STATE = HERE / "state" / "cache-state.json"
COSTO_TOKEN_DIR = HERE.parent / "costo-token-real"

# Clave simulada SOLO para el transporte doble cuando no hay vault: jamás se
# imprime y jamás sale del proceso (prefijo sk + guion + 32 chars = 35).
FAKE_DRY_KEY = "sk" + "-" + "d" * 32

# --- transportes ------------------------------------------------------------

class DryTransport:
    """Transporte DOBLE: sin red, sin gasto. Comprueba SOLO el formato de la
    clave en memoria y devuelve un usage simulado. Nunca imprime la clave."""

    def __init__(self) -> None:
        self.format_ok: list[bool] = []

    def post(self, endpoint: str, key, body, timeout_s):
        """POST doble: valida el FORMATO de la clave en memoria, sin red."""
        del endpoint, body, timeout_s
        ok_fmt = (isinstance(key, str) and key.startswith("sk" + "-")
                  and len(key) == 35)
        self.format_ok.append(ok_fmt)
        return {"ok": True, "statusCode": 200,
                "usage": {"prompt_tokens": 12, "completion_tokens": 7,
                          "prompt_cache_hit_tokens": 0},
                "content": "DRY-RESPONSE (transporte doble: sin red)",
                "model": "deepseek-v4-pro", "finishReason": "stop",
                "cost": 0.0001}


# --- utilidades -------------------------------------------------------------

def _print_json(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False, sort_keys=True))


def _emit(text: str) -> None:
    print(text, file=sys.stderr)


# --- cache-stats ------------------------------------------------------------

def cmd_cache_stats(args: argparse.Namespace) -> int:
    """cache-stats: stats JSON del cache semántico (fail-closed)."""
    cache = SemanticCache()
    state_path: Optional[pathlib.Path] = None
    if args.state:
        state_path = pathlib.Path(args.state)
        if not state_path.is_file():
            _emit(f"ERROR: estado de cache no encontrado: {state_path}")
            return 1
    elif DEFAULT_CACHE_STATE.is_file():
        state_path = DEFAULT_CACHE_STATE
    else:
        _emit(f"nota: sin estado persistente en {DEFAULT_CACHE_STATE}; "
              "stats de un cache vacío")
    if state_path is not None:
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("root no es objeto")
            entries = data.get("entries") or {}
            if not isinstance(entries, dict):
                raise ValueError("entries no es objeto")
            cache.hits = max(0, int(data.get("hits", 0)))
            cache.misses = max(0, int(data.get("misses", 0)))
            cache.evictions = max(0, int(data.get("evictions", 0)))
            for content, meta in entries.items():
                if not isinstance(meta, dict):
                    continue
                key = content_key(str(content))
                cache._store[key] = {
                    "response": None,
                    "tokens_saved": max(0, int(meta.get("tokens_saved", 0))),
                    "created": 0.0, "last_used": 0.0,
                    "reuses": max(0, int(meta.get("reuses", 0))),
                }
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            _emit(f"ERROR: estado de cache ilegible ({type(exc).__name__}); "
                  "fail-closed")
            return 1
    _print_json({"source": str(state_path) if state_path else None,
                 "stats": cache.stats()})
    return 0


# --- budget-snapshot --------------------------------------------------------

def cmd_budget_snapshot(args: argparse.Namespace) -> int:
    """budget-snapshot: snapshot JSON del presupuesto semanal."""
    state = pathlib.Path(args.state_json)
    if not state.is_file():
        _emit(f"ERROR: estado de presupuesto no encontrado: {state}")
        return 1
    try:
        json.loads(state.read_text(encoding="utf-8"))  # fail-closed estricto
    except (OSError, json.JSONDecodeError) as exc:
        _emit(f"ERROR: estado de presupuesto ilegible ({type(exc).__name__})")
        return 1
    budget = WeeklyBudget(state)
    _print_json(budget.snapshot())
    return 0


# --- pool-snapshot ----------------------------------------------------------

def cmd_pool_snapshot(args: argparse.Namespace) -> int:
    """pool-snapshot: snapshot JSON del pool de cuentas."""
    path = pathlib.Path(args.cuentas_json)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        _emit(f"ERROR: no se pudo leer {path}: {exc}")
        return 1
    except json.JSONDecodeError as exc:
        _emit(f"ERROR: JSON inválido en {path}: {exc}")
        return 1
    if isinstance(data, dict):
        data = data.get("accounts")
    if not isinstance(data, list):
        _emit("ERROR: el JSON debe ser una lista de cuentas "
              "{label,key_ref,budget_usd_diario,max_concurrent} "
              "(o un objeto con clave 'accounts')")
        return 1
    try:
        pool = AccountPool(data)
    except (ValueError, TypeError) as exc:
        _emit(f"ERROR: cuentas inválidas: {exc}")
        return 1
    _print_json(pool.snapshot())
    return 0


# --- submit-dry -------------------------------------------------------------

def cmd_submit_dry(args: argparse.Namespace) -> int:
    """submit-dry: dispatcher con transporte DOBLE, receipt sin claves."""
    prompt = " ".join(args.prompt)
    keys = secrets_loader.load_deepseek_keys(only_available=True)
    if keys:
        accounts = [{"label": lbl, "key_ref": lbl,
                     "budget_usd_diario": 5.0, "max_concurrent": 4}
                    for lbl in sorted(keys)]
        resolver: Callable[[str], Optional[str]] = lambda ref: keys.get(ref)
    else:
        # Sin vault: cuentas sintéticas y clave simulada (jamás impresa,
        # jamás enviada). El transporte doble sigue validando el formato.
        accounts = [{"label": f"dry-{i}", "key_ref": f"dry-ref-{i}",
                     "budget_usd_diario": 5.0, "max_concurrent": 4}
                    for i in range(1, 5)]
        resolver = lambda ref: FAKE_DRY_KEY
    pool = AccountPool(accounts, resolver=resolver)
    transport = DryTransport()
    with tempfile.TemporaryDirectory() as td:
        budget = WeeklyBudget(pathlib.Path(td) / "budget.json")
        budget.set_weekly("cli-dry", 1.0)
        dispatcher = MultiAccountDispatcher(pool, budget, resolver, transport)
        plan = {"max_tokens": 64, "rate_usd_per_1m": 0.66}
        try:
            receipt = dispatcher.submit(
                "cli-dry", [{"role": "user", "content": prompt}], plan)
        except DispatcherError as exc:
            _emit(f"ERROR: {exc.code}: {exc}")
            return 1
    out = {
        "transport": "double",
        "dry": True,
        "no_network": True,
        "vault_keys_used": len(keys),
        "key_format_ok": all(transport.format_ok),
        "receipt": {k: receipt.get(k) for k in
                    ("status", "opId", "unit", "account", "failoverUsed",
                     "usage", "cost", "postedAt")},
    }
    _print_json(out)
    return 0


# --- verify-balances --------------------------------------------------------

def cmd_verify_balances(args: argparse.Namespace) -> int:
    """verify-balances: solo etiqueta + http + is_available por clave."""
    del args
    keys = secrets_loader.load_deepseek_keys(only_available=False)
    if not keys:
        _emit("ERROR: sin claves DeepSeek en el vault; nada que verificar")
        return 1
    seen: set[str] = set()
    for label, key in sorted(keys.items()):
        if key in seen:
            continue
        seen.add(key)
        # Resuelto EN EL MOMENTO de la llamada: en tests se sustituye por un
        # doble con mock.patch.object(secrets_loader, "verify_deepseek").
        res = secrets_loader.verify_deepseek(key, label)
        # SOLO etiqueta + http + is_available; la clave jamás aparece aquí.
        _print_json({"label": label,
                     "http": res.get("http"),
                     "is_available": res.get("is_available")})
    return 0


# --- battery ----------------------------------------------------------------

def _suite_summary(stdout: str, stderr: str) -> str:
    # unittest escribe el resumen en stderr; si no hay nada, stdout.
    text = stderr if stderr.strip() else stdout
    lines = [l for l in text.splitlines()
             if l.strip() and not set(l.strip()) <= {"-"}]
    tail = lines[-3:] if len(lines) >= 3 else lines
    return " | ".join(tail)


def cmd_battery(args: argparse.Namespace) -> int:
    """battery: unittest discover de ahorro-core-main y costo-token-real."""
    del args
    suites = [("ahorro-core-main", HERE),
              ("costo-token-real", COSTO_TOKEN_DIR)]
    rc = 0
    for label, directory in suites:
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "discover",
             "-s", str(directory), "-t", str(directory), "-p", "test_*.py"],
            cwd=str(directory), capture_output=True, text=True)
        print(f"[{label}] exit={proc.returncode} "
              f"{_suite_summary(proc.stdout, proc.stderr)}")
        if proc.returncode != 0:
            rc = 1
    return rc


# --- main -------------------------------------------------------------------

COMMANDS = {
    "cache-stats": cmd_cache_stats,
    "budget-snapshot": cmd_budget_snapshot,
    "pool-snapshot": cmd_pool_snapshot,
    "submit-dry": cmd_submit_dry,
    "verify-balances": cmd_verify_balances,
    "battery": cmd_battery,
}


def build_parser() -> argparse.ArgumentParser:
    """ArgumentParser del CLI unificado de ahorro."""
    ap = argparse.ArgumentParser(
        prog="ahorro_runner",
        description="Runner CLI unificado del software de ahorro de DeepSeek.")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("cache-stats", help="stats del cache semántico")
    p.add_argument("--state", metavar="CACHE.json",
                   help=f"estado JSON del cache (por defecto "
                        f"{DEFAULT_CACHE_STATE})")

    p = sub.add_parser("budget-snapshot", help="snapshot del presupuesto "
                                               "semanal (fail-closed)")
    p.add_argument("state_json", metavar="state.json",
                   help="fichero de estado de WeeklyBudget")

    p = sub.add_parser("pool-snapshot", help="snapshot del pool de cuentas")
    p.add_argument("cuentas_json", metavar="cuentas.json",
                   help="lista JSON de cuentas {label,key_ref,"
                        "budget_usd_diario,max_concurrent}")

    p = sub.add_parser("submit-dry",
                       help="dispatcher multi-cuenta con transporte DOBLE "
                            "(sin red, sin gasto); imprime el receipt sin "
                            "claves")
    p.add_argument("prompt", nargs="+", metavar="PROMPT",
                   help="texto del prompt (se une con espacios)")

    sub.add_parser("verify-balances",
                   help="verifica cada clave DeepSeek contra "
                        f"{BALANCE_URL}; muestra SOLO etiqueta + http + "
                        "is_available (nunca la clave)")

    sub.add_parser("battery",
                   help="unittest discover de ahorro-core-main y "
                        "costo-token-real; devuelve su exit code")
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    """Punto de entrada del CLI: parsea y despacha el subcomando."""
    args = build_parser().parse_args(argv)
    handler = COMMANDS.get(args.command)
    if handler is None:
        _emit(f"ERROR: comando desconocido: {args.command}")
        return 2
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
