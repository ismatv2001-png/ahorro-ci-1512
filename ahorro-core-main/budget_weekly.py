#!/usr/bin/env python3
"""budget_weekly.py — Presupuestos SEMANALES por unidad de trabajo.

Fail-closed: sin presupuesto o sin datos, DENIEGA. Persistencia JSON atómica
(tmp + rename), rollover automático los lunes 00:00 UTC, nunca permite coste
negativo. El snapshot expone {unit_id: {reserved, spent, remaining}} y NUNCA
expone claves ni otros secretos.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import tempfile
from typing import Callable, Optional


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _monday(date: dt.date) -> dt.date:
    return date - dt.timedelta(days=date.weekday())


class WeeklyBudget:
    def __init__(
        self,
        state_path: str | os.PathLike,
        *,
        clock: Optional[Callable[[], dt.datetime]] = None,
        default_weekly_usd: float = 0.0,
    ) -> None:
        self.state_path = pathlib.Path(state_path)
        self._clock = clock or _utc_now
        self.default_weekly_usd = float(default_weekly_usd)
        self._week: str = self._current_week()
        self._units: dict[str, dict] = {}
        self._load()

    # --- reloj/semana ------------------------------------------------------
    def _current_week(self) -> str:
        return _monday(self._clock().date()).isoformat()

    def _rollover_if_needed(self) -> None:
        week = self._current_week()
        if week != self._week:
            self._units = {}
            self._week = week

    # --- persistencia atómica ----------------------------------------------
    def _load(self) -> None:
        self._rollover_if_needed()
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return  # fail-closed: estado ilegible => presupuestos vacíos
        if not isinstance(data, dict) or data.get("week") != self._week:
            return
        units = data.get("units")
        if not isinstance(units, dict):
            return
        for uid, u in units.items():
            if not isinstance(u, dict):
                continue
            try:
                self._units[str(uid)] = {
                    "weekly_usd": max(0.0, float(u.get("weekly_usd", 0.0))),
                    "reserved": max(0.0, float(u.get("reserved", 0.0))),
                    "spent": max(0.0, float(u.get("spent", 0.0))),
                }
            except (TypeError, ValueError):
                continue

    def _save(self) -> None:
        payload = {"week": self._week, "units": self._units}
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self.state_path.parent), prefix=".budget-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.state_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # --- API ---------------------------------------------------------------
    def set_weekly(self, unit_id: str, weekly_usd: float) -> None:
        self._rollover_if_needed()
        self._units.setdefault(str(unit_id), {
            "weekly_usd": 0.0, "reserved": 0.0, "spent": 0.0})
        self._units[str(unit_id)]["weekly_usd"] = max(0.0, float(weekly_usd))
        self._save()

    def try_reserve(self, unit_id: str, input_tokens: int,
                    output_max_tokens: int, rate_usd: float) -> tuple[bool, str]:
        """Reserva el coste estimado (fail-closed). Devuelve (allowed, reason)."""
        self._rollover_if_needed()
        uid = str(unit_id)
        unit = self._units.get(uid)
        if unit is None:
            unit = {"weekly_usd": self.default_weekly_usd,
                    "reserved": 0.0, "spent": 0.0}
            self._units[uid] = unit
        try:
            it = float(input_tokens)
            ot = float(output_max_tokens)
            rate = float(rate_usd)
        except (TypeError, ValueError):
            return False, "non-numeric-inputs"
        if it < 0 or ot < 0 or rate < 0:
            return False, "negative-inputs"
        est = (it + ot) * rate / 1_000_000.0
        remaining = unit["weekly_usd"] - unit["reserved"] - unit["spent"]
        if est > remaining + 1e-12:
            return False, "budget-exceeded"
        unit["reserved"] += est
        self._save()
        return True, "reserved"

    def settle(self, unit_id: str, cost_usd: float) -> None:
        """Liquida el coste REAL tras el POST (nunca negativo)."""
        self._rollover_if_needed()
        unit = self._units.get(str(unit_id))
        if unit is None:
            return
        cost = max(0.0, float(cost_usd))
        unit["spent"] = max(0.0, unit["spent"] + cost)
        self._save()

    def snapshot(self) -> dict:
        self._rollover_if_needed()
        per = {}
        for uid, u in self._units.items():
            per[uid] = {
                "weekly_usd": round(u["weekly_usd"], 6),
                "reserved": round(u["reserved"], 6),
                "spent": round(u["spent"], 6),
                "remaining": round(max(
                    0.0, u["weekly_usd"] - u["reserved"] - u["spent"]), 6),
            }
        return {"week": self._week, "units": per}
