"""cost_meter.py — métrica de coste por token (stdlib puro).

Tarifas CONGELADAS del plan (misma disciplina del adaptador deepseek-env y del
carril: jamás tarifas de hoy). Calcula $ por llamada, por unidad de trabajo,
por token, y el ahorro vs baseline con fórmula documentada.
"""
from __future__ import annotations

# tarifas de referencia del carril (USD por 1M tokens) — ajustables por plan
FROZEN_PRICES: dict[str, dict] = {
    "deepseek-chat": {"input_per_1m": 0.66, "output_per_1m": 1.98,
                      "cache_hit_per_1m": 0.14},
    "deepseek-reasoner": {"input_per_1m": 2.19, "output_per_1m": 6.57,
                          "cache_hit_per_1m": 0.55},
}


class CostMeter:
    def __init__(self, prices: dict | None = None):
        self.prices = prices or FROZEN_PRICES
        self.calls: list[dict] = []

    def cost(self, model: str, prompt_tokens: int,
             prompt_cache_hit_tokens: int, completion_tokens: int) -> dict:
        p = self.prices[model]
        cache_per = p.get("cache_hit_per_1m", p["input_per_1m"])
        amount = round(((prompt_tokens - prompt_cache_hit_tokens)
                        * p["input_per_1m"]
                        + prompt_cache_hit_tokens * cache_per
                        + completion_tokens * p["output_per_1m"]) / 1_000_000.0,
                       6)
        total_tokens = prompt_tokens + completion_tokens
        call = {"model": model, "prompt_tokens": prompt_tokens,
                "prompt_cache_hit_tokens": prompt_cache_hit_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "cost_usd": amount,
                "usd_per_token": round(amount / total_tokens, 10)
                if total_tokens else 0.0}
        self.calls.append(call)
        return call

    def by_unit(self, unit_id: str) -> dict:
        calls = [c for c in self.calls if c.get("unit_id") == unit_id]
        return {"unit_id": unit_id, "calls": len(calls),
                "tokens": sum(c["total_tokens"] for c in calls),
                "cost_usd": round(sum(c["cost_usd"] for c in calls), 6)}

    def totals(self) -> dict:
        return {"calls": len(self.calls),
                "tokens": sum(c["total_tokens"] for c in self.calls),
                "cost_usd": round(sum(c["cost_usd"] for c in self.calls), 6)}

    @staticmethod
    def savings_pct(baseline_cost_usd: float, actual_cost_usd: float) -> float:
        """Ahorro % vs baseline: (1 - actual/baseline)*100; >=100% solo si
        baseline > 0 y actual == 0 (coste cero)."""
        if baseline_cost_usd <= 0:
            return 0.0
        return round((1.0 - actual_cost_usd / baseline_cost_usd) * 100.0, 2)
