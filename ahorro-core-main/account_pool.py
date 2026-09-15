#!/usr/bin/env python3
"""account_pool.py — Pool de cuentas DeepSeek para el software de ahorro.

Permite usar EN PARALELO las 4 claves DeepSeek con saldo (hasta 500 agentes):
selección round-robin ponderada por presupuesto restante, detección de 402
(cuenta sin saldo) con UN solo salto a otra cuenta, y métricas por cuenta.

REGLA DE SEGURIDAD: este módulo NUNCA contiene claves. Las referencias
`key_ref` (etiquetas del vault) las resuelve `secrets_loader.load_deepseek_keys()`
en memoria en el momento del envío. Sin imports pesados ni red: testable con
dobles inyectados.
"""

from __future__ import annotations

import datetime as dt
import threading
from typing import Any, Callable, Optional


class AccountPool:
    """Pool de cuentas DeepSeek: round-robin por presupuesto y 402 sin saldo."""

    def __init__(
        self,
        accounts: list[dict],
        *,
        clock: Optional[Callable[[], dt.datetime]] = None,
        resolver: Optional[Callable[[str], Optional[str]]] = None,
    ) -> None:
        """accounts: [{label, key_ref, budget_usd_diario, max_concurrent}]."""
        if not accounts:
            raise ValueError("accounts must be non-empty")
        self.accounts: dict[str, dict] = {}
        for a in accounts:
            label = str(a.get("label", ""))
            if not label:
                raise ValueError("account label required")
            self.accounts[label] = {
                "label": label,
                "key_ref": str(a.get("key_ref", "")),
                "budget_usd_diario": float(a.get("budget_usd_diario", 0.0)),
                "max_concurrent": int(a.get("max_concurrent", 1)),
                "spent_usd": 0.0,
                "tokens": 0,
                "errors": 0,
                "calls": 0,
                "in_flight": 0,
                "no_balance": bool(a.get("no_balance", False)),
            }
        self._clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self._resolver = resolver or (lambda ref: None)
        # A2: solo se exige clave resoluble si HAY resolver explícito; sin él,
        # el pool funciona como planificador puro (dobles de tests).
        self._require_resolvable = resolver is not None
        self._rr: dict[str, int] = {lbl: 0 for lbl in self.accounts}
        self._day = self._clock().date()
        self._lock = threading.RLock()

    # --- selección ---------------------------------------------------------
    def pick(self) -> Optional[str]:
        """Cuenta con presupuesto restante, clave resoluble y hueco de
        concurrencia, round-robin ponderado por presupuesto restante.
        Atómico entre hebras (RLock): jamás dos hebras reciben la misma
        cuenta con max_concurrent=1 (A1)."""
        with self._lock:
            self._rollover_if_needed()
            candidates = [
                lbl for lbl, a in self.accounts.items()
                if not a["no_balance"]
                and a["in_flight"] < a["max_concurrent"]
                and a["spent_usd"] < a["budget_usd_diario"]
                # A2: cuenta sin clave resoluble jamás se elige (evita
                # starvation de intentos que fallarían localmente).
                and (not self._require_resolvable
                     or self._resolver(a["key_ref"]) is not None)
            ]
            if not candidates:
                return None
            candidates.sort(key=lambda lbl: (
                -(self.accounts[lbl]["budget_usd_diario"]
                  - self.accounts[lbl]["spent_usd"]),
                self._rr[lbl],
            ))
            label = candidates[0]
            self._rr[label] += 1
            self.accounts[label]["in_flight"] += 1
            return label

    def acquire(self) -> Optional[str]:
        """Sinónimo semántico de pick(): devuelve la cuenta o None."""
        return self.pick()

    def release(self, label: str, *, ok: bool = True, status: int = 200,
                cost_usd: float = 0.0, tokens: int = 0) -> None:
        """Registra el resultado del intento con esa cuenta (atómico)."""
        with self._lock:
            a = self.accounts[label]
            a["in_flight"] = max(0, a["in_flight"] - 1)
            a["calls"] += 1
            if not ok:
                a["errors"] += 1
                if status == 402:
                    a["no_balance"] = True  # sin saldo: fuera de rotación
                return
            a["spent_usd"] = max(0.0, a["spent_usd"] + max(0.0, cost_usd))
            a["tokens"] += max(0, int(tokens))

    # --- failover con UN solo salto ----------------------------------------
    def next_after_402(self, failed_label: str) -> Optional[str]:
        """Otra cuenta disponible para el ÚNICO salto permitido tras un 402."""
        with self._lock:
            self._rollover_if_needed()
            for lbl in sorted(self.accounts, key=lambda x: self._rr[x]):
                a = self.accounts[lbl]
                if lbl == failed_label or a["no_balance"]:
                    continue
                if self._require_resolvable and self._resolver(a["key_ref"]) is None:
                    continue  # A2: sin clave resoluble, no es candidata
                if a["in_flight"] < a["max_concurrent"] and \
                        a["spent_usd"] < a["budget_usd_diario"]:
                    self._rr[lbl] += 1
                    a["in_flight"] += 1
                    return lbl
            return None

    # --- métricas -----------------------------------------------------------
    def snapshot(self) -> dict:
        """Métricas por cuenta y restante total del día (sin claves)."""
        with self._lock:
            self._rollover_if_needed()
            per = {}
            for lbl, a in self.accounts.items():
                per[lbl] = {
                    "budget_usd_diario": a["budget_usd_diario"],
                    "spent_usd": round(a["spent_usd"], 6),
                    "remaining_usd": round(
                        max(0.0, a["budget_usd_diario"] - a["spent_usd"]), 6),
                    "tokens": a["tokens"],
                    "calls": a["calls"],
                    "errors": a["errors"],
                    "in_flight": a["in_flight"],
                    "no_balance": a["no_balance"],
                    "max_concurrent": a["max_concurrent"],
                }
            return {
                "day": self._day.isoformat(),
                "accounts": per,
                "total_remaining_usd": round(
                    sum(p["remaining_usd"] for p in per.values()), 6),
            }

    def resolve_key(self, label: str) -> Optional[str]:
        """Clave de la cuenta EN MEMORIA vía el resolver (nunca impresa)."""
        return self._resolver(self.accounts[label]["key_ref"])

    def mark_no_balance(self, label: str) -> None:
        """Marca 402 SIN tocar in_flight/calls/errors (para el flujo de
        failover, donde el release único lo hace el llamador)."""
        with self._lock:
            self.accounts[label]["no_balance"] = True

    # --- rollover diario ----------------------------------------------------
    def _rollover_if_needed(self) -> None:
        today = self._clock().date()
        if today != self._day:
            for a in self.accounts.values():
                a["spent_usd"] = 0.0
                a["no_balance"] = False  # 402 del día anterior no marca hoy
            self._day = today

def snapshot_json(pool: AccountPool) -> str:
    """JSON del snapshot del pool (nunca incluye claves ni key_ref)."""
    import json
    return json.dumps(pool.snapshot(), ensure_ascii=False, sort_keys=True)
