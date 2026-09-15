"""semantic_cache.py — cache de contexto por hash de contenido normalizado.

Ahorro sin pérdida de calidad: una entrada solo se sirve si el hash del
contenido normalizado coincide EXACTAMENTE (jamás por semejanza aproximada,
que podría degradar la respuesta). La evicción protege las entradas de ALTA
reutilización (score = reusos * peso - edad) para no degradar el hit-rate.
"""
from __future__ import annotations

import hashlib
import re
import time
from typing import Any


def normalize(content: str) -> str:
    """Normalización determinista: whitespace colapsado + minúsculas."""
    return re.sub(r"\s+", " ", content.strip().lower())


def content_key(content: str) -> str:
    return hashlib.sha256(normalize(content).encode("utf-8")).hexdigest()


class SemanticCache:
    def __init__(self, max_entries: int = 1000, ttl_s: float = 3600.0,
                 reuse_weight: float = 2.0):
        self.max_entries = max_entries
        self.ttl_s = ttl_s
        self.reuse_weight = reuse_weight
        self._store: dict[str, dict[str, Any]] = {}
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def get(self, content: str) -> dict | None:
        key = content_key(content)
        entry = self._store.get(key)
        if entry is None:
            self.misses += 1
            return None
        if time.monotonic() - entry["created"] > self.ttl_s:
            del self._store[key]
            self.misses += 1
            return None
        entry["reuses"] += 1
        entry["last_used"] = time.monotonic()
        self.hits += 1
        return {"key": key, "response": entry["response"],
                "tokens_saved": entry["tokens_saved"]}

    def put(self, content: str, response: Any, tokens_saved: int) -> str:
        key = content_key(content)
        if key in self._store:
            return key  # idempotente: no pisa una entrada existente
        if len(self._store) >= self.max_entries:
            self._evict_one()
        now = time.monotonic()
        self._store[key] = {"response": response, "tokens_saved": tokens_saved,
                            "created": now, "last_used": now, "reuses": 0}
        return key

    # -- evicción ----------------------------------------------------------
    def _score(self, entry: dict) -> float:
        age = time.monotonic() - entry["created"]
        return entry["reuses"] * self.reuse_weight - age / self.ttl_s

    def _evict_one(self) -> None:
        if not self._store:
            return
        victim = min(self._store, key=lambda k: self._score(self._store[k]))
        del self._store[victim]
        self.evictions += 1

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {
            "entries": len(self._store),
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 6) if total else 0.0,
            "tokens_saved": sum(e["tokens_saved"] for e in self._store.values()),
            "evictions": self.evictions,
        }
