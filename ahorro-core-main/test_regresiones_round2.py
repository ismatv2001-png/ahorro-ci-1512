#!/usr/bin/env python3
"""test_regresiones_round2.py — Regresiones de los 14 bugs de la review
adversarial round2 (ahora con el comportamiento CORRECTO)."""

import datetime as dt
import json
import pathlib
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from account_pool import AccountPool  # noqa: E402
from budget_weekly import WeeklyBudget  # noqa: E402
from dispatcher_multicuenta import (DispatcherError,  # noqa: E402
                                    MultiAccountDispatcher)


class FakeTransport:
    def __init__(self):
        self.calls = 0
        self.script = []

    def post(self, endpoint, key, body, timeout_s):
        self.calls += 1
        if self.script:
            return self.script.pop(0)
        return {"ok": True, "statusCode": 200,
                "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                          "prompt_cache_hit_tokens": 0},
                "content": "ok", "model": "deepseek-v4-pro",
                "finishReason": "stop", "cost": 0.0001}


class BadTransport:
    def post(self, endpoint, key, body, timeout_s):
        raise ValueError("json roto")


def make_dispatcher(tmp, n=4, transport=None, cache=None):
    pool = AccountPool(
        [{"label": f"c{i}", "key_ref": f"ref-{i}",
          "budget_usd_diario": 5.0, "max_concurrent": 1} for i in range(n)],
        resolver=lambda ref: f"KEY:{ref}")
    budget = WeeklyBudget(Path(tmp) / "b.json")
    budget.set_weekly("unit-a", 10.0)
    return MultiAccountDispatcher(pool, budget, lambda ref: f"KEY:{ref}",
                                  transport or FakeTransport(), cache=cache)


PLAN = {"max_tokens": 50, "rate_usd_per_1m": 1.0}
MSGS = [{"role": "user", "content": "hola"}]


class TestRegresionesRound2(unittest.TestCase):

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)

    # B1: NaN/Inf en try_reserve jamás abre el presupuesto.
    def test_b1_nan_inf_rejected(self):
        b = WeeklyBudget(Path(self._td.name) / "b.json")
        b.set_weekly("u1", 1.0)
        for bad in (float("nan"), float("inf")):
            self.assertFalse(b.try_reserve("u1", bad, 10, 1.0)[0])
            self.assertFalse(b.try_reserve("u1", 10, bad, 1.0)[0])
            self.assertFalse(b.try_reserve("u1", 10, 10, bad)[0])

    # B2: dos instancias sobre el mismo estado: sin doble concesión.
    def test_b2_no_lost_update_between_instances(self):
        path = Path(self._td.name) / "b.json"
        b1 = WeeklyBudget(path)
        b1.set_weekly("u1", 1.0)
        ok, _ = b1.try_reserve("u1", 600_000, 0, 1.0)  # 0.60
        self.assertTrue(ok)
        b2 = WeeklyBudget(path)  # recarga el estado persistido
        ok2, reason = b2.try_reserve("u1", 800_000, 0, 1.0)  # 0.80 > 0.40
        self.assertFalse(ok2)
        self.assertEqual(reason, "budget-exceeded")

    # B4: settle tardío tras el rollover no se pierde.
    def test_b4_late_settle_survives_rollover(self):
        now = dt.datetime(2026, 9, 13, 12, 0, tzinfo=dt.timezone.utc)
        later = dt.datetime(2026, 9, 15, 0, 30, tzinfo=dt.timezone.utc)
        state = {"now": now}
        b = WeeklyBudget(Path(self._td.name) / "b.json",
                         clock=lambda: state["now"])
        b.set_weekly("u1", 1.0)
        b.try_reserve("u1", 100_000, 0, 1.0)
        state["now"] = later
        b.settle("u1", 0.05)  # tardío: semana anterior
        raw = json.loads(Path(self._td.name, "b.json").read_text())
        prev = raw.get("prev_units", {})
        self.assertTrue(any("u1" in w for w in prev.values()))
        spent_prev = [w["u1"]["spent"] for w in prev.values() if "u1" in w]
        self.assertAlmostEqual(spent_prev[0], 0.05, places=6)

    # D1: plan sin rate -> invalid-plan ANTES de red.
    def test_d1_plan_without_rate_rejected_pre_network(self):
        t = FakeTransport()
        d = make_dispatcher(self._td.name, transport=t)
        with self.assertRaises(DispatcherError) as cm:
            d.submit("unit-a", MSGS, {"max_tokens": 50})
        self.assertEqual(cm.exception.code, "invalid-plan")
        self.assertEqual(t.calls, 0)

    # D2: no-account libera la reserva REAL (sin fuga).
    def test_d2_refund_releases_reserve(self):
        pool = AccountPool([{"label": "c0", "key_ref": "r0",
                             "budget_usd_diario": 0.0, "max_concurrent": 1}],
                           resolver=lambda r: "KEY")
        budget = WeeklyBudget(Path(self._td.name) / "b.json")
        budget.set_weekly("unit-a", 10.0)
        d = MultiAccountDispatcher(pool, budget, lambda r: "KEY",
                                   FakeTransport())
        with self.assertRaises(DispatcherError) as cm:
            d.submit("unit-a", MSGS, PLAN)
        self.assertEqual(cm.exception.code, "no-account")
        snap = budget.snapshot()["units"]["unit-a"]
        self.assertAlmostEqual(snap["reserved"], 0.0, places=6)
        self.assertAlmostEqual(snap["remaining"], 10.0, places=6)

    # D3: transporte ValueError (no-OSError) no atasca el slot.
    def test_d3_valueerror_releases_slot_and_reserve(self):
        d = make_dispatcher(self._td.name, transport=BadTransport())
        with self.assertRaises(DispatcherError) as cm:
            d.submit("unit-a", MSGS, PLAN)
        self.assertEqual(cm.exception.code, "provider-rejected")
        snap = d.pool.snapshot()["accounts"]
        self.assertEqual(snap["c0"]["in_flight"], 0)
        self.assertEqual(snap["c0"]["calls"], 1)

    # D4: doble submit del mismo trabajo -> reusedReceipt, un solo POST.
    def test_d4_idempotency_single_post(self):
        t = FakeTransport()
        d = make_dispatcher(self._td.name, transport=t)
        r1 = d.submit("unit-a", MSGS, PLAN)
        r2 = d.submit("unit-a", MSGS, PLAN)
        self.assertEqual(r1["status"], "ok")
        self.assertTrue(r2.get("reusedReceipt"))
        self.assertEqual(r2["opId"], r1["opId"])
        self.assertEqual(t.calls, 1)

    # D5: 402 sin respaldo -> métricas sin doble release.
    def test_d5_single_release_on_402_no_backup(self):
        t = FakeTransport()
        t.script = [{"ok": False, "statusCode": 402, "cost": 0.0,
                     "usage": None}]
        d = make_dispatcher(self._td.name, n=1, transport=t)
        with self.assertRaises(DispatcherError):
            d.submit("unit-a", MSGS, PLAN)
        snap = d.pool.snapshot()["accounts"]["c0"]
        self.assertEqual(snap["calls"], 1)
        self.assertEqual(snap["errors"], 1)
        self.assertEqual(snap["in_flight"], 0)

    # D6: la clave de cache depende de max_tokens (sin hit cross-model).
    def test_d6_cache_key_includes_plan_params(self):
        from semantic_cache import SemanticCache
        t = FakeTransport()
        cache = SemanticCache(max_entries=10)
        d = make_dispatcher(self._td.name, transport=t, cache=cache)
        d.submit("unit-a", MSGS, {"max_tokens": 50, "rate_usd_per_1m": 1.0})
        r2 = d.submit("unit-a", MSGS, {"max_tokens": 8, "rate_usd_per_1m": 1.0})
        self.assertEqual(r2["status"], "ok")  # no cache-hit cross-params
        self.assertEqual(t.calls, 2)

    # A1: pick atómico entre hebras con max_concurrent=1.
    def test_a1_threads_get_distinct_accounts(self):
        pool = AccountPool(
            [{"label": f"c{i}", "key_ref": f"r{i}",
              "budget_usd_diario": 5.0, "max_concurrent": 1} for i in range(4)],
            resolver=lambda r: "KEY")
        picked: list[str] = []
        lock = threading.Lock()

        def worker():
            lbl = pool.pick()
            if lbl is not None:
                with lock:
                    picked.append(lbl)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(len(picked), len(set(picked)))  # sin duplicados

    # A2: cuenta sin clave resoluble no se elige.
    def test_a2_unresolvable_account_skipped(self):
        pool = AccountPool(
            [{"label": "sin-clave", "key_ref": "r0",
              "budget_usd_diario": 5.0, "max_concurrent": 1},
             {"label": "con-clave", "key_ref": "r1",
              "budget_usd_diario": 5.0, "max_concurrent": 1}],
            resolver=lambda ref: "KEY" if ref == "r1" else None)
        for _ in range(5):
            lbl = pool.pick()
            self.assertEqual(lbl, "con-clave")
            pool.release(lbl)  # liberar para el siguiente pick


if __name__ == "__main__":
    unittest.main(verbosity=2)
