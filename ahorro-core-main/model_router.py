"""model_router.py — routing de modelos con GATE DE CALIDAD (stdlib puro).

Principio del propietario: AHORRO JAMÁS CON MENOR CALIDAD. Este router:
1. impone un modelo MÍNIMO admisible por clase de tarea (suelo de calidad);
2. solo considera modelos cuyo quality_score >= suelo (gate de calidad);
3. entre los que PASAN el gate, elige el más barato (ahorro real);
4. si ninguno pasa el gate (o falta presupuesto), FALLA CERRADO hacia el
   modelo de calidad de la clase, jamás hacia uno peor.
"""
from __future__ import annotations

from typing import Any

# calidad relativa de referencia (0-1) por modelo y suelo por clase de tarea
MODEL_QUALITY: dict[str, float] = {
    "deepseek-reasoner": 1.00,
    "deepseek-chat": 0.92,
    "deepseek-lite": 0.70,
}
CLASS_FLOOR: dict[str, float] = {
    "simple": 0.90,       # admite chat (0.92), NO lite (0.70)
    "complex": 0.98,      # solo reasoner
    "generation": 0.90,
}


class ModelRouter:
    def __init__(self, prices: dict[str, dict], quality: dict | None = None,
                 floors: dict | None = None):
        self.prices = prices
        self.quality = quality or MODEL_QUALITY
        self.floors = floors or CLASS_FLOOR
        self.decisions: list[dict] = []

    def _cost_est(self, model: str, prompt_tokens: int,
                  completion_tokens: int) -> float:
        p = self.prices[model]
        return round((prompt_tokens * p["input_per_1m"]
                      + completion_tokens * p["output_per_1m"]) / 1_000_000.0, 6)

    def route(self, task_class: str, prompt_tokens: int,
              completion_tokens: int, budget_left_usd: float) -> dict:
        floor = self.floors.get(task_class)
        if floor is None:
            return {"routed": False, "reason": "unknown-task-class"}
        candidates = [m for m in sorted(self.prices,
                                        key=lambda m: self._cost_est(
                                            m, prompt_tokens, completion_tokens))
                      if self.quality.get(m, 0.0) >= floor]
        if not candidates:
            # fail-closed a calidad: sin candidato que pase el gate, nada barato
            return {"routed": False, "reason": "no-model-passes-quality-gate",
                    "floor": floor}
        decision = None
        for m in candidates:
            cost = self._cost_est(m, prompt_tokens, completion_tokens)
            if cost <= budget_left_usd:
                decision = {"routed": True, "model": m, "cost_est": cost,
                            "quality_score": self.quality[m], "floor": floor,
                            "gate": "PASA"}
                break
        if decision is None:
            best = candidates[-1]  # el de mayor calidad entre los que pasan gate
            decision = {"routed": False,
                        "reason": "budget-insufficient",
                        "cheapest_passing": candidates[0],
                        "quality_safe_model": best,
                        "cost_est": self._cost_est(best, prompt_tokens,
                                                  completion_tokens),
                        "floor": floor}
        self.decisions.append(decision)
        return decision

    def stats(self) -> dict:
        routed = [d for d in self.decisions if d.get("routed")]
        return {"decisions": len(self.decisions),
                "routed": len(routed),
                "degraded_never": all(
                    d.get("quality_score", 1.0) >= d.get("floor", 0.0)
                    for d in self.decisions if d.get("routed")),
                "fail_closed_to_quality": sum(
                    1 for d in self.decisions if not d.get("routed"))}
