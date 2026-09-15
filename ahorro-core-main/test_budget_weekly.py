#!/usr/bin/env python3
"""test_budget_weekly.py — Pruebas de budget_weekly.py (fail-closed)."""

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from budget_weekly import WeeklyBudget  # noqa: E402


def make_budget(tmp, clock=None):
    return WeeklyBudget(Path(tmp) / "state" / "budget.json", clock=clock)


class TestWeeklyBudget(unittest.TestCase):

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)

    def test_fail_closed_without_budget(self):
        b = make_budget(self._td.name)
        allowed, reason = b.try_reserve("u1", 100, 100, 1.0)
        self.assertFalse(allowed)
        self.assertEqual(reason, "budget-exceeded")

    def test_reserve_and_settle(self):
        b = make_budget(self._td.name)
        b.set_weekly("u1", 1.0)  # 1 USD/semana
        allowed, reason = b.try_reserve("u1", 500_000, 100_000, 1.0)
        self.assertTrue(allowed, reason)  # 0.60 USD estimado
        b.settle("u1", 0.40)
        snap = b.snapshot()["units"]["u1"]
        # Semántica corregida (B3): settle libera la reserva equivalente —
        # sin doble contabilidad. remaining = weekly - reserved - spent.
        self.assertAlmostEqual(snap["reserved"], 0.20, places=6)
        self.assertAlmostEqual(snap["spent"], 0.40, places=6)
        self.assertAlmostEqual(snap["remaining"], 0.40, places=6)

    def test_exceeded_denied(self):
        b = make_budget(self._td.name)
        b.set_weekly("u1", 0.10)
        allowed, reason = b.try_reserve("u1", 1_000_000, 0, 1.0)  # 1.0 USD
        self.assertFalse(allowed)
        self.assertEqual(reason, "budget-exceeded")

    def test_negative_inputs_denied(self):
        b = make_budget(self._td.name)
        b.set_weekly("u1", 1.0)
        self.assertFalse(b.try_reserve("u1", -1, 10, 1.0)[0])
        self.assertFalse(b.try_reserve("u1", 10, -1, 1.0)[0])
        self.assertFalse(b.try_reserve("u1", 10, 10, -1.0)[0])
        self.assertFalse(b.try_reserve("u1", "x", 10, 1.0)[0])

    def test_settle_never_negative(self):
        b = make_budget(self._td.name)
        b.set_weekly("u1", 1.0)
        b.settle("u1", -5.0)
        self.assertEqual(b.snapshot()["units"]["u1"]["spent"], 0.0)

    def test_rollover_monday_utc(self):
        now = dt.datetime(2026, 9, 13, 12, 0, tzinfo=dt.timezone.utc)  # domingo
        later = dt.datetime(2026, 9, 15, 0, 30, tzinfo=dt.timezone.utc)  # lunes
        state = {"now": now}
        b = make_budget(self._td.name, clock=lambda: state["now"])
        b.set_weekly("u1", 1.0)
        b.try_reserve("u1", 100_000, 0, 1.0)
        state["now"] = later
        snap = b.snapshot()
        self.assertEqual(snap["week"], "2026-09-14")
        self.assertNotIn("u1", snap["units"])  # estado de la semana anterior fuera

    def test_persistence_atomic_and_reload(self):
        b = make_budget(self._td.name)
        b.set_weekly("u1", 2.0)
        b.try_reserve("u1", 100_000, 0, 1.0)
        b2 = make_budget(self._td.name)
        snap = b2.snapshot()["units"]["u1"]
        self.assertAlmostEqual(snap["weekly_usd"], 2.0, places=6)
        self.assertAlmostEqual(snap["reserved"], 0.1, places=6)

    def test_corrupt_state_fail_closed(self):
        p = Path(self._td.name) / "state" / "budget.json"
        p.parent.mkdir(parents=True)
        p.write_text("{corrupt json", encoding="utf-8")
        b = make_budget(self._td.name)
        self.assertEqual(b.snapshot()["units"], {})

    def test_snapshot_no_secret_fields(self):
        b = make_budget(self._td.name)
        b.set_weekly("u1", 1.0)
        raw = json.dumps(b.snapshot(), sort_keys=True)
        self.assertNotIn("key", raw.lower().replace("weekly", ""))
        for uid, u in b.snapshot()["units"].items():
            self.assertEqual(
                set(u), {"weekly_usd", "reserved", "spent", "remaining"})

    def test_reserve_double_counting_blocks_second(self):
        b = make_budget(self._td.name)
        b.set_weekly("u1", 1.0)
        ok1, _ = b.try_reserve("u1", 800_000, 0, 1.0)  # 0.80
        ok2, reason = b.try_reserve("u1", 800_000, 0, 1.0)  # +0.80 > 1.0
        self.assertTrue(ok1)
        self.assertFalse(ok2)
        self.assertEqual(reason, "budget-exceeded")


if __name__ == "__main__":
    unittest.main(verbosity=2)
