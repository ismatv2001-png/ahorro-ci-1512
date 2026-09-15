#!/usr/bin/env bash
# sync.sh — Re-ejecutable: copia los módulos de la batería de tests del carril
# de ahorro desde sus rutas originales a este staging (ci-github-staging/).
#
#   bash sync.sh
#
# Qué copia (cp de las rutas originales, idempotente):
#   * ahorro-core-main/  -> ci-github-staging/ahorro-core-main/
#   * costo-token-real/  -> ci-github-staging/costo-token-real/
#   * secrets_loader.py  -> ci-github-staging/secrets_loader.py
#
# Qué NUNCA copia: el vault ni claves (el vault vive en
# ~/.config/deepseek-harness-1512/secrets/... , fuera del repo). En CI,
# secrets_loader.py opera sin vault (devuelve None si no existe) y los tests
# de la batería no tocan red.
# Excluye del staging: ahorro_runner.py + test_ahorro_runner.py (WIP de otra
# lane que llama a la API real; ver bloque 3).
set -euo pipefail

ORIG="${AHORRO_ORIG:-/workspaces/harness-1512/nuevos-trabajos}"
DEST="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[sync] origen : $ORIG"
echo "[sync] destino: $DEST"

# 1. Directorios de módulos + tests (cp -a preserva; se purga __pycache__).
for dir in ahorro-core-main costo-token-real; do
  if [ ! -d "$ORIG/$dir" ]; then
    echo "[sync] ERROR: no existe $ORIG/$dir" >&2
    exit 1
  fi
  rm -rf "$DEST/$dir"                      # re-ejecutable: destino previo fuera
  cp -a "$ORIG/$dir" "$DEST/$dir"
  find "$DEST/$dir" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
  echo "[sync] copiado $dir/ ($(find "$DEST/$dir" -type f | wc -l) ficheros)"
done

# 2. secrets_loader.py (raíz del worktree -> raíz del staging).
if [ ! -f "$ORIG/secrets_loader.py" ]; then
  echo "[sync] ERROR: no existe $ORIG/secrets_loader.py" >&2
  exit 1
fi
cp -a "$ORIG/secrets_loader.py" "$DEST/secrets_loader.py"
echo "[sync] copiado secrets_loader.py"

# 3. Runner CLI: INCLUIDO. Su test quedó blindado contra la red el
#    2026-09-15 (stub de urlopen en proceso + sitecustomize en subprocesos +
#    tripwire; 14/14 sin red), así que la batería sigue siendo 100% offline.
echo "[sync] ahorro_runner.py + test incluidos (test blindado contra red)"

# 4. Guardas: sin vault real, sin directorio secrets/ y sin claves por contenido.
#    (smoke_dispatcher_vault.py es código legítimo: "vault" en su nombre no es
#    material sensible; los "sk-..." cortos de los tests son claves sintéticas.)
if find "$DEST" -type f \( -name 'vault-*.json' -o -name '*.key' -o -name '*.pem' \
     -o -name '*credential*' \) | grep -q .; then
  echo "[sync] ERROR: fichero sensible detectado en el staging (abortando)" >&2
  exit 1
fi
if [ -d "$DEST/secrets" ] || find "$DEST" -type d -name secrets | grep -q .; then
  echo "[sync] ERROR: directorio secrets/ detectado en el staging (abortando)" >&2
  exit 1
fi
if grep -rInE 'ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}' \
     "$DEST" >/dev/null 2>&1; then
  echo "[sync] ERROR: patrón de clave real detectado por contenido (abortando)" >&2
  exit 1
fi
echo "[sync] OK: staging sin vault ni claves. Batería lista para CI."
