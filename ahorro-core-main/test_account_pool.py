#!/usr/bin/env python3
"""test_account_pool.py — Pruebas de account_pool.py con dobles inyectados."""

import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from account_pool import AccountPool  # noqa: E402


def make_pool(n=4, budgets=None, clock=None, max_concurrent=1):
    accounts = [
        {"label": f"cuenta-{i}", "key_ref": f"deepseek_{i}",
         "budget_usd_diario": (budgets or [5.0] * n)[i],
         "max_concurrent": max_concurrent}
        for i in range(n)
    ]
    return AccountPool(accounts, clock=clock)


class TestAccountPool(unittest.TestCase):

    def test_pick_round_robin_with_budget_weight(self):
        pool = make_pool(budgets=[10.0, 1.0, 10.0, 1.0])
        picks = [pool.pick() for _ in range(4)]
        self.assertEqual(len(set(picks)), 4)  # 4 cuentas distintas
        # Las de más presupuesto (0 y 2) salen antes que las de 1.0.
        self.assertLess(picks.index("cuenta-0"), picks.index("cuenta-1"))
        self.assertLess(picks.index("cuenta-2"), picks.index("cuenta-3"))

    def test_pick_exhausts_budget_then_none(self):
        pool = make_pool(n=1, budgets=[0.0])
        self.assertIsNone(pool.pick())

    def test_402_marks_no_balance_and_single_failover(self):
        pool = make_pool()
        first = pool.pick()
        pool.release(first, ok=False, status=402)
        snap = pool.snapshot()["accounts"][first]
        self.assertTrue(snap["no_balance"])
        nxt = pool.next_after_402(first)
        self.assertIsNotNone(nxt)
        self.assertNotEqual(nxt, first)
        # La marcada ya no vuelve a salir en pick().
        self.assertNotIn(first, [pool.pick() for _ in range(10)])

    def test_402_no_failover_if_only_account(self):
        pool = make_pool(n=1)
        first = pool.pick()
        pool.release(first, ok=False, status=402)
        self.assertIsNone(pool.next_after_402(first))

    def test_success_accumulates_metrics(self):
        pool = make_pool()
        lbl = pool.pick()
        pool.release(lbl, ok=True, cost_usd=0.05, tokens=120)
        snap = pool.snapshot()["accounts"][lbl]
        self.assertEqual(snap["calls"], 1)
        self.assertEqual(snap["tokens"], 120)
        self.assertAlmostEqual(snap["spent_usd"], 0.05, places=6)

    def test_concurrency_limit(self):
        pool = make_pool(n=1)
        a = pool.pick()
        b = pool.pick()
        self.assertIsNotNone(a)
        self.assertIsNone(b)  # max_concurrent=1 alcanzado
        pool.release(a)
        self.assertIsNotNone(pool.pick())

    def test_daily_rollover_resets_spent_and_no_balance(self):
        today = dt.datetime(2026, 9, 15, 10, 0, tzinfo=dt.timezone.utc)
        tomorrow = dt.datetime(2026, 9, 16, 0, 30, tzinfo=dt.timezone.utc)
        state = {"now": today}
        pool = make_pool(n=1, budgets=[5.0], clock=lambda: state["now"])
        lbl = pool.pick()
        pool.release(lbl, ok=False, status=402)
        self.assertTrue(pool.snapshot()["accounts"][lbl]["no_balance"])
        state["now"] = tomorrow
        snap = pool.snapshot()["accounts"][lbl]
        self.assertFalse(snap["no_balance"])
        self.assertEqual(snap["spent_usd"], 0.0)

    def test_snapshot_json_valid(self):
        pool = make_pool()
        import json
        data = json.loads(__import__("account_pool").snapshot_json(pool))
        self.assertEqual(len(data["accounts"]), 4)
        self.assertIn("total_remaining_usd", data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
