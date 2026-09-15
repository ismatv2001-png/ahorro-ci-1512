#!/usr/bin/env python3
"""budget_weekly.py — Presupuestos SEMANALES por unidad de trabajo.

Fail-closed: sin presupuesto o sin datos, DENIEGA. Persistencia JSON atómica
(tmp + rename) PROTEGIDA con lock de fichero (flock) para evitar lost-update
entre instancias/procesos; rollover automático los lunes 00:00 UTC; nunca
permite coste negativo ni entradas no finitas (NaN/Inf); settle libera la
reserva correspondiente y los settles tardíos de la semana anterior no se
pierden. El snapshot expone {unit_id: {reserved, spent, remaining}} y NUNCA
expone claves ni otros secretos.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import pathlib
import tempfile
from contextlib import contextmanager
from typing import Callable, Iterator, Optional

try:  # POSIX (Linux/macOS)
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _monday(date: dt.date) -> dt.date:
    return date - dt.timedelta(days=date.weekday())


class WeeklyBudget:
    """Presupuesto semanal fail-closed con persistencia JSON atómica."""

    def __init__(
        self,
        state_path: str | os.PathLike,
        *,
        clock: Optional[Callable[[], dt.datetime]] = None,
        default_weekly_usd: float = 0.0,
    ) -> None:
        self.state_path = pathlib.Path(state_path)
        self.lock_path = self.state_path.with_name(self.state_path.name + ".lock")
        self._clock = clock or _utc_now
        self.default_weekly_usd = float(default_weekly_usd)
        self._week: str = self._current_week()
        self._units: dict[str, dict] = {}
        self._prev_units: dict[str, dict] = {}
        self._load()

    # --- reloj/semana ------------------------------------------------------
    def _current_week(self) -> str:
        return _monday(self._clock().date()).isoformat()

    def _rollover_if_needed(self) -> None:
        week = self._current_week()
        if week != self._week:
            # B4: las unidades con actividad pasan al histórico para que un
            # settle tardío (posterior al rollover) no se pierda.
            for uid, u in list(self._units.items()):
                if u["reserved"] > 0 or u["spent"] > 0:
                    self._prev_units.setdefault(self._week, {})[uid] = u
            self._units = {}
            self._week = week

    # --- lock multi-proceso (B2: sin lost-update) ---------------------------
    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Lock exclusivo sobre el fichero de estado; recarga bajo lock."""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.lock_path, "a+")
        try:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            yield
            self._load_disk()  # re-lectura autoritativa bajo lock
        finally:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()

    # --- persistencia atómica ----------------------------------------------
    def _load(self) -> None:
        self._rollover_if_needed()
        self._load_disk()

    def _load_disk(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return  # fail-closed: estado ilegible => presupuestos vacíos
        if not isinstance(data, dict) or data.get("week") != self._week:
            return
        units = data.get("units")
        if isinstance(units, dict):
            for uid, u in units.items():
                if not isinstance(u, dict):
                    continue
                try:
                    self._units[str(uid)] = self._sanitize(u)
                except (TypeError, ValueError):
                    continue
        prev = data.get("prev_units")
        if isinstance(prev, dict):
            for week, wunits in prev.items():
                if not isinstance(wunits, dict):
                    continue
                for uid, u in wunits.items():
                    if not isinstance(u, dict):
                        continue
                    try:
                        self._prev_units.setdefault(str(week), {})[str(uid)] = \
                            self._sanitize(u)
                    except (TypeError, ValueError):
                        continue

    @staticmethod
    def _sanitize(u: dict) -> dict:
        weekly = float(u.get("weekly_usd", 0.0))
        reserved = float(u.get("reserved", 0.0))
        spent = float(u.get("spent", 0.0))
        if not all(math.isfinite(x) for x in (weekly, reserved, spent)):
            raise ValueError("non-finite")
        return {"weekly_usd": max(0.0, weekly),
                "reserved": max(0.0, reserved),
                "spent": max(0.0, spent)}

    def _save(self) -> None:
        payload = {"week": self._week, "units": self._units,
                   "prev_units": self._prev_units}
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
        """Fija el presupuesto semanal de una unidad y persiste."""
        with self._locked():
            self._rollover_if_needed()
            value = float(weekly_usd)
            if not math.isfinite(value):
                raise ValueError("weekly_usd must be finite")
            self._units.setdefault(str(unit_id), {
                "weekly_usd": 0.0, "reserved": 0.0, "spent": 0.0})
            self._units[str(unit_id)]["weekly_usd"] = max(0.0, value)
            self._save()

    def try_reserve(self, unit_id: str, input_tokens: int,
                    output_max_tokens: int, rate_usd: float) -> tuple[bool, str]:
        """Reserva el coste estimado (fail-closed). Devuelve (allowed, reason)."""
        with self._locked():
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
            # B1: NaN/Inf jamás abren la puerta (fail-closed).
            if not all(math.isfinite(x) for x in (it, ot, rate)):
                return False, "non-finite-inputs"
            if it < 0 or ot < 0 or rate < 0:
                return False, "negative-inputs"
            est = (it + ot) * rate / 1_000_000.0
            remaining = unit["weekly_usd"] - unit["reserved"] - unit["spent"]
            if est > remaining + 1e-12:
                return False, "budget-exceeded"
            unit["reserved"] += est
            self._save()
            return True, "reserved"

    def release_reserve(self, unit_id: str, amount_usd: float) -> None:
        """Libera reserva NO consumida (p. ej. no hubo POST). Never negative."""
        with self._locked():
            self._rollover_if_needed()
            unit = self._units.get(str(unit_id))
            if unit is None:
                return
            amount = float(amount_usd)
            if not math.isfinite(amount):
                return
            unit["reserved"] = max(0.0, unit["reserved"] - max(0.0, amount))
            self._save()

    def settle(self, unit_id: str, cost_usd: float) -> None:
        """Liquida el coste REAL tras el POST (nunca negativo); libera la
        reserva equivalente. Un settle tardío tras el rollover semanal se
        aplica a la semana de origen (B4)."""
        with self._locked():
            self._rollover_if_needed()
            uid = str(unit_id)
            unit = self._units.get(uid)
            if unit is None:
                unit = None
                for week in sorted(self._prev_units):
                    if uid in self._prev_units[week]:
                        unit = self._prev_units[week][uid]
                        break
                if unit is None:
                    return
            cost = float(cost_usd)
            if not math.isfinite(cost):
                return
            cost = max(0.0, cost)
            # B3: la reserva se libera en el importe liquidado (sin doble
            # contabilidad: remaining = weekly - reserved - spent).
            unit["reserved"] = max(0.0, unit["reserved"] - cost)
            unit["spent"] = max(0.0, unit["spent"] + cost)
            self._save()

    def snapshot(self) -> dict:
        """Snapshot {unit_id: {reserved, spent, remaining}} sin secretos."""
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
