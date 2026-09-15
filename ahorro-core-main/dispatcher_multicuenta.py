#!/usr/bin/env python3
"""dispatcher_multicuenta.py — Dispatcher/cola de agentes con 4 cuentas DeepSeek.

Encadena las piezas del software de ahorro:
  semantic_cache  -> sirve el hit SIN reserva ni red (0 coste)
  budget_weekly   -> try_reserve fail-closed ANTES de cualquier POST
  account_pool    -> cuenta round-robin por presupuesto; 402 marca sin saldo
  secrets_loader  -> resuelve la clave SOLO en memoria (jamás impresa)
  transporte      -> POST único; 402 => UN solo salto de cuenta (regla 2026-09-13)

Garantías (fixes review round2, 2026-09-15):
  D1 plan validado (max_tokens>=1, rate finito>0) ANTES de cualquier efecto;
  D2 sin cuenta disponible => release_reserve REAL (sin fuga de presupuesto);
  D3 transporte: OSError=incierto (sin reintento); otros errores=local
     definitivo (jamás atasca in_flight ni la reserva);
  D4 idempotencia por opId (doble submit no duplica el cargo);
  D5 release EXACTAMENTE una vez por intento (402 sin respaldo incluido);
  D6 clave de cache ligada a model+max_tokens+reasoning_effort (sin hit
     cross-model).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from typing import Any, Callable, Optional

from account_pool import AccountPool
from budget_weekly import WeeklyBudget


def canonical_json(value: Any) -> str:
    """JSON canónico (claves ordenadas, sin espacios) para hashes de petición."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def sha256_text(value: str) -> str:
    """Hex sha256 de un texto UTF-8."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def utc_now_iso() -> str:
    """Timestamp UTC ISO-8601 con sufijo Z."""
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


class DispatcherError(Exception):
    """Error del dispatcher con código, mensaje y flag retryable."""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class MultiAccountDispatcher:
    """Dispatcher de cola: cache -> presupuesto -> cuenta -> POST -> settle."""

    def __init__(
        self,
        pool: AccountPool,
        budget: WeeklyBudget,
        resolver: Callable[[str], Optional[str]],
        transport: Any,
        *,
        cache: Any = None,
        endpoint: str = "https://api.deepseek.com/chat/completions",
        model: str = "deepseek-v4-pro",
        reasoning_effort: str = "max",
        timeout_s: float = 600.0,
    ) -> None:
        self.pool = pool
        self.budget = budget
        self.resolver = resolver
        self.transport = transport
        self.cache = cache
        self.endpoint = endpoint
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.timeout_s = timeout_s
        self._receipts: dict[str, dict] = {}
        self._inflight: set[str] = set()

    # --- cola ---------------------------------------------------------------
    def submit(self, unit_id: str, messages: list[dict], plan: dict) -> dict:
        """Un trabajo de la cola: cache -> reserva -> cuenta -> POST -> settle."""
        self._require_messages(messages)
        input_estimate, max_tokens, rate = self._estimate(messages, plan)

        # 1) Cache: hit => 0 coste, sin reserva, sin red.
        hit = self._cache_lookup(messages, max_tokens, unit_id)
        if hit is not None:
            return hit

        # 2) Presupuesto semanal fail-closed ANTES de cualquier efecto.
        self._reserve_or_raise(unit_id, input_estimate, max_tokens, rate)
        body = self._build_body(messages, max_tokens)
        op_id = sha256_text(canonical_json(body))

        # D4: idempotencia — el mismo opId jamás se ejecuta dos veces.
        if op_id in self._receipts:
            return {**self._receipts[op_id], "reusedReceipt": True}
        if op_id in self._inflight:
            raise DispatcherError("op-in-flight",
                                  f"operation {op_id} already in flight")
        self._inflight.add(op_id)
        try:
            # 3) Cuenta del pool + clave EN MEMORIA.
            account = self.pool.pick()
            if account is None:
                self._refund_reserve(unit_id, input_estimate, max_tokens, rate)
                raise DispatcherError(
                    "no-account",
                    "no account with budget/concurrency available")

            account, attempt, failover_used, failed_account = \
                self._post_with_failover(account, body)
            self._release_attempt(account, attempt)
            if failed_account is not None:
                # La cuenta original (402) se libera UNA vez, tras el salto.
                self.pool.release(failed_account, ok=False, status=402)

            # 4) Liquidación del presupuesto con el coste REAL.
            self.budget.settle(unit_id, attempt.get("cost", 0.0))

            if attempt.get("uncertain"):
                raise DispatcherError("uncertain-outcome",
                                      "POST outcome uncertain; no retry", False)
            if not attempt.get("ok"):
                raise DispatcherError(
                    "provider-rejected",
                    f"provider rejected (status={attempt.get('statusCode')}, "
                    f"account={account}, failoverUsed={bool(failover_used)})")
            usage = attempt.get("usage") or {}
            if not usage:
                raise DispatcherError(
                    "missing-usage",
                    "response ok without usage; cannot compute cost")

            # 5) Poblar el cache con la respuesta REAL (ahorro siguiente).
            tokens_saved = sum(int(usage.get(k, 0)) for k in
                               ("prompt_tokens", "completion_tokens"))
            if self.cache is not None:
                self.cache.put(self._cache_content(messages, max_tokens),
                               attempt.get("content"), tokens_saved)

            receipt = {"status": "ok", "opId": op_id, "unit": unit_id,
                       "account": account, "failoverUsed": failover_used,
                       "usage": usage, "cost": attempt.get("cost"),
                       "postedAt": utc_now_iso()}
            self._receipts[op_id] = receipt
            return receipt
        finally:
            self._inflight.discard(op_id)

    def _require_messages(self, messages: list[dict]) -> None:
        """Rechaza payloads sin una lista de mensajes no vacía."""
        if not isinstance(messages, list) or not messages:
            raise DispatcherError("invalid-payload", "messages required")

    def _estimate(self, messages: list[dict],
                  plan: dict) -> tuple[int, int, float]:
        """Estima (input_tokens, max_tokens, rate) del plan para la reserva.
        D1: plan sin rate o con max_tokens inválido se rechaza ANTES de
        cualquier efecto (fail-closed)."""
        try:
            max_tokens = int(plan.get("max_tokens", 0))
            rate = float(plan.get("rate_usd_per_1m", 0.0))
        except (TypeError, ValueError):
            raise DispatcherError("invalid-plan",
                                  "max_tokens and rate_usd_per_1m must be numeric")
        if max_tokens < 1:
            raise DispatcherError("invalid-plan", "max_tokens must be >= 1")
        if not math.isfinite(rate) or rate <= 0:
            raise DispatcherError("invalid-plan",
                                  "rate_usd_per_1m must be finite and > 0")
        est_chars = sum(len(str(m.get("content") or "")) for m in messages
                        if isinstance(m, dict))
        return max(1, est_chars // 3), max_tokens, rate

    def _cache_content(self, messages: list[dict], max_tokens: int) -> str:
        """Contenido clave del cache: D6 — incluye model, max_tokens y
        reasoning_effort para que un hit jamás cruce modelos/parámetros."""
        return canonical_json({"model": self.model,
                               "reasoning_effort": self.reasoning_effort,
                               "max_tokens": max_tokens,
                               "messages": messages})

    def _cache_lookup(self, messages: list[dict], max_tokens: int,
                      unit_id: str) -> Optional[dict]:
        """Hit de cache: receipt sin reserva ni red (0 coste); None si no hay."""
        if self.cache is None:
            return None
        content = self._cache_content(messages, max_tokens)
        hit = self.cache.get(content)
        if hit is None:
            return None
        return {"status": "cache-hit", "unit": unit_id,
                "cacheKey": sha256_text(content),
                "tokensSaved": hit.get("tokens_saved"),
                "cost": 0.0, "postedAt": utc_now_iso()}

    def _reserve_or_raise(self, unit_id: str, input_estimate: int,
                          max_tokens: int, rate: float) -> None:
        """Reserva fail-closed ANTES de cualquier efecto de red."""
        allowed, reason = self.budget.try_reserve(
            unit_id, input_estimate, max_tokens, rate)
        if not allowed:
            raise DispatcherError("budget-gate",
                                  f"preflight refused POST: {reason}")

    def _build_body(self, messages: list[dict], max_tokens: int) -> dict:
        """Body del POST (la clave viaja en cabecera HTTP, jamás aquí)."""
        return {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "reasoning_effort": self.reasoning_effort,
            "stream": False,
        }

    def _post_with_failover(self, account: str,
                            body: dict) -> tuple[str, dict, bool, Optional[str]]:
        """POST y, solo ante 402, UN salto a otra cuenta (regla 2026-09-13).
        D5: NUNCA hace release; devuelve la cuenta fallida para que el
        llamador libere cada cuenta EXACTAMENTE una vez."""
        attempt = self._post_with(account, body)
        failover_used = False
        failed_account: Optional[str] = None
        if (not attempt.get("ok") and not attempt.get("uncertain")
                and not attempt.get("local")
                and attempt.get("statusCode") == 402):
            self.pool.mark_no_balance(account)
            backup = self.pool.next_after_402(account)
            if backup is not None:
                failed_account = account
                failover_used = True
                account = backup
                attempt = self._post_with(account, body)
        return account, attempt, failover_used, failed_account

    def _release_attempt(self, account: str, attempt: dict) -> None:
        """Registra en el pool el resultado real del intento (uso y coste)."""
        self.pool.release(account, ok=attempt.get("ok", False),
                          status=attempt.get("statusCode", 0),
                          cost_usd=attempt.get("cost", 0.0),
                          tokens=sum(int((attempt.get("usage") or {}).get(k, 0))
                                     for k in ("prompt_tokens", "completion_tokens")))

    # --- internos ------------------------------------------------------------
    def _post_with(self, account: str, body: dict) -> dict:
        """Un POST con la clave de la cuenta EN MEMORIA (nunca impresa)."""
        key = self.pool.resolve_key(account)
        if not key:
            return {"ok": False, "local": True, "statusCode": 0,
                    "error": {"code": "no-key-resolved"}, "cost": 0.0,
                    "usage": None}
        try:
            return self.transport.post(self.endpoint, key, body,
                                       timeout_s=self.timeout_s)
        except OSError as exc:
            # D3: transporte incierto (timeout/reset/refused): jamás reintentar.
            return {"ok": False, "uncertain": True,
                    "error": {"transport": exc.__class__.__name__},
                    "cost": 0.0, "usage": None}
        except Exception as exc:
            # D3: fallo LOCAL definitivo (JSON roto, ValueError...): no atasca
            # in_flight ni la reserva; clasificación local, sin reintento.
            return {"ok": False, "local": True, "statusCode": 0,
                    "error": {"code": "local-transport-failure",
                              "detail": exc.__class__.__name__},
                    "cost": 0.0, "usage": None}

    def _refund_reserve(self, unit_id: str, input_estimate: int,
                        max_tokens: int, rate: float) -> None:
        """D2: sin cuenta disponible no hubo POST: se LIBERA la reserva
        estimada (release_reserve real, sin fuga de presupuesto)."""
        est = (input_estimate + max_tokens) * rate / 1_000_000.0
        self.budget.release_reserve(unit_id, est)
