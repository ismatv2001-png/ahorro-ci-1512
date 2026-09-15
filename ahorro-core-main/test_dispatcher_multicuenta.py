#!/usr/bin/env python3
"""test_dispatcher_multicuenta.py — Dispatcher multi-cuenta con dobles.

Sin red y sin saldo real: transporte doble inyectado, presupuesto en tmp,
claves resueltas con un resolver doble (NUNCA valores reales).
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from account_pool import AccountPool  # noqa: E402
from budget_weekly import WeeklyBudget  # noqa: E402
from dispatcher_multicuenta import (DispatcherError,  # noqa: E402
                                    MultiAccountDispatcher)


class FakeTransport:
    def __init__(self):
        self.calls = []  # (endpoint, key, body) — solo en memoria del test
        self.script = []  # respuestas programadas

    def post(self, endpoint, key, body, timeout_s):
        self.calls.append((endpoint, key, body))
        resp = self.script.pop(0) if self.script else {
            "ok": True, "statusCode": 200, "usage": {"prompt_tokens": 10,
                                                     "completion_tokens": 5},
            "content": "ok", "model": "deepseek-v4-pro", "finishReason": "stop",
            "cost": 0.0001}
        return resp


def ok_usage(cost=0.0001):
    return {"ok": True, "statusCode": 200,
            "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                      "prompt_cache_hit_tokens": 0},
            "content": "ok", "model": "deepseek-v4-pro",
            "finishReason": "stop", "cost": cost}


def make_dispatcher(tmp, n_accounts=4, transport=None):
    resolver = lambda ref: f"KEY:{ref}"  # doble: jamás una clave real
    pool = AccountPool([
        {"label": f"c{i}", "key_ref": f"ref-{i}",
         "budget_usd_diario": 5.0, "max_concurrent": 1} for i in range(n_accounts)],
        resolver=resolver)
    budget = WeeklyBudget(Path(tmp) / "budget.json")
    budget.set_weekly("unit-a", 10.0)
    return MultiAccountDispatcher(
        pool, budget, resolver, transport or FakeTransport())


class TestMultiAccountDispatcher(unittest.TestCase):

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.plan = {"max_tokens": 50, "rate_usd_per_1m": 1.0}

    def test_submit_ok_uses_account_and_receipt(self):
        t = FakeTransport()
        d = make_dispatcher(self._td.name, transport=t)
        r = d.submit("unit-a", [{"role": "user", "content": "hola"}], self.plan)
        self.assertEqual(r["status"], "ok")
        self.assertIn(r["account"], ("c0", "c1", "c2", "c3"))
        self.assertFalse(r["failoverUsed"])
        self.assertEqual(r["cost"], 0.0001)
        self.assertEqual(len(t.calls), 1)
        # La clave del doble viaja SOLO en memoria del transporte.
        self.assertTrue(t.calls[0][1].startswith("KEY:ref-"))

    def test_402_fails_over_single_jump(self):
        t = FakeTransport()
        t.script = [{"ok": False, "statusCode": 402, "cost": 0.0,
                     "usage": None, "error": "402"},
                    ok_usage()]
        d = make_dispatcher(self._td.name, transport=t)
        r = d.submit("unit-a", [{"role": "user", "content": "hola"}], self.plan)
        self.assertTrue(r["failoverUsed"])
        self.assertEqual(len(t.calls), 2)
        self.assertNotEqual(t.calls[0][1], t.calls[1][1])  # cuentas distintas

    def test_402_both_accounts_rejected(self):
        t = FakeTransport()
        t.script = [{"ok": False, "statusCode": 402, "cost": 0.0,
                     "usage": None},
                    {"ok": False, "statusCode": 402, "cost": 0.0,
                     "usage": None}]
        d = make_dispatcher(self._td.name, n_accounts=2, transport=t)
        with self.assertRaises(DispatcherError) as cm:
            d.submit("unit-a", [{"role": "user", "content": "hola"}], self.plan)
        self.assertEqual(cm.exception.code, "provider-rejected")

    def test_transport_error_uncertain_no_retry(self):
        t = FakeTransport()
        t.script = [{"ok": False, "uncertain": True, "cost": 0.0,
                     "usage": None, "error": "timeout"}]
        d = make_dispatcher(self._td.name, transport=t)
        with self.assertRaises(DispatcherError) as cm:
            d.submit("unit-a", [{"role": "user", "content": "hola"}], self.plan)
        self.assertEqual(cm.exception.code, "uncertain-outcome")
        self.assertEqual(len(t.calls), 1)  # jamás reintenta

    def test_budget_gate_blocks_before_network(self):
        t = FakeTransport()
        d = make_dispatcher(self._td.name, transport=t)
        d.budget.set_weekly("unit-a", 0.000001)  # casi nada
        with self.assertRaises(DispatcherError) as cm:
            d.submit("unit-a", [{"role": "user", "content": "hola"}], self.plan)
        self.assertEqual(cm.exception.code, "budget-gate")
        self.assertEqual(len(t.calls), 0)  # sin red

    def test_cache_hit_zero_cost_no_network(self):
        from semantic_cache import SemanticCache
        t = FakeTransport()
        cache = SemanticCache(max_entries=10)
        d = make_dispatcher(self._td.name, transport=t)
        d.cache = cache
        msgs = [{"role": "user", "content": "hola"}]
        r1 = d.submit("unit-a", msgs, self.plan)
        r2 = d.submit("unit-a", msgs, self.plan)
        self.assertEqual(r1["status"], "ok")
        self.assertEqual(r2["status"], "cache-hit")
        self.assertEqual(r2["cost"], 0.0)
        self.assertEqual(len(t.calls), 1)  # solo el primer POST

    def test_missing_usage_after_ok_raises(self):
        t = FakeTransport()
        t.script = [{"ok": True, "statusCode": 200, "cost": 0.0,
                     "usage": {}, "content": "x"}]
        d = make_dispatcher(self._td.name, transport=t)
        with self.assertRaises(DispatcherError) as cm:
            d.submit("unit-a", [{"role": "user", "content": "hola"}], self.plan)
        self.assertEqual(cm.exception.code, "missing-usage")

    def test_no_account_available_refunds_and_raises(self):
        pool = AccountPool([{"label": "c0", "key_ref": "ref-0",
                             "budget_usd_diario": 0.0, "max_concurrent": 1}])
        budget = WeeklyBudget(Path(self._td.name) / "b.json")
        budget.set_weekly("unit-a", 10.0)
        d = MultiAccountDispatcher(pool, budget, lambda r: "KEY",
                                   FakeTransport())
        with self.assertRaises(DispatcherError) as cm:
            d.submit("unit-a", [{"role": "user", "content": "hola"}], self.plan)
        self.assertEqual(cm.exception.code, "no-account")

    def test_keys_never_in_receipt_or_errors(self):
        t = FakeTransport()
        t.script = [{"ok": False, "statusCode": 500, "cost": 0.0,
                     "usage": None, "error": "boom"}]
        d = make_dispatcher(self._td.name, transport=t)
        try:
            d.submit("unit-a", [{"role": "user", "content": "hola"}], self.plan)
        except DispatcherError as e:
            self.assertNotIn("KEY:", str(e))
        snap = d.budget.snapshot()
        self.assertNotIn("KEY:", json.dumps(snap))


if __name__ == "__main__":
    unittest.main(verbosity=2)
