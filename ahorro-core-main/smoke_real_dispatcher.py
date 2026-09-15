#!/usr/bin/env python3
"""smoke_real_dispatcher.py — UN solo POST real de verificación end-to-end.

Cadena COMPLETA con la API oficial: vault -> AccountPool -> presupuesto
semanal -> clave en memoria -> POST https://api.deepseek.com/chat/completions
con max_tokens mínimo. Coste esperado ~0.00002 USD. La clave jamás se imprime;
el receipt solo muestra account, failoverUsed, usage y coste real.
"""

import json
import pathlib
import ssl
import sys
import tempfile
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from secrets_loader import load_deepseek_keys  # noqa: E402

# S2: tarifas congeladas en constantes con nombre (una sola fuente).
TARIFA_INPUT_PER_1M = 0.66
TARIFA_CACHE_HIT_PER_1M = 0.022
TARIFA_OUTPUT_PER_1M = 1.98
from account_pool import AccountPool  # noqa: E402
from budget_weekly import WeeklyBudget  # noqa: E402
from dispatcher_multicuenta import MultiAccountDispatcher  # noqa: E402


class RealTransport:
    """POST real a la API oficial; devuelve el protocolo del dispatcher."""

    def __init__(self):
        self.ctx = ssl.create_default_context()

    def post(self, endpoint, key, body, timeout_s):
        req = urllib.request.Request(
            endpoint, data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout_s,
                                        context=self.ctx) as r:
                data = json.loads(r.read().decode("utf-8"))
                usage = data.get("usage") or {}
                # Coste real con tarifa congelada (deepseek-v4-pro: 0.66/1.98).
                prompt = int(usage.get("prompt_tokens") or 0)
                cache_hit = int(usage.get("prompt_cache_hit_tokens") or 0)
                completion = int(usage.get("completion_tokens") or 0)
                cost = ((prompt - min(cache_hit, prompt)) * TARIFA_INPUT_PER_1M
                        + min(cache_hit, prompt) * TARIFA_CACHE_HIT_PER_1M
                        + completion * TARIFA_OUTPUT_PER_1M) / 1_000_000.0
                content = ""
                choices = data.get("choices") or []
                if choices and choices[0].get("message"):
                    content = choices[0]["message"].get("content") or ""
                return {"ok": True, "statusCode": r.status, "usage": usage,
                        "content": content[:200],
                        "model": data.get("model"),
                        "finishReason": (choices[0].get("finish_reason")
                                         if choices else None),
                        "cost": round(cost, 8)}
        except urllib.error.HTTPError as e:
            return {"ok": False, "statusCode": e.code, "cost": 0.0,
                    "usage": None, "error": e.reason}
        except OSError as e:
            return {"ok": False, "uncertain": True, "cost": 0.0,
                    "usage": None, "error": type(e).__name__}


def main() -> int:
    keys = load_deepseek_keys(only_available=True)
    if not keys:
        print("ERROR: sin claves disponibles", file=sys.stderr)
        return 1
    resolver = lambda ref: keys.get(ref)
    pool = AccountPool(
        [{"label": lbl, "key_ref": lbl, "budget_usd_diario": 0.50,
          "max_concurrent": 4} for lbl in sorted(keys)],
        resolver=resolver)
    with tempfile.TemporaryDirectory() as td:
        budget = WeeklyBudget(pathlib.Path(td) / "b.json")
        budget.set_weekly("smoke-real", 0.01)
        d = MultiAccountDispatcher(pool, budget, resolver, RealTransport())
        plan = {"max_tokens": 8, "rate_usd_per_1m": 0.66}
        receipt = d.submit(
            "smoke-real",
            [{"role": "user", "content": "responde solo: ok"}], plan)
    out = {k: receipt.get(k) for k in
           ("status", "unit", "account", "failoverUsed", "usage", "cost",
            "postedAt")}
    print(json.dumps(out, ensure_ascii=False, sort_keys=True))
    return 0 if receipt.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
