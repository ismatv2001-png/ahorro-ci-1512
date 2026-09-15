#!/usr/bin/env python3
"""secrets_loader.py — Carga de claves de APIs de IA EN MEMORIA, sin exponerlas.

REGLA DE SEGURIDAD (propietario 2026-09-13): las claves JAMÁS se imprimen, ni
van a logs, ni a argv, ni a ficheros. Este loader:

  * lee cada clave de su ruta canónica (fichero local 0600 o variable de
    entorno) SOLO cuando se necesita, en memoria;
  * `--report` muestra únicamente máscaras (prefijo***sufijo, longitud);
  * `--verify` comprueba saldo DeepSeek contra la API oficial mostrando solo
    status / is_available (la clave viaja en la cabecera HTTP, nunca en la
    salida);
  * `--run <comando...>` ejecuta el comando con DEEPSEEK_API_KEY y
    DEEPSEEK_FALLBACK_API_KEY en el ENTORNO del proceso hijo: la clave no pasa
    por la línea de comandos ni por stdout;
  * API Python: `load(name)`, `load_ai_keys()`, `report()`, `verify_deepseek()`.

Fuentes canónicas (ver CREDENTIALS-REGISTRY-20260915.md):
  deepseek_primary   -> env DEEPSEEK_API_KEY, si no ~/.config/deepseek-harness-1512/api-key
  deepseek_original  -> env DEEPSEEK_FALLBACK_API_KEY, si no api-key.1512-original-20260915
  gemini_1512        -> C:\\...\\IA-BARATA-QWEN3\\claves\\clave-1512.txt (solo Windows)
  gemini_2001        -> C:\\...\\IA-BARATA-QWEN3\\claves\\clave-2001.txt (solo Windows)
  openai / anthropic -> rutas opcionales; hoy NO existen en la máquina.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request

HOME = pathlib.Path.home()
DS_CONFIG = HOME / ".config" / "deepseek-harness-1512"
VAULT_PATH = DS_CONFIG / "secrets" / "vault-20260915.json"

# Claves con nombres estables para el software de ahorro.
SOURCES: dict[str, dict] = {
    "deepseek_primary": {
        "env": "DEEPSEEK_API_KEY",
        "file": str(DS_CONFIG / "api-key"),
        "kind": "deepseek",
    },
    "deepseek_original": {
        "env": "DEEPSEEK_FALLBACK_API_KEY",
        "file": str(DS_CONFIG / "api-key.1512-original-20260915"),
        "kind": "deepseek",
    },
    "gemini_1512": {
        "env": "GEMINI_API_KEY",
        "file": r"C:\Users\Administrador\Documents\Codex\deep seek harness\IA-BARATA-QWEN3\claves\clave-1512.txt",
        "kind": "gemini",
    },
    "gemini_2001": {
        "env": "GEMINI_2001_API_KEY",
        "file": r"C:\Users\Administrador\Documents\Codex\deep seek harness\IA-BARATA-QWEN3\claves\clave-2001.txt",
        "kind": "gemini",
    },
}

# Registradas para el día en que existan (hoy devuelven None).
OPTIONAL_SOURCES: dict[str, dict] = {
    "openai": {"env": "OPENAI_API_KEY", "kind": "openai"},
    "anthropic": {"env": "ANTHROPIC_API_KEY", "kind": "anthropic"},
}

# Tokens del hub HCP (dispatcher/cola de agentes). JSON multi-clave: se cargan
# EN MEMORIA completos para que el dispatcher los use; nunca se imprimen.
HUB_TOKEN_FILES: dict[str, str] = {
    "hcp_lane_tokens": "/home/codespace/recovery-work-20260915/files/nuevos-trabajos/.hcp-lane-tokens.json",
    "hcp_slot_tokens": "/home/codespace/recovery-work-20260915/files/nuevos-trabajos/haztodo-control-plane/docs/evidence/phase2/phase5-slot-tokens.json",
}


def _load_vault() -> dict | None:
    """Lee el vault EN MEMORIA. None si no existe o no es JSON."""
    if not VAULT_PATH.is_file():
        return None
    try:
        return json.loads(VAULT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def load_deepseek_keys(only_available: bool = True) -> dict[str, str]:
    """Todas las claves DeepSeek del vault: {etiqueta: valor} EN MEMORIA."""
    vault = _load_vault()
    out: dict[str, str] = {}
    if not vault:
        return out
    for label, entry in (vault.get("deepseek") or {}).items():
        if not isinstance(entry, dict):
            continue
        value = entry.get("value")
        if not isinstance(value, str):
            continue
        if only_available and entry.get("verified", {}).get("is_available") is not True:
            continue
        out[label] = value
    return out


def load_github_oauth() -> str | None:
    """OAuth de GitHub desde el vault, en memoria (nunca impresa)."""
    vault = _load_vault()
    if not vault:
        return None
    gh = vault.get("github") or {}
    entry = gh.get("oauth_ismatv2001_png")
    if isinstance(entry, dict):
        value = entry.get("value")
        if isinstance(value, str):
            return value
    return None


def load_ci_tokens() -> dict[str, str]:
    """Tokens de buzones CI: {fuente.campo: valor} EN MEMORIA."""
    vault = _load_vault()
    if not vault:
        return {}
    out: dict[str, str] = {}
    for k, v in (vault.get("ci_mailboxes") or {}).items():
        if isinstance(v, str):
            out[str(k)] = v
    return out


def load_hub_tokens(name: str) -> dict[str, str] | None:
    """Carga un mapa de tokens HCP EN MEMORIA. None si el fichero no existe."""
    path = HUB_TOKEN_FILES.get(name)
    if path is None:
        raise KeyError(f"unknown hub token file: {name}")
    p = pathlib.Path(path)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return {str(k): str(v) for k, v in data.items()}


def report_hub_tokens() -> list[str]:
    """Líneas de máscaras de los tokens HCP (nunca valores)."""
    lines = []
    for name, path in sorted(HUB_TOKEN_FILES.items()):
        data = load_hub_tokens(name)
        if data is None:
            lines.append(f"{name:18s} MISSING  {path}")
            continue
        masked_entries = ", ".join(
            f"{k}:{mask(v)}" for k, v in sorted(data.items())[:4])
        more = "" if len(data) <= 4 else f", … +{len(data) - 4} más"
        lines.append(
            f"{name:18s} {len(data):3d} tokens  [{masked_entries}{more}]  {path}")
    return lines


def _read_clean(path: str) -> str | None:
    p = pathlib.Path(path)
    if not p.is_file():
        return None
    try:
        value = p.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    return value or None


def load(name: str) -> str | None:
    """Devuelve la clave EN MEMORIA (nunca la imprime). Fail-closed: None si falta."""
    if name in SOURCES:
        src = SOURCES[name]
        value = os.environ.get(src["env"])
        if value:
            return value.strip()
        path = src.get("file")
        if path:
            value = _read_clean(path)
            if value:
                return value
        return None
    if name in OPTIONAL_SOURCES:
        value = os.environ.get(OPTIONAL_SOURCES[name]["env"])
        return value.strip() if value else None
    raise KeyError(f"unknown secret name: {name}")


def load_ai_keys() -> dict[str, str | None]:
    """Todas las claves de IA en un dict en memoria (None = no disponible)."""
    return {name: load(name) for name in list(SOURCES) + list(OPTIONAL_SOURCES)}


def mask(value: str | None) -> str:
    """Máscara segura: prefijo***sufijo con longitud (nunca el valor)."""
    if value is None:
        return "MISSING"
    if len(value) <= 8:
        return f"len={len(value)}"
    return f"{value[:4]}***{value[-3:]} (len={len(value)})"


def report() -> list[str]:
    """Líneas de máscaras y estado de todas las fuentes (nunca valores)."""
    lines = []
    # Claves DeepSeek del vault (con su estado verificado).
    vault = _load_vault()
    if vault:
        lines.append("== DeepSeek (vault)")
        for label, entry in sorted((vault.get("deepseek") or {}).items()):
            value = entry.get("value") if isinstance(entry, dict) else None
            ver = (entry.get("verified") or {}) if isinstance(entry, dict) else {}
            lines.append(
                f"{label:28s} {mask(value):26s} http={ver.get('http')} "
                f"available={ver.get('is_available')}")
        gh = vault.get("github") or {}
        for gk, gv in gh.items():
            if isinstance(gv, dict):
                lines.append(f"{'github:'+gk:28s} {mask(gv.get('value')):26s} "
                             f"http={gv.get('verified', {}).get('http')}")
        ci = vault.get("ci_mailboxes") or {}
        for ck in sorted(ci):
            lines.append(f"{'ci:'+ck[:40]:28s} {mask(str(ci[ck])):26s}")
    else:
        lines.append("vault: MISSING")
    for name in sorted(SOURCES):
        src = SOURCES[name]
        value = load(name)
        src_desc = f"env:{src['env']} | file:{src.get('file', '—')}"
        lines.append(f"{name:20s} [{src['kind']:8s}] {mask(value):28s} {src_desc}")
    for name in sorted(OPTIONAL_SOURCES):
        src = OPTIONAL_SOURCES[name]
        value = load(name)
        lines.append(f"{name:20s} [{src['kind']:8s}] {mask(value):28s} env:{src['env']} (ruta opcional no registrada)")
    return lines


def verify_deepseek(key: str, label: str) -> dict:
    """GET /user/balance: devuelve solo estado, jamás la clave."""
    req = urllib.request.Request(
        "https://api.deepseek.com/user/balance",
        headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.loads(r.read().decode("utf-8"))
            infos = body.get("balance_infos") or []
            has_funds = any(
                str(b.get("total_balance", "0")).replace(".", "", 1).lstrip("0")
                not in ("", "0") for b in infos)
            return {"label": label, "http": r.status,
                    "is_available": body.get("is_available"),
                    "currency": body.get("currency"),
                    "balance_entries": len(infos),
                    "has_funds": has_funds}
    except urllib.error.HTTPError as e:
        return {"label": label, "http": e.code, "error": e.reason}
    except Exception as e:  # transporte
        return {"label": label, "http": None,
                "error": type(e).__name__}


def run_command(argv: list[str]) -> int:
    """Ejecuta un comando con las claves solo en el ENTORNO del hijo."""
    primary = load("deepseek_primary")
    original = load("deepseek_original")
    if not primary:
        print("ERROR: deepseek_primary no disponible; nada que exportar.",
              file=sys.stderr)
        return 2
    env = dict(os.environ)
    env["DEEPSEEK_API_KEY"] = primary
    if original:
        env["DEEPSEEK_FALLBACK_API_KEY"] = original
    # Claves Gemini si existen (solo en Windows).
    for gname, genv in (("gemini_1512", "GEMINI_API_KEY"),
                        ("gemini_2001", "GEMINI_2001_API_KEY")):
        gkey = load(gname)
        if gkey and genv not in env:
            env[genv] = gkey
    if not argv:
        print("ERROR: --run necesita un comando.", file=sys.stderr)
        return 2
    return subprocess.call(argv, env=env)


def main() -> int:
    """CLI: --report/--hub-report/--verify/--run (claves solo en entorno)."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--report", action="store_true",
                    help="mostrar solo máscaras y estado (nunca valores)")
    ap.add_argument("--hub-report", action="store_true",
                    help="mostrar solo máscaras de los tokens HCP (nunca valores)")
    ap.add_argument("--verify", action="store_true",
                    help="comprobar saldo DeepSeek (solo status/is_available)")
    ap.add_argument("--run", nargs=argparse.REMAINDER,
                    help="ejecutar comando con claves en el entorno del hijo")
    args = ap.parse_args()

    if args.run is not None and args.run:
        return run_command(args.run)
    if args.hub_report:
        print("\n".join(report_hub_tokens()))
        return 0
    if args.verify:
        # Verifica TODAS las claves DeepSeek del vault + las canónicas.
        seen: set[str] = set()
        for label, key in load_deepseek_keys(only_available=False).items():
            if key in seen:
                continue
            seen.add(key)
            res = verify_deepseek(key, label)
            print(json.dumps(res, ensure_ascii=False, sort_keys=True))
        for name in ("deepseek_primary", "deepseek_original"):
            key = load(name)
            if key is None or key in seen:
                if key is None:
                    print(f"{name}: MISSING (no verificable)")
                continue
            seen.add(key)
            res = verify_deepseek(key, name)
            print(json.dumps(res, ensure_ascii=False, sort_keys=True))
        return 0
    # Por defecto (o --report): máscaras.
    print("\n".join(report()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
