"""budget_gate.py — presupuesto por unidad de trabajo (stdlib puro, sin red).

Principio del propietario: AHORRO CON CALIDAD IGUAL O SUPERIOR. Esta pieza
aporta la contabilidad EXACTA del gasto por unidad de trabajo: si no hay
presupuesto, NO se ejecuta nada (fail-closed). Las cuentas usan redondeo a
6 decimales en cada operación (misma disciplina que el adaptador deepseek-env).
"""
from __future__ import annotations

import json
from typing import Any


class BudgetGateError(Exception):
    pass


class BudgetGate:
    """Contabilidad por unidad de trabajo con límites duros y fail-closed."""

    def __init__(self, limits: dict[str, Any]):
        self.max_tokens = int(limits.get("max_tokens", 0))
        self.max_cost_usd = float(limits.get("max_cost_usd", 0.0))
        self._tokens_committed = 0
        self._cost_committed = 0.0
        self._units: dict[str, dict] = {}

    # -- consulta ----------------------------------------------------------
    def remaining(self) -> dict:
        return {
            "remaining_tokens": self.max_tokens - self._tokens_committed,
            "remaining_cost_usd": round(self.max_cost_usd
                                        - self._cost_committed, 6),
        }

    # -- admisión (fail-closed) --------------------------------------------
    def allow(self, unit_id: str, tokens_est: int, cost_est: float) -> dict:
        tokens_est = int(tokens_est)
        cost_est = round(float(cost_est), 6)
        if self.max_tokens <= 0 or self.max_cost_usd <= 0.0:
            # sin presupuesto configurado => nada se admite (fail-closed)
            return {"allowed": False, "reason": "budget-not-configured",
                    "remaining": self.remaining()}
        if tokens_est < 1:
            return {"allowed": False, "reason": "invalid-tokens",
                    "remaining": self.remaining()}
        if cost_est < 0:
            return {"allowed": False, "reason": "invalid-cost",
                    "remaining": self.remaining()}
        if self._tokens_committed + tokens_est > self.max_tokens:
            return {"allowed": False, "reason": "tokens-over-budget",
                    "remaining": self.remaining()}
        new_cost = round(self._cost_committed + cost_est, 6)
        if new_cost > self.max_cost_usd:
            return {"allowed": False, "reason": "cost-over-budget",
                    "remaining": self.remaining()}
        self._tokens_committed += tokens_est
        self._cost_committed = new_cost
        self._units[unit_id] = {"tokens_est": tokens_est,
                                "cost_est": cost_est, "settled": False}
        return {"allowed": True, "reason": "ok", "remaining": self.remaining()}

    # -- liquidación (uso REAL, jamás la estimación) ------------------------
    def settle(self, unit_id: str, tokens_used: int, cost_used: float) -> dict:
        unit = self._units.get(unit_id)
        if unit is None:
            raise BudgetGateError(f"unit {unit_id!r} never admitted")
        if unit.get("settled"):
            raise BudgetGateError(f"unit {unit_id!r} already settled")
        tokens_used = int(tokens_used)
        cost_used = round(float(cost_used), 6)
        # ajuste exacto: devolver la diferencia entre estimación y uso real
        self._tokens_committed += tokens_used - unit["tokens_est"]
        self._cost_committed = round(self._cost_committed
                                     + cost_used - unit["cost_est"], 6)
        unit.update({"tokens_used": tokens_used, "cost_used": cost_used,
                     "settled": True})
        return {"unit_id": unit_id, "tokens_used": tokens_used,
                "cost_used": cost_used, "remaining": self.remaining()}

    # -- informe -----------------------------------------------------------
    def report(self) -> dict:
        settled = [u for u in self._units.values() if u.get("settled")]
        return {
            "limits": {"max_tokens": self.max_tokens,
                       "max_cost_usd": self.max_cost_usd},
            "committed": {"tokens": self._tokens_committed,
                          "cost_usd": round(self._cost_committed, 6)},
            "remaining": self.remaining(),
            "units_total": len(self._units),
            "units_settled": len(settled),
            "units_open": len(self._units) - len(settled),
        }

    def to_json(self) -> str:
        return json.dumps(self.report(), ensure_ascii=False, sort_keys=True)
