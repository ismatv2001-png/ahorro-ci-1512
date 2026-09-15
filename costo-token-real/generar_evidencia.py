#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""generar_evidencia.py — escenarios de ahorro y EVIDENCIA final.

Consume los eventos reales con cost_meter.py (import local), calcula:
  * agregados reales (totales, por modelo, por día, por unidad),
  * ahorro de caché YA realizado,
  * proyecciones con varios escenarios explícitos (hit_rate, router),
  * sha256 de los artefactos del carril.
Escribe EVIDENCIA-COSTO-TOKEN.json. NUNCA imprime textos.
"""

import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cost_meter as cm

FUENTES = [
    "/home/codespace/complete-1512/sessions",
    "/home/codespace/.dsh-harness-1512-complete/sessions",
    "/home/codespace/.dsh-harness-1512-recent72/sessions",
    "/home/codespace/.dsh-harness-1512/sessions",
]

ESCENARIOS = [
    {"nombre": "cache_90", "hit_rate_objetivo": 0.90, "fraccion_router": 0.0},
    {"nombre": "cache_95", "hit_rate_objetivo": 0.95, "fraccion_router": 0.0},
    {"nombre": "cache_95_router_10", "hit_rate_objetivo": 0.95,
     "fraccion_router": 0.10},
    {"nombre": "cache_95_router_25", "hit_rate_objetivo": 0.95,
     "fraccion_router": 0.25},
]

def sha256_de(ruta):
    h = hashlib.sha256()
    with open(ruta, "rb") as fh:
        for bloque in iter(lambda: fh.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()

def redondear(o, nd=9):
    if isinstance(o, float):
        return round(o, nd)
    if isinstance(o, dict):
        return {k: redondear(v, nd) for k, v in o.items()}
    if isinstance(o, list):
        return [redondear(v, nd) for v in o]
    return o

def main():
    eventos, stats = cm.leer_rutas(FUENTES)
    precios = cm.cargar_precios()
    agregados = cm.agregar(eventos, precios)
    ahorro = cm.ahorro_cache_realizado(eventos, precios)
    escenarios = {}
    for e in ESCENARIOS:
        escenarios[e["nombre"]] = redondear(cm.proyeccion_ahorro(
            eventos, precios,
            hit_rate_objetivo=e["hit_rate_objetivo"],
            fraccion_router=e["fraccion_router"]))

    # ahorro relativo de la proyección vs coste actual facturado
    coste_actual = agregados["totales"]["coste_usd"]
    baseline_sin_cache = coste_actual + ahorro["ahorro_cache_realizado_usd"]
    pct_cache = (100.0 * ahorro["ahorro_cache_realizado_usd"] /
                 baseline_sin_cache) if baseline_sin_cache > 0 else None
    llamadas_cero = sum(1 for ev in eventos if ev.tokens_facturados == 0)
    rel = {}
    for nombre, esc in escenarios.items():
        extra = esc["ahorro_proyectado_total_usd"]
        rel[nombre] = {
            "ahorro_usd": extra,
            "pct_sobre_coste_actual": (
                round(100.0 * extra / coste_actual, 4)
                if coste_actual > 0 else None),
        }

    artefactos = {}
    for f in ("cost_meter.py", "test_cost_meter.py",
              "HALLAZGOS-DATOS-REALES.md", "informe-real-completo.json"):
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), f)
        if os.path.exists(p):
            artefactos[f] = {"sha256": sha256_de(p),
                             "bytes": os.path.getsize(p)}

    evidencia = {
        "carril": "ismatv1512-nuevos-trabajos",
        "pieza": "metrica-coste-por-token-con-uso-real",
        "datos_reales": True,
        "fuentes_datos_reales": FUENTES,
        "estadisticas_lectura": redondear(stats),
        "agregados": redondear(agregados),
        "ahorro_cache_ya_realizado_usd": redondear(
            ahorro["ahorro_cache_realizado_usd"]),
        "ahorro_cache_ya_realizado_por_modelo": redondear(
            ahorro["por_modelo"]),
        "linea_base_sin_cache_usd": redondear(baseline_sin_cache),
        "ahorro_cache_pct_sobre_linea_base": redondear(pct_cache, 4),
        "llamadas_con_usage_cero": llamadas_cero,
        "escenarios_proyeccion": escenarios,
        "ahorro_relativo_escenarios": rel,
        "precios_usados": precios,
        "sha256_artefactos": artefactos,
    }
    aqui = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(aqui, "EVIDENCIA-COSTO-TOKEN.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(evidencia, fh, indent=2, ensure_ascii=False)
    print("sha256 EVIDENCIA-COSTO-TOKEN.json =", sha256_de(out))
    print("escrito:", out)

if __name__ == "__main__":
    main()
