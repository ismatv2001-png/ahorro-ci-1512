#!/usr/bin/env python3
"""fuzz_semantic_cache_props.py — 12 propiedades fuzz del cache semántico.

Cada propiedad es un invariante con nombre y justificación de una línea.
Sin frameworks: asserts puros sobre entradas aleatorias (semilla fija).
Ejecutar: python3 fuzz_semantic_cache_props.py [iteraciones]
"""

import random
import string
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from semantic_cache import SemanticCache, content_key, normalize  # noqa: E402

ALPHA = string.ascii_letters + string.digits + " .,;:!?áéíóúñ"


def rand_prompt(rng: random.Random, max_len: int = 80) -> str:
    return "".join(rng.choice(ALPHA) for _ in range(rng.randint(1, max_len)))


def run_props(iterations: int = 300) -> list[tuple[str, bool, str]]:
    rng = random.Random(20260915)
    results: list[tuple[str, bool, str]] = []

    def check(name: str, why: str, fn) -> None:
        try:
            fn(rng)
            results.append((name, True, why))
        except AssertionError as e:
            results.append((name, False, f"VIOLADA: {e} ({why})"))
        except Exception as e:
            results.append((name, False, f"ERROR {type(e).__name__}: {e} ({why})"))

    # P1: normalización idempotente — normalizar dos veces no cambia nada.
    def p1(rng):
        for _ in range(iterations):
            s = rand_prompt(rng)
            assert normalize(normalize(s)) == normalize(s)

    check("p1-normalize-idempotente",
          "el hash debe ser estable bajo aplicación repetida de la normalización", p1)

    # P2: hash determinista — mismo contenido => misma clave siempre.
    def p2(rng):
        for _ in range(iterations):
            s = rand_prompt(rng)
            assert content_key(s) == content_key(s)

    check("p2-hash-determinista",
          "dos llamadas con el mismo contenido deben dar la misma clave", p2)

    # P3: hit exacto — tras put, get del MISMO contenido devuelve la respuesta.
    def p3(rng):
        c = SemanticCache(max_entries=16)
        for i in range(iterations):
            s = rand_prompt(rng)
            c.put(s, {"i": i}, tokens_saved=1)
            got = c.get(s)
            assert got is not None and got["response"] == {"i": i}

    check("p3-hit-exacto",
          "el contenido exacto debe servirse del cache tras guardarlo", p3)

    # P4: mutación de 1 carácter NO debe dar hit (jamás semejanza aproximada).
    # Se usan prompts largos y se descartan las iteraciones en que el mutado
    # coincide (normalizado) con otro prompt ya insertado: en ese caso el hit
    # sería de contenido EXACTO y legítimo, no una violación.
    def p4(rng):
        c = SemanticCache(max_entries=64)
        inserted: set[str] = set()
        for _ in range(iterations):
            s = "".join(rng.choice(ALPHA) for _ in range(32))
            c.put(s, "resp", tokens_saved=1)
            inserted.add(normalize(s))
            mid = 16
            mutated = s[:mid] + ("X" if s[mid] != "X" else "Y") + s[mid + 1:]
            if normalize(mutated) in inserted:
                continue  # coincidencia exacta legítima con otro prompt
            assert c.get(mutated) is None

    check("p4-mutacion-1-char-miss",
          "un prompt mutado no debe servirse: la semejanza aproximada degradaría calidad", p4)

    # P5: cache vacío => siempre miss.
    def p5(rng):
        c = SemanticCache(max_entries=16)
        for _ in range(iterations):
            assert c.get(rand_prompt(rng)) is None

    check("p5-cache-vacio-miss", "sin entradas no puede haber aciertos", p5)

    # P6: evicción LRU/puntuación — el número de entradas nunca excede max_entries.
    def p6(rng):
        c = SemanticCache(max_entries=8)
        for _ in range(iterations):
            c.put(rand_prompt(rng), "r", tokens_saved=1)
        assert len(c._store) <= 8
        assert c.stats()["entries"] <= 8

    check("p6-max-entries",
          "la ventana debe respetar el máximo para acotar memoria", p6)

    # P7: TTL — una entrada expirada no se sirve.
    def p7(rng):
        c = SemanticCache(max_entries=16, ttl_s=0.0)
        s = rand_prompt(rng, 20) or "a"
        c.put(s, "resp", tokens_saved=1)
        assert c.get(s) is None

    check("p7-ttl-expirado-miss", "lo caducado no debe servirse", p7)

    # P8: contadores — hits+misses coincide con llamadas get; put idempotente no duplica.
    def p8(rng):
        c = SemanticCache(max_entries=16)
        puts = gets = 0
        for _ in range(iterations):
            s = rand_prompt(rng, 20) or "a"
            c.put(s, "r", tokens_saved=1)
            puts += 1
        for _ in range(iterations):
            c.get(rand_prompt(rng, 20) or "a")
            gets += 1
        assert c.stats()["hits"] + c.stats()["misses"] == gets

    check("p8-contadores-coherentes",
          "la métrica debe poder auditarse: cada get cuenta una vez", p8)

    # P9: put idempotente — re-put del mismo contenido no pisa la respuesta.
    def p9(rng):
        c = SemanticCache(max_entries=16)
        s = rand_prompt(rng, 20) or "a"
        c.put(s, "primera", tokens_saved=1)
        c.put(s, "segunda", tokens_saved=99)
        assert c.get(s)["response"] == "primera"
        assert c.get(s)["tokens_saved"] == 1

    check("p9-put-idempotente",
          "la primera respuesta es la autoritativa; re-put no la degrada", p9)

    # P10: tokens_saved no negativo se propaga.
    def p10(rng):
        c = SemanticCache(max_entries=16)
        s = rand_prompt(rng, 20) or "a"
        c.put(s, "r", tokens_saved=7)
        assert c.get(s)["tokens_saved"] == 7

    check("p10-tokens-saved-propaga",
          "el ahorro registrado debe devolverse íntegro en el hit", p10)

    # P11: sin colisiones observables entre NORMALIZADOS distintos — la
    # normalización puede unir prompts distintos a propósito (espacios,
    # mayúsculas); la clave debe ser inyectiva sobre el espacio normalizado.
    def p11(rng):
        seen: set[str] = set()
        for _ in range(iterations):
            seen.add(normalize(rand_prompt(rng, 60)))
        keys = {content_key(s) for s in seen}
        assert len(keys) == len(seen)

    check("p11-sin-colisiones-observables",
          "SHA-256: normalizados distintos jamás comparten clave en la práctica", p11)

    # P12: stats coherentes tras evicción — entries == len(store).
    def p12(rng):
        c = SemanticCache(max_entries=4)
        for _ in range(iterations):
            c.put(rand_prompt(rng, 30), "r", tokens_saved=1)
        assert c.stats()["entries"] == len(c._store)

    check("p12-stats-entries-real",
          "el snapshot debe reflejar el estado real, sin inventar", p12)

    return results


def main() -> int:
    iterations = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    results = run_props(iterations)
    ok = sum(1 for _, passed, _ in results if passed)
    for name, passed, why in results:
        print(f"{'PASS' if passed else 'FAIL'}  {name}: {why}")
    print(f"\n{ok}/{len(results)} propiedades PASAN ({iterations} iteraciones c/u)")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
