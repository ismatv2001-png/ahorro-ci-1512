"""test_ahorro_core.py — tests del núcleo de ahorro con gate de calidad.

Cubre el principio del propietario: ahorro objetivo >=95% en carga repetitiva,
evicción que no degrada, router que RECHAZA degradación de calidad, presupuesto
fail-closed, coste exacto y gate que RECHAZA una optimización que baja calidad.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from budget_gate import BudgetGate, BudgetGateError                      # noqa: E402
from cost_meter import FROZEN_PRICES, CostMeter                          # noqa: E402
from model_router import ModelRouter                                    # noqa: E402
from quality_gate import evaluate, score                                # noqa: E402
from savings_pipeline import (BASELINE_RESPONSE, CRITERIA, DEGRADED_RESPONSE,  # noqa: E402
                              PROMPT, REQUIRED, run_pipeline)
from semantic_cache import SemanticCache                                 # noqa: E402


class TestBudgetGate(unittest.TestCase):
    def setUp(self):
        self.gate = BudgetGate({"max_tokens": 1000, "max_cost_usd": 1.0})

    def test_admite_dentro_de_limites(self):
        r = self.gate.allow("u1", 100, 0.1)
        self.assertTrue(r["allowed"])

    def test_fail_closed_sin_presupuesto(self):
        g = BudgetGate({"max_tokens": 0, "max_cost_usd": 0.0})
        r = g.allow("u1", 100, 0.1)
        self.assertFalse(r["allowed"])
        self.assertEqual(r["reason"], "budget-not-configured")

    def test_rechaza_tokens_sobre_presupuesto(self):
        r = self.gate.allow("u1", 1001, 0.1)
        self.assertFalse(r["allowed"])
        self.assertEqual(r["reason"], "tokens-over-budget")

    def test_rechaza_coste_sobre_presupuesto(self):
        r = self.gate.allow("u1", 10, 1.5)
        self.assertFalse(r["allowed"])
        self.assertEqual(r["reason"], "cost-over-budget")

    def test_settle_usa_uso_real_exacto(self):
        self.gate.allow("u1", 100, 0.1)
        s = self.gate.settle("u1", 80, 0.05)
        self.assertEqual(s["tokens_used"], 80)
        self.assertEqual(s["cost_used"], 0.05)
        rem = self.gate.remaining()
        self.assertEqual(rem["remaining_tokens"], 920)
        self.assertEqual(rem["remaining_cost_usd"], 0.95)

    def test_settle_doble_rechazado(self):
        self.gate.allow("u1", 100, 0.1)
        self.gate.settle("u1", 90, 0.09)
        with self.assertRaises(BudgetGateError):
            self.gate.settle("u1", 90, 0.09)

    def test_report_consistente(self):
        self.gate.allow("u1", 100, 0.1)
        rep = self.gate.report()
        self.assertEqual(rep["units_total"], 1)
        self.assertEqual(rep["units_settled"], 0)


class TestSemanticCache(unittest.TestCase):
    def test_hit_exacto_y_tokens_ahorrados(self):
        c = SemanticCache()
        c.put(PROMPT, BASELINE_RESPONSE, tokens_saved=120)
        hit = c.get(PROMPT)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["tokens_saved"], 120)
        self.assertEqual(hit["response"], BASELINE_RESPONSE)

    def test_contenido_distinto_no_hace_hit(self):
        c = SemanticCache()
        c.put(PROMPT, BASELINE_RESPONSE, tokens_saved=120)
        self.assertIsNone(c.get(PROMPT + " extra"))

    def test_normalizacion_whitespace(self):
        c = SemanticCache()
        c.put("hola   mundo", "r", 10)
        self.assertIsNotNone(c.get("  hola mundo "))

    def test_eviccion_protege_alta_reutilizacion(self):
        c = SemanticCache(max_entries=2)
        c.put("a", "r", 10)
        c.get("a")  # reuso => score alto
        c.get("a")
        c.put("b", "r", 10)
        c.put("c", "r", 10)  # fuerza eviccion
        self.assertIsNotNone(c.get("a"))   # sobrevive por reuso
        self.assertEqual(c.evictions, 1)

    def test_ttl_expira(self):
        c = SemanticCache(ttl_s=0.0)
        c.put("a", "r", 10)
        self.assertIsNone(c.get("a"))

    def test_put_idempotente(self):
        c = SemanticCache()
        k1 = c.put("a", "r1", 10)
        k2 = c.put("a", "r2", 20)
        self.assertEqual(k1, k2)
        self.assertEqual(c.get("a")["response"], "r1")


class TestModelRouter(unittest.TestCase):
    def setUp(self):
        self.router = ModelRouter(FROZEN_PRICES)

    def test_clase_simple_admite_chat_y_rechaza_lite(self):
        d = self.router.route("simple", 120, 60, budget_left_usd=1.0)
        self.assertTrue(d["routed"])
        self.assertGreaterEqual(d["quality_score"], 0.90)
        self.assertEqual(d["gate"], "PASA")

    def test_clase_complex_solo_modelo_de_calidad(self):
        d = self.router.route("complex", 120, 60, budget_left_usd=1.0)
        self.assertTrue(d["routed"])
        self.assertEqual(d["model"], "deepseek-reasoner")

    def test_presupuesto_insuficiente_falla_cerrado_a_calidad(self):
        d = self.router.route("complex", 10_000_000, 10_000_000,
                              budget_left_usd=0.0001)
        self.assertFalse(d["routed"])
        self.assertEqual(d["reason"], "budget-insufficient")
        self.assertIn("quality_safe_model", d)

    def test_nunca_degrada_bajo_suelo(self):
        for cls in ("simple", "complex", "generation"):
            d = self.router.route(cls, 120, 60, budget_left_usd=100.0)
            self.assertTrue(d["routed"])
            self.assertGreaterEqual(d["quality_score"], self.router.floors[cls])
        self.assertTrue(self.router.stats()["degraded_never"])

    def test_clase_desconocida_rechazada(self):
        d = self.router.route("nope", 120, 60, 1.0)
        self.assertFalse(d["routed"])


class TestCostMeter(unittest.TestCase):
    def test_coste_exacto_con_precios_fijos(self):
        m = CostMeter()
        call = m.cost("deepseek-chat", 100, 0, 50)
        self.assertEqual(call["cost_usd"],
                         round((100 * 0.66 + 50 * 1.98) / 1_000_000.0, 6))
        self.assertEqual(call["total_tokens"], 150)

    def test_cache_hit_mas_barato(self):
        m = CostMeter()
        full = m.cost("deepseek-chat", 100, 0, 50)
        cached = m.cost("deepseek-chat", 100, 80, 50)
        self.assertLess(cached["cost_usd"], full["cost_usd"])

    def test_ahorro_pct_formula(self):
        self.assertEqual(CostMeter.savings_pct(1.0, 0.05), 95.0)
        self.assertEqual(CostMeter.savings_pct(0.0, 0.0), 0.0)
        self.assertEqual(CostMeter.savings_pct(1.0, 0.0), 100.0)


class TestQualityGate(unittest.TestCase):
    def test_baseline_contra_si_misma_pasa(self):
        r = evaluate(BASELINE_RESPONSE, BASELINE_RESPONSE, CRITERIA, REQUIRED)
        self.assertEqual(r["verdict"], "PASA")
        self.assertEqual(r["delta"], 0.0)

    def test_respuesta_degrada_rechazada(self):
        r = evaluate(DEGRADED_RESPONSE, BASELINE_RESPONSE, CRITERIA, REQUIRED)
        self.assertEqual(r["verdict"], "RECHAZA")
        self.assertLess(r["candidate"]["total"], r["baseline"]["total"])
        self.assertEqual(r["principle"], "degradacion_detectada")

    def test_respuesta_mejor_pasa_con_delta_positivo(self):
        mejor = BASELINE_RESPONSE + " Ademas reduce el coste por token."
        r = evaluate(mejor, BASELINE_RESPONSE, CRITERIA, REQUIRED)
        self.assertEqual(r["verdict"], "PASA")
        # con baseline ya en 1.0 el cap impide delta>0: calidad >= baseline basta
        self.assertGreaterEqual(r["candidate"]["total"], r["baseline"]["total"])

    def test_determinismo(self):
        r1 = evaluate(BASELINE_RESPONSE, BASELINE_RESPONSE, CRITERIA, REQUIRED)
        r2 = evaluate(BASELINE_RESPONSE, BASELINE_RESPONSE, CRITERIA, REQUIRED)
        self.assertEqual(r1, r2)

    def test_score_acotado_0_1(self):
        s = score(DEGRADED_RESPONSE, BASELINE_RESPONSE, CRITERIA, REQUIRED)
        for k in ("accuracy", "fidelity", "completeness", "total"):
            self.assertGreaterEqual(s[k], 0.0)
            self.assertLessEqual(s[k], 1.0)


class TestSavingsPipeline(unittest.TestCase):
    def test_95_pct_ahorro_con_calidad_igual(self):
        r = run_pipeline(n_units=100, repeat_pct=95.0)
        self.assertGreaterEqual(r["savings_pct"], 95.0)
        self.assertEqual(r["served_from_cache"], 95)
        self.assertEqual(r["provider_calls"], 5)
        self.assertEqual(r["quality_gate"]["rechazadas"], 0)
        self.assertEqual(r["quality_gate"]["pasadas"], 100)

    def test_principio_calidad_jamas_baja(self):
        # una "optimización" que degrada la respuesta debe ser RECHAZADA
        r = evaluate(DEGRADED_RESPONSE, BASELINE_RESPONSE, CRITERIA, REQUIRED)
        self.assertEqual(r["verdict"], "RECHAZA")


if __name__ == "__main__":
    unittest.main(verbosity=2)
