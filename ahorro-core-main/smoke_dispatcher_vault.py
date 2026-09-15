#!/usr/bin/env python3
"""smoke_dispatcher_vault.py — Smoke de integración REAL sin gastar saldo.

Encadena: secrets_loader (vault 0600) -> AccountPool -> MultiAccountDispatcher
-> transporte DOBLE que verifica el formato de la clave (sk-, 35 chars) SIN
imprimirla ni enviarla a la red. Evidencia de que las 4 cuentas del vault
quedan utilizables por el dispatcher del software de ahorro.
"""

import json
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from secrets_loader import load_deepseek_keys  # noqa: E402
from account_pool import AccountPool  # noqa: E402
from budget_weekly import WeeklyBudget  # noqa: E402
from dispatcher_multicuenta import MultiAccountDispatcher  # noqa: E402


class ProbeTransport:
    """Doble: comprueba el formato de la clave y devuelve usage simulado."""

    def __init__(self):
        self.probed = []  # (label_de_cuenta, formato_ok) — nunca la clave

    def post(self, endpoint, key, body, timeout_s):
        ok_fmt = (isinstance(key, str) and key.startswith("sk-")
                  and len(key) == 35)
        self.probed.append(ok_fmt)
        return {"ok": True, "statusCode": 200,
                "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                          "prompt_cache_hit_tokens": 0},
                "content": "smoke-ok", "model": body.get("model"),
                "finishReason": "stop", "cost": 0.0001}


def main() -> int:
    keys = load_deepseek_keys(only_available=True)
    if len(keys) < 3:
        print(f"ERROR: solo {len(keys)} claves disponibles en el vault")
        return 1
    resolver = lambda ref: keys.get(ref)  # EN MEMORIA
    pool = AccountPool(
        [{"label": lbl, "key_ref": lbl, "budget_usd_diario": 5.0,
          "max_concurrent": 8} for lbl in sorted(keys)],
        resolver=resolver)
    with tempfile.TemporaryDirectory() as td:
        budget = WeeklyBudget(pathlib.Path(td) / "budget.json")
        budget.set_weekly("smoke-unit", 10.0)
        t = ProbeTransport()
        d = MultiAccountDispatcher(pool, budget, resolver, t)
        plan = {"max_tokens": 50, "rate_usd_per_1m": 1.0}
        receipts = []
        for i in range(8):  # 8 trabajos => reparto entre las cuentas
            r = d.submit("smoke-unit",
                         [{"role": "user", "content": f"smoke {i}"}], plan)
            receipts.append((r["account"], r["failoverUsed"], r["cost"]))
    # Verificaciones sin exponer claves (raises explícitos: funcionan con -O).
    if not all(t.probed):
        raise SystemExit("FALLO: alguna clave no pasó el control de formato")
    accounts_used = sorted({a for a, _, _ in receipts})
    print(json.dumps({
        "vault_available_keys": len(keys),
        "accounts_used": accounts_used,
        "jobs": len(receipts),
        "key_format_ok": len(t.probed),
        "all_costs": [c for _, _, c in receipts],
        "pool_snapshot": pool.snapshot()["accounts"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
