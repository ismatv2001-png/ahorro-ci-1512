#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests unitarios de cost_meter.py (stdlib puro, unittest).

Cubren: parseo robusto (líneas rotas ignoradas con conteo), coste exacto
con precios conocidos, agrupación por unidad, proyección de ahorro
matemáticamente correcta y la garantía de que el módulo NO vuelca
tokens/prompts/claves.
"""

import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cost_meter as cm

UTC = datetime.timezone.utc

# --- helpers ---------------------------------------------------------------

def _linea_harness_uso(ts_ms, input_tokens, output_tokens, cache_hit=0,
                       cache_write=0, reasoning=0, modelo=None, unidad=None):
    d = {"type": "assistant/message", "time": ts_ms,
         "data": {"usage": {"inputTokens": input_tokens,
                            "outputTokens": output_tokens,
                            "cacheReadTokens": cache_hit,
                            "cacheWriteTokens": cache_write,
                            "reasoningTokens": reasoning}}}
    if modelo is not None:
        d["data"]["model"] = modelo
    return json.dumps(d)

def _evento(modelo="deepseek-v4-pro", ts_ms=1789293660000, inp=1_000_000,
            hit=0, out=1_000_000, unidad="u1"):
    return cm.EventoUso(modelo, ts_ms, input_tokens=inp,
                        cache_hit_tokens=hit, output_tokens=out,
                        unidad=unidad, origen_modelo="explicito")

class TestParseo(unittest.TestCase):
    def test_parse_harness_assistant_message_completo(self):
        ln = _linea_harness_uso(1789293660000, 100, 50, cache_hit=20,
                                cache_write=5, reasoning=30)
        ev, accion, flags = cm.parsear_linea_uso(
            ln, modelo_actual="deepseek-v4-pro", unidad="s1")
        self.assertEqual(accion, "uso")
        self.assertEqual(ev.input_tokens, 100)
        self.assertEqual(ev.output_tokens, 50)
        self.assertEqual(ev.cache_hit_tokens, 20)
        self.assertEqual(ev.cache_write_tokens, 5)
        self.assertEqual(ev.reasoning_tokens, 30)
        self.assertEqual(ev.modelo, "deepseek-v4-pro")
        self.assertEqual(ev.ts_ms, 1789293660000)
        self.assertEqual(ev.unidad, "s1")

    def test_parse_request_context_actualiza_modelo(self):
        ln = json.dumps({"type": "request/context", "time": 1,
                         "data": {"provider": "deepseek-official",
                                  "model": "deepseek-v4-pro",
                                  "contextWindow": 1000000}})
        res, accion, _ = cm.parsear_linea_uso(ln)
        self.assertEqual(accion, "contexto")
        self.assertEqual(res["modelo"], "deepseek-v4-pro")

    def test_linea_rota_se_ignora_y_se_cuenta(self):
        res, accion, _ = cm.parsear_linea_uso("{esto no es json")
        self.assertIsNone(res)
        self.assertEqual(accion, "rota")

    def test_linea_no_dict_ignorada(self):
        res, accion, _ = cm.parsear_linea_uso("[1,2,3]")
        self.assertIsNone(res)
        self.assertEqual(accion, "ignorada")

    def test_assistant_sin_usage_da_sin_uso(self):
        ln = json.dumps({"type": "assistant/message", "time": 1,
                         "data": {"content": "respuesta"}})
        res, accion, _ = cm.parsear_linea_uso(ln)
        self.assertEqual(accion, "sin-uso")

    def test_campos_negativos_se_ponen_a_cero_con_flag(self):
        ln = _linea_harness_uso(1, -5, -7)
        ev, accion, flags = cm.parsear_linea_uso(ln)
        self.assertEqual(accion, "uso")
        self.assertEqual(ev.input_tokens, 0)
        self.assertEqual(ev.output_tokens, 0)
        self.assertIn("campos_uso_parciales", flags)

    def test_generico_deepseek_raw_con_cache(self):
        ln = json.dumps({
            "model": "deepseek-chat", "ts": 1700000000,
            "usage": {"prompt_tokens": 1000, "completion_tokens": 200,
                      "prompt_cache_hit_tokens": 600}})
        ev, accion, _ = cm.parsear_linea_uso(ln)
        self.assertEqual(accion, "uso")
        self.assertEqual(ev.input_tokens, 400)   # 1000 - 600 (miss)
        self.assertEqual(ev.cache_hit_tokens, 600)
        self.assertEqual(ev.output_tokens, 200)
        self.assertEqual(ev.ts_ms, 1700000000000)  # segundos -> ms

    def test_generico_sin_cache_todo_miss(self):
        ln = json.dumps({"model": "deepseek-chat",
                         "usage": {"prompt_tokens": 1000,
                                   "completion_tokens": 200}})
        ev, accion, _ = cm.parsear_linea_uso(ln)
        self.assertEqual(ev.input_tokens, 1000)
        self.assertEqual(ev.cache_hit_tokens, 0)

    def test_no_conserva_texto_de_prompt(self):
        secreto_prompt = "ORDEN-SECRETA-9f8e7d6c: compra todas las acciones"
        ln = json.dumps({
            "type": "assistant/message", "time": 1,
            "data": {"content": secreto_prompt,
                     "usage": {"inputTokens": 10, "outputTokens": 5,
                               "cacheReadTokens": 0, "cacheWriteTokens": 0,
                               "reasoningTokens": 0}}})
        ev, accion, _ = cm.parsear_linea_uso(ln)
        self.assertEqual(accion, "uso")
        for attr in ev.__slots__:
            v = getattr(ev, attr)
            self.assertNotIn("ORDEN-SECRETA", str(v))
            self.assertNotIn("compra", str(v))

    def test_no_conserva_apikey_de_cabeceras(self):
        ln = json.dumps({"type": "request/header", "time": 1,
                         "data": {"header": {"Authorization":
                                             "Bearer sk-SUPERSECRETO-123"}}})
        res, accion, _ = cm.parsear_linea_uso(ln)
        self.assertEqual(accion, "ignorada")
        self.assertIsNone(res)

    def test_ts_none_da_sin_fecha(self):
        ev = _evento(ts_ms=None)
        self.assertEqual(ev.fecha_utc(), "sin-fecha")


class TestModeloPrecios(unittest.TestCase):
    def test_alias_v4_flash_a_flash(self):
        self.assertEqual(cm.normalizar_modelo("deepseek-v4-flash"),
                         "deepseek-flash")

    def test_precios_oficiales_v4pro(self):
        p = cm.PRECIOS_OFICIALES["deepseek-v4-pro"]
        self.assertEqual(p["input_miss"]["pico"], 1.32)
        self.assertEqual(p["input_miss"]["valle"], 0.66)
        self.assertEqual(p["cache_hit"]["pico"], 0.044)
        self.assertEqual(p["output"]["valle"], 1.98)
        self.assertTrue(p["confirmado"])

    def test_precios_legacy_no_confirmados(self):
        self.assertFalse(cm.PRECIOS_OFICIALES["deepseek-chat"]["confirmado"])
        self.assertFalse(cm.PRECIOS_OFICIALES["deepseek-reasoner"]["confirmado"])
        self.assertIn("PRECIO_A_CONFIRMAR",
                      cm.PRECIOS_OFICIALES["deepseek-chat"]["origen"])

    def test_hora_pico_lunes_2am_utc(self):
        ts = datetime.datetime(2026, 9, 14, 2, 0, tzinfo=UTC).timestamp() * 1000
        self.assertTrue(cm.es_hora_pico(int(ts)))

    def test_hora_valle_finde(self):
        ts = datetime.datetime(2026, 9, 12, 2, 0, tzinfo=UTC).timestamp() * 1000
        self.assertFalse(cm.es_hora_pico(int(ts)))

    def test_modelo_desconocido_flag_sin_precio(self):
        pre, flags = cm.precio_evento(cm.PRECIOS_OFICIALES,
                                      "modelo-que-no-existe", 1)
        self.assertIsNone(pre)
        self.assertIn("modelo_sin_precio", flags)

    def test_modelo_legacy_flag_precio_a_confirmar(self):
        pre, flags = cm.precio_evento(cm.PRECIOS_OFICIALES,
                                      "deepseek-chat", 1)
        self.assertIsNotNone(pre)
        self.assertIn(cm.PRECIO_A_CONFIRMAR, flags)


class TestCoste(unittest.TestCase):
    def test_coste_exacto_v4pro_pico_1m_1m(self):
        # 1M miss pico (1.32) + 1M output pico (3.96) = 5.28 USD
        ev = _evento(inp=1_000_000, out=1_000_000,
                     ts_ms=int(datetime.datetime(
                         2026, 9, 14, 2, 0, tzinfo=UTC).timestamp() * 1000))
        coste, det, flags = cm.coste_evento(ev, cm.PRECIOS_OFICIALES)
        self.assertAlmostEqual(coste, 5.28, places=6)
        self.assertTrue(det["pico"])

    def test_coste_cache_hit_a_precio_hit(self):
        # 1M hit pico = 0.044 USD (v4-pro)
        ev = _evento(inp=0, hit=1_000_000, out=0,
                     ts_ms=int(datetime.datetime(
                         2026, 9, 14, 2, 0, tzinfo=UTC).timestamp() * 1000))
        coste, det, _ = cm.coste_evento(ev, cm.PRECIOS_OFICIALES)
        self.assertAlmostEqual(coste, 0.044, places=6)
        self.assertAlmostEqual(det["cache_hit_usd"], 0.044, places=6)

    def test_coste_valle_mitad(self):
        ts = int(datetime.datetime(2026, 9, 14, 12, 0,
                                   tzinfo=UTC).timestamp() * 1000)
        ev = _evento(inp=1_000_000, out=0, ts_ms=ts)
        coste, det, _ = cm.coste_evento(ev, cm.PRECIOS_OFICIALES)
        self.assertAlmostEqual(coste, 0.66, places=6)  # valle miss
        self.assertFalse(det["pico"])

    def test_reasoning_no_se_factura_dos_veces(self):
        # outputTokens ya incluye razonamiento: reasoning no suma nada.
        precios = {"deepseek-v4-pro": {"input_miss": 1.0, "cache_hit": 0.5,
                                       "output": 2.0, "confirmado": True,
                                       "origen": "test"}}
        ev = cm.EventoUso("deepseek-v4-pro", 1, input_tokens=0,
                          output_tokens=1000, reasoning_tokens=500)
        coste, _, _ = cm.coste_evento(ev, precios)
        self.assertAlmostEqual(coste, 1000 / 1e6 * 2.0, places=9)

    def test_modelo_sin_precio_coste_none(self):
        ev = _evento(modelo="desconocido-x")
        coste, det, flags = cm.coste_evento(ev, cm.PRECIOS_OFICIALES)
        self.assertIsNone(coste)
        self.assertIsNone(det)
        self.assertIn("modelo_sin_precio", flags)


class TestSesionLectura(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="costmeter-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _escribir_sesion(self, nombre, lineas):
        p = os.path.join(self.tmp, nombre)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lineas) + "\n")
        return p

    def test_leer_fichero_asocia_modelo_y_unidad(self):
        lineas = [
            json.dumps({"type": "session", "id": "session-abc-123", "time": 0}),
            json.dumps({"type": "request/context", "time": 1,
                        "data": {"model": "deepseek-v4-pro"}}),
            _linea_harness_uso(2, 100, 50),
            "LINEA ROTA {",
        ]
        p = self._escribir_sesion("s.jsonl", lineas)
        evs, stats = cm.leer_fichero_sesion(p)
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].modelo, "deepseek-v4-pro")
        self.assertEqual(evs[0].unidad, "session-abc-123")
        self.assertEqual(stats["rotas"], 1)
        self.assertEqual(stats["lineas"], 4)

    def test_leer_fichero_zstd_roundtrip(self):
        lineas = [json.dumps({"type": "request/context", "time": 1,
                              "data": {"model": "deepseek-v4-pro"}}),
                  _linea_harness_uso(2, 10, 5)]
        plano = self._escribir_sesion("s.jsonl", lineas)
        zst = plano + ".zstd"
        proc = subprocess.run(["zstd", "-q", "-f", plano, "-o", zst])
        if proc.returncode != 0:
            self.skipTest("zstd CLI no disponible")
        evs, stats = cm.leer_fichero_sesion(zst)
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].input_tokens, 10)
        self.assertTrue(stats["zstd"])

    def test_unidad_uuid_conservada(self):
        p = "/x/sesiones/09f2d136-f5f1-47c3-90b4-8fb0ffec0d9a/session.jsonl"
        self.assertEqual(cm.unidad_desde_path(p),
                         "09f2d136-f5f1-47c3-90b4-8fb0ffec0d9a")

    def test_unidad_no_uuid_hasheada_sin_ruta(self):
        p = "/datos/privados/cliente-importante/session.jsonl"
        u = cm.unidad_desde_path(p)
        self.assertTrue(u.startswith("u-"))
        self.assertNotIn("cliente-importante", u)
        self.assertEqual(len(u), 2 + 16)

    def test_deduplicacion_entre_ficheros(self):
        p1 = self._escribir_sesion(
            "a/s1.jsonl", [json.dumps({"type": "session", "id": "s-x"}),
                           _linea_harness_uso(1, 10, 5)])
        p2 = self._escribir_sesion(
            "b/s2.jsonl", [json.dumps({"type": "session", "id": "s-x"}),
                           _linea_harness_uso(2, 20, 10)])
        evs, stats = cm.leer_rutas([p1, p2], deduplicar_unidades=True)
        self.assertEqual(len(evs), 1)  # solo la primera copia
        self.assertEqual(stats["eventos_duplicados_descartados"], 1)
        self.assertEqual(evs[0].input_tokens, 10)


class TestAgregacion(unittest.TestCase):
    def test_totales_y_usd_por_token(self):
        precios = {"deepseek-v4-pro": {"input_miss": 1.0, "cache_hit": 0.5,
                                       "output": 2.0, "confirmado": True,
                                       "origen": "test"}}
        evs = [_evento(inp=1_000_000, out=1_000_000),   # 1 + 2 = 3 USD
               _evento(inp=500_000, out=500_000)]        # 0.5 + 1 = 1.5
        rep = cm.agregar(evs, precios)
        t = rep["totales"]
        self.assertEqual(t["llamadas"], 2)
        self.assertEqual(t["tokens_facturados"], 3_000_000)
        self.assertAlmostEqual(t["coste_usd"], 4.5, places=6)
        self.assertAlmostEqual(t["usd_por_token"], 4.5 / 3_000_000, places=12)
        self.assertAlmostEqual(t["usd_por_llamada"], 2.25, places=6)

    def test_por_dia_utc(self):
        precios = {"deepseek-v4-pro": {"input_miss": 1.0, "cache_hit": 0.5,
                                       "output": 2.0, "confirmado": True,
                                       "origen": "test"}}
        d1 = datetime.datetime(2026, 9, 13, 12, 0, tzinfo=UTC).timestamp() * 1000
        d2 = datetime.datetime(2026, 9, 14, 12, 0, tzinfo=UTC).timestamp() * 1000
        evs = [_evento(ts_ms=int(d1)), _evento(ts_ms=int(d2))]
        rep = cm.agregar(evs, precios)
        self.assertEqual(set(rep["por_dia"].keys()),
                         {"2026-09-13", "2026-09-14"})
        self.assertEqual(rep["por_dia"]["2026-09-13"]["llamadas"], 1)

    def test_por_modelo_y_unidad(self):
        precios = {"deepseek-v4-pro": {"input_miss": 1.0, "cache_hit": 0.5,
                                       "output": 2.0, "confirmado": True,
                                       "origen": "test"}}
        evs = [_evento(unidad="u-a"), _evento(unidad="u-b"),
               _evento(unidad="u-a")]
        rep = cm.agregar(evs, precios)
        self.assertEqual(rep["por_modelo"]["deepseek-v4-pro"]["llamadas"], 3)
        self.assertEqual(rep["por_unidad"]["n_unidades"], 2)

    def test_sin_precio_excluido_del_coste(self):
        precios = {"deepseek-v4-pro": {"input_miss": 1.0, "cache_hit": 0.5,
                                       "output": 2.0, "confirmado": True,
                                       "origen": "test"}}
        evs = [_evento(), _evento(modelo="sin-precio-xyz")]
        rep = cm.agregar(evs, precios)
        self.assertEqual(rep["totales"]["llamadas_sin_precio"], 1)
        self.assertAlmostEqual(rep["totales"]["coste_usd"], 3.0, places=6)
        # usd_por_token solo sobre tokens con precio
        self.assertAlmostEqual(rep["totales"]["usd_por_token"],
                               3.0 / 2_000_000, places=12)


class TestAhorro(unittest.TestCase):
    PRECIOS = {"deepseek-v4-pro": {"input_miss": 1.0, "cache_hit": 0.1,
                                   "output": 2.0, "confirmado": True,
                                   "origen": "test"}}

    def test_ahorro_cache_realizado_formula(self):
        # 1M hit -> 1M/1M * (1.0 - 0.1) = 0.9 USD ahorrados vs sin caché
        evs = [_evento(inp=0, hit=1_000_000, out=0)]
        rep = cm.ahorro_cache_realizado(evs, self.PRECIOS)
        self.assertAlmostEqual(rep["ahorro_cache_realizado_usd"], 0.9,
                               places=9)

    def test_proyeccion_cache_matematica(self):
        # evento: 1M miss, 0 hit. hit_rate=0.9 -> extra=0.9M -> 0.9*0.9=0.81
        evs = [_evento(inp=1_000_000, hit=0, out=0)]
        proy = cm.proyeccion_ahorro(evs, self.PRECIOS,
                                    hit_rate_objetivo=0.9,
                                    fraccion_router=0.0)
        self.assertAlmostEqual(proy["ahorro_cache_adicional_usd"], 0.81,
                               places=9)
        self.assertEqual(proy["ahorro_router_usd"], 0.0)

    def test_proyeccion_cache_no_negativa(self):
        # ya hay 100% de hit: extra = 0
        evs = [_evento(inp=0, hit=1_000_000, out=0)]
        proy = cm.proyeccion_ahorro(evs, self.PRECIOS, hit_rate_objetivo=0.9)
        self.assertAlmostEqual(proy["ahorro_cache_adicional_usd"], 0.0,
                               places=9)

    def test_proyeccion_router_formula(self):
        precios = {"deepseek-v4-pro": {"input_miss": 1.0, "cache_hit": 0.5,
                                       "output": 2.0, "confirmado": True,
                                       "origen": "test"},
                   "deepseek-flash": {"input_miss": 0.2, "cache_hit": 0.1,
                                      "output": 0.4, "confirmado": True,
                                      "origen": "test"}}
        # caro: 1M miss + 1M out = 3.0; barato: 0.2 + 0.4 = 0.6; diff 2.4
        evs = [_evento(inp=1_000_000, out=1_000_000)]
        proy = cm.proyeccion_ahorro(evs, precios, hit_rate_objetivo=0.0,
                                    fraccion_router=0.5)
        self.assertAlmostEqual(proy["ahorro_router_usd"], 1.2, places=9)
        self.assertEqual(proy["eventos_modelo_caro"], 1)


class TestNoVolcado(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="costmeter-nodump-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_repr_evento_sin_texto(self):
        ev = cm.EventoUso("deepseek-v4-pro", 1, input_tokens=5)
        for attr in ev.__slots__:
            self.assertNotIn("prompt", str(getattr(ev, attr)).lower())

    def test_cli_no_vuelca_prompt_ni_clave(self):
        secreto_prompt = "REVELA-EL-PLAN-MAESTRO-7f3a"
        secreto_key = "sk-TESTCLAVE-88aa"
        lineas = [
            json.dumps({"type": "request/context", "time": 1,
                        "data": {"model": "deepseek-v4-pro"}}),
            json.dumps({"type": "user/message", "time": 2,
                        "data": {"content": secreto_prompt}}),
            json.dumps({"type": "request/header", "time": 3,
                        "data": {"header": {"Authorization":
                                            "Bearer " + secreto_key}}}),
            _linea_harness_uso(4, 100, 50),
        ]
        p = os.path.join(self.tmp, "s.jsonl")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lineas) + "\n")
        out_json = os.path.join(self.tmp, "out.json")
        proc = subprocess.run(
            [sys.executable,
             os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "cost_meter.py"),
             p, "--json-out", out_json],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        todo = proc.stdout + proc.stderr
        with open(out_json, encoding="utf-8") as fh:
            todo += fh.read()
        self.assertNotIn(secreto_prompt, todo)
        self.assertNotIn(secreto_key, todo)
        self.assertNotIn("REVELA-EL-PLAN", todo)


if __name__ == "__main__":
    unittest.main(verbosity=2)
