#!/usr/bin/env python3
"""dispatcher_multicuenta.py — Dispatcher/cola de agentes con 4 cuentas DeepSeek.

Encadena las piezas del software de ahorro:
  semantic_cache  -> sirve el hit SIN reserva ni red (0 coste)
  budget_weekly   -> try_reserve fail-closed ANTES de cualquier POST
  account_pool    -> cuenta round-robin por presupuesto; 402 marca sin saldo
  secrets_loader  -> resuelve la clave SOLO en memoria (jamás impresa)
  transporte      -> POST único; 402 => UN solo salto de cuenta (regla 2026-09-13)

Sin gasto en tests: el transporte es inyectable (doble). Las claves no viajan
en argv ni en logs: van en la cabecera HTTP desde memoria.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any, Callable, Optional

from account_pool import AccountPool
from budget_weekly import WeeklyBudget


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


class DispatcherError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class MultiAccountDispatcher:
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

    # --- cola ---------------------------------------------------------------
    def submit(self, unit_id: str, messages: list[dict], plan: dict) -> dict:
        """Un trabajo de la cola: cache -> reserva -> cuenta -> POST -> settle."""
        if not isinstance(messages, list) or not messages:
            raise DispatcherError("invalid-payload", "messages required")
        est_chars = sum(len(str(m.get("content") or "")) for m in messages
                        if isinstance(m, dict))
        input_estimate = max(1, est_chars // 3)
        max_tokens = int(plan.get("max_tokens", 0))
        rate = float(plan.get("rate_usd_per_1m", 0.0))

        # 1) Cache: hit => 0 coste, sin reserva, sin red.
        cache_key = sha256_text(canonical_json(messages))
        if self.cache is not None:
            hit = self.cache.get(canonical_json(messages))
            if hit is not None:
                return {"status": "cache-hit", "unit": unit_id,
                        "cacheKey": cache_key, "tokensSaved": hit.get("tokens_saved"),
                        "cost": 0.0, "postedAt": utc_now_iso()}

        # 2) Presupuesto semanal fail-closed ANTES de cualquier efecto.
        allowed, reason = self.budget.try_reserve(
            unit_id, input_estimate, max_tokens, rate)
        if not allowed:
            raise DispatcherError("budget-gate",
                                  f"preflight refused POST: {reason}")

        body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "reasoning_effort": self.reasoning_effort,
            "stream": False,
        }
        op_id = sha256_text(canonical_json(body))

        # 3) Cuenta del pool + clave EN MEMORIA.
        account = self.pool.pick()
        if account is None:
            self._refund_reserve(unit_id, input_estimate, max_tokens, rate)
            raise DispatcherError("no-account",
                                  "no account with budget/concurrency available")

        attempt = self._post_with(account, body)
        failover_used = False
        if (not attempt.get("ok") and not attempt.get("uncertain")
                and not attempt.get("local")
                and attempt.get("statusCode") == 402):
            self.pool.release(account, ok=False, status=402)
            backup = self.pool.next_after_402(account)
            if backup is not None:
                failover_used = True
                account = backup
                attempt = self._post_with(account, body)
        self.pool.release(account, ok=attempt.get("ok", False),
                          status=attempt.get("statusCode", 0),
                          cost_usd=attempt.get("cost", 0.0),
                          tokens=sum(int((attempt.get("usage") or {}).get(k, 0))
                                     for k in ("prompt_tokens", "completion_tokens")))

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
            raise DispatcherError("missing-usage",
                                  "response ok without usage; cannot compute cost")

        # 5) Poblar el cache con la respuesta REAL (ahorro en la siguiente).
        tokens_saved = sum(int(usage.get(k, 0)) for k in
                           ("prompt_tokens", "completion_tokens"))
        if self.cache is not None:
            self.cache.put(canonical_json(messages),
                           attempt.get("content"), tokens_saved)

        return {"status": "ok", "opId": op_id, "unit": unit_id,
                "account": account, "failoverUsed": failover_used,
                "usage": usage, "cost": attempt.get("cost"),
                "postedAt": utc_now_iso()}

    # --- internos ------------------------------------------------------------
    def _post_with(self, account: str, body: dict) -> dict:
        key = self.pool.resolve_key(account)
        if not key:
            return {"ok": False, "local": True, "statusCode": 0,
                    "error": {"code": "no-key-resolved"}, "cost": 0.0,
                    "usage": None}
        try:
            return self.transport.post(self.endpoint, key, body,
                                       timeout_s=self.timeout_s)
        except OSError as exc:
            return {"ok": False, "uncertain": True,
                    "error": {"transport": exc.__class__.__name__},
                    "cost": 0.0, "usage": None}

    def _refund_reserve(self, unit_id: str, input_estimate: int,
                        max_tokens: int, rate: float) -> None:
        # Sin cuenta disponible: no hubo POST; se compensa la reserva con un
        # settle de coste 0 y se devuelve el control al planificador.
        self.budget.settle(unit_id, 0.0)
