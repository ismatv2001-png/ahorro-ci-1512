#!/usr/bin/env python3
"""operate_ahorro.py — puesta en operación del núcleo de ahorro+calidad.

Ejecuta el pipeline completo (cache semántico + router + presupuesto +
métrica + gate de calidad) en escenarios de carga realista y firma la
evidencia con timestamp UTC real del sistema. Sin red, determinista.
Salida: EVIDENCIA-OPERACION-20260915.json + .sha256 en este directorio.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from savings_pipeline import run_pipeline, DEGRADED_RESPONSE, BASELINE_RESPONSE, CRITERIA, REQUIRED
from quality_gate import evaluate

HERE = Path(__file__).resolve().parent


def main() -> int:
    now = datetime.now(timezone.utc)
    generated_utc = now.isoformat(timespec="seconds").replace("+00:00", "Z")

    scenarios = {
        "escenario_canonico_95pct": run_pipeline(n_units=100, repeat_pct=95.0),
        "escenario_carga_90pct_200u": run_pipeline(n_units=200, repeat_pct=90.0),
    }
    # Gate de calidad con respuesta degradada: debe RECHAZAR (calidad jamás baja).
    degraded = evaluate(DEGRADED_RESPONSE, BASELINE_RESPONSE, CRITERIA, REQUIRED)
    pristine = evaluate(BASELINE_RESPONSE, BASELINE_RESPONSE, CRITERIA, REQUIRED)

    checks = []
    s = scenarios["escenario_canonico_95pct"]
    checks.append({"check": "ahorro>=95 en escenario canonico",
                   "ok": s["savings_pct"] >= 95.0, "valor": s["savings_pct"]})
    checks.append({"check": "gate calidad 100/100 en escenario canonico",
                   "ok": s["quality_gate"]["pasadas"] == s["quality_gate"]["evaluated"],
                   "valor": s["quality_gate"]})
    checks.append({"check": "respuesta degradada RECHAZADA",
                   "ok": degraded["verdict"] == "RECHAZA", "valor": degraded})
    checks.append({"check": "respuesta fiel PASA",
                   "ok": pristine["verdict"] == "PASA", "valor": pristine})
    s2 = scenarios["escenario_carga_90pct_200u"]
    checks.append({"check": "escenario 90pct mantiene gate 100/100",
                   "ok": s2["quality_gate"]["pasadas"] == s2["quality_gate"]["evaluated"],
                   "valor": s2["quality_gate"]})

    evidence = {
        "generated_utc": generated_utc,
        "scenarios": scenarios,
        "gate_individual": {"degradada": degraded, "fiel": pristine},
        "checks": checks,
        "all_checks_ok": all(c["ok"] for c in checks),
    }
    out = HERE / "EVIDENCIA-OPERACION-20260915.json"
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    (HERE / "EVIDENCIA-OPERACION-20260915.sha256").write_text(
        f"{digest}  {out.name}\n", encoding="utf-8")
    print(json.dumps({"generated_utc": generated_utc, "sha256": digest,
                      "all_checks_ok": evidence["all_checks_ok"],
                      "canonico": {"savings_pct": s["savings_pct"],
                                   "cache": s["served_from_cache"],
                                   "llamadas": s["provider_calls"],
                                   "gate": s["quality_gate"]}},
                     ensure_ascii=False, indent=2))
    return 0 if evidence["all_checks_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
