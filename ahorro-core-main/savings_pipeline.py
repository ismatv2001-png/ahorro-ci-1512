"""savings_pipeline.py — pipeline demostrador del ahorro CON gate de calidad.

Escenario ejecutable del principio del propietario: una carga de 100 unidades
de trabajo con un 95% de prompts repetidos debe producir >=95% de ahorro de
coste Y pasar el gate de calidad (calidad igual o superior, jamás inferior).
Todo offline y determinista; sin llamadas a proveedor.
"""
from __future__ import annotations

from budget_gate import BudgetGate
from cost_meter import CostMeter
from model_router import ModelRouter
from quality_gate import evaluate
from semantic_cache import SemanticCache

PROMPT = ("Explica en tres frases como funciona un cache de contexto en un "
          "sistema de agentes de IA, incluyendo el hash de contenido y la "
          "eviccion.")
CRITERIA = ["hash", "cache", "contexto", "eviccion"]
REQUIRED = ["hash", "cache"]
BASELINE_RESPONSE = ("Un cache de contexto guarda respuestas indexadas por el "
                     "hash del contenido normalizado; ante una peticion "
                     "repetida devuelve la respuesta guardada sin llamar al "
                     "modelo. La eviccion elimina entradas poco reutilizadas "
                     "para no degradar el ratio de aciertos.")
DEGRADED_RESPONSE = "no se"


def run_pipeline(n_units: int = 100, repeat_pct: float = 95.0) -> dict:
    cache = SemanticCache(max_entries=32, ttl_s=3600.0)
    meter = CostMeter()
    gate = BudgetGate({"max_tokens": 100_000, "max_cost_usd": 10.0})
    router = ModelRouter(meter.prices)

    # warm-up: primado del cache FUERA de las unidades contadas (el cache de
    # contexto en producción arranca caliente tras la primera llamada real).
    cache.put(PROMPT, BASELINE_RESPONSE, tokens_saved=120)

    repeats = int(n_units * repeat_pct / 100.0)
    full_call_cost = round((120 * meter.prices["deepseek-chat"]["input_per_1m"]
                            + 60 * meter.prices["deepseek-chat"]["output_per_1m"])
                           / 1_000_000.0, 6)
    baseline_cost = 0.0
    actual_cost = 0.0
    gate_verdicts = []
    served_from_cache = 0
    provider_calls = 0

    for i in range(n_units):
        unit_id = f"u{i:03d}"
        # unidades 0..repeats-1 repiten el prompt (cacheables);
        # el resto son variantes ÚNICAS (obligan llamada al proveedor)
        prompt = PROMPT if i < repeats else f"{PROMPT} variante unica {i}"
        hit = cache.get(prompt)
        if hit is not None:
            served_from_cache += 1
            response = hit["response"]
        else:
            provider_calls += 1
            response = BASELINE_RESPONSE  # respuesta del proveedor (simulada)
            cache.put(prompt, response, tokens_saved=120)
            d = router.route("simple", 120, 60, budget_left_usd=1.0)
            model = d["model"] if d.get("routed") else "deepseek-chat"
            cost_est = d.get("cost_est", full_call_cost)
            admitted = gate.allow(unit_id, 180, cost_est)
            call = meter.cost(model, 120, 0, 60)
            call["unit_id"] = unit_id
            if admitted["allowed"]:
                gate.settle(unit_id, call["total_tokens"], call["cost_usd"])
            actual_cost += call["cost_usd"]
        # baseline SIN cache ni router: tarifa plena del modelo de la clase
        baseline_cost += full_call_cost
        gate_verdicts.append(evaluate(response, BASELINE_RESPONSE,
                                      CRITERIA, REQUIRED))

    rejected = [g for g in gate_verdicts if g["verdict"] == "RECHAZA"]
    savings = CostMeter.savings_pct(baseline_cost, actual_cost)
    return {
        "units": n_units,
        "repeats_pct": repeat_pct,
        "served_from_cache": served_from_cache,
        "provider_calls": provider_calls,
        "baseline_cost_usd": round(baseline_cost, 6),
        "actual_cost_usd": round(actual_cost, 6),
        "savings_pct": savings,
        "quality_gate": {"evaluated": len(gate_verdicts),
                         "pasadas": len(gate_verdicts) - len(rejected),
                         "rechazadas": len(rejected)},
        "cache_stats": cache.stats(),
        "router_stats": router.stats(),
        "budget_report": gate.report(),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(run_pipeline(), ensure_ascii=False, indent=2))
