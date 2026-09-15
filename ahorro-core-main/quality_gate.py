"""quality_gate.py — GATE DE CALIDAD que ejecuta el principio del propietario.

"Calidad JAMÁS baja, solo se mantiene o mejora": cualquier optimización de
ahorro (caché, router, recorte de contexto, modelo más barato) DEBE pasar este
gate. Veredicto PASA solo si calidad_candidato >= calidad_baseline - epsilon
(epsilon por defecto 0.0, documentada). Un candidato PEOR es RECHAZADO con
números, nunca "pasa con calidad menor".
"""
from __future__ import annotations

import json
import re
from typing import Any

EPSILON_DEFAULT = 0.0  # tolerancia de calidad: 0 = jamás admitir degradación


def _norm_tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9áéíóúüñ]+", text.lower()))


def _ngrams(tokens: list[str], n: int) -> set[str]:
    return {"|".join(tokens[i:i + n]) for i in range(len(tokens) - n + 1)}


def _fidelity(reference: str, candidate: str) -> float:
    """Solapamiento normalizado de unigramas+bigramas (0-1, determinista).

    Usa el ORDEN ORIGINAL de tokens: una adición sobre la referencia intacta
    conserva todos los bigramas de la referencia (calidad no menor); reordenar
    o perder contenido sí reduce la fidelidad.
    """
    ref = re.findall(r"[a-z0-9áéíóúüñ]+", reference.lower())
    cand = re.findall(r"[a-z0-9áéíóúüñ]+", candidate.lower())
    if not ref:
        return 1.0 if not cand else 0.0
    r1, c1 = set(ref), set(cand)
    r2, c2 = _ngrams(ref, 2), _ngrams(cand, 2)
    uni = len(r1 & c1) / len(r1)
    bi = len(r2 & c2) / len(r2) if r2 else uni
    return round(0.6 * uni + 0.4 * bi, 6)


def _criteria_accuracy(criteria: list[str], reference: str,
                       candidate: str) -> float:
    if not criteria:
        return 1.0
    low = candidate.lower()
    hits = sum(1 for c in criteria if c.lower() in low)
    return round(hits / len(criteria), 6)


def score(candidate: str, reference: str, criteria: list[str] | None = None,
          required_parts: list[str] | None = None) -> dict:
    criteria = criteria or []
    required_parts = required_parts or []
    acc = _criteria_accuracy(criteria, reference, candidate)
    fid = _fidelity(reference, candidate)
    completeness = round(sum(1 for p in required_parts if p.lower()
                             in candidate.lower()) / len(required_parts), 6) \
        if required_parts else 1.0
    total = round(0.45 * acc + 0.35 * fid + 0.20 * completeness, 6)
    return {"accuracy": acc, "fidelity": fid, "completeness": completeness,
            "total": total}


def evaluate(candidate: str, reference: str,
             criteria: list[str] | None = None,
             required_parts: list[str] | None = None,
             epsilon: float = EPSILON_DEFAULT) -> dict:
    """Veredicto con números. PASA solo si no hay degradación (o es <= epsilon)."""
    cand = score(candidate, reference, criteria, required_parts)
    base = score(reference, reference, criteria, required_parts)
    delta = round(cand["total"] - base["total"], 6)
    verdict = "PASA" if delta >= -epsilon else "RECHAZA"
    return {"verdict": verdict, "candidate": cand, "baseline": base,
            "delta": delta, "epsilon": epsilon,
            "principle": "calidad_jamas_baja" if verdict == "PASA"
            else "degradacion_detectada",
            "evidence": f"candidato {cand['total']} vs baseline "
                        f"{base['total']} (delta {delta:+.6f}, "
                        f"epsilon {epsilon})"}


def gate_json(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, sort_keys=True)
