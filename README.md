# ci-github-staging — CI gratuito (netAmount=0) de la batería del carril de ahorro

Staging local con todo lo necesario para correr la batería de tests en **GitHub
Actions gratuito** (runner público `ubuntu-latest`, cuota incluida, sin
facturación). No se usa ningún secreto en CI: los tests no tocan red.

## Contenido

| Fichero | Qué es |
|---|---|
| `.github/workflows/battery.yml` | Workflow CI: checkout → Python 3.12 → unittest ×2 → fuzz |
| `ahorro-core-main/` | Módulos, tests (`test_*.py`) y fuzz del carril de ahorro |
| `costo-token-real/` | Módulo y tests de coste por token con uso real |
| `secrets_loader.py` | Loader de claves; en CI opera **sin vault** (devuelve `None`) |
| `sync.sh` | Re-ejecutable: copia los módulos desde las rutas originales |
| `EVIDENCIA-CI-LOCAL.md` | Evidencia de la ejecución local (simulación del CI) |

La batería es 100% stdlib (sin `requirements.txt`): el workflow instala
dependencias solo si el fichero existiera.

## Paso 0 — Sincronizar los módulos (re-ejecutable)

```bash
cd /workspaces/harness-1512/nuevos-trabajos/ci-github-staging
bash sync.sh
```

Copia `ahorro-core-main/`, `costo-token-real/` y `secrets_loader.py` desde
`/workspaces/harness-1512/nuevos-trabajos/` con `cp -a` (idempotente: se puede
repetir). **Nunca copia el vault ni claves** (el vault vive en
`~/.config/deepseek-harness-1512/secrets/...`, fuera del repo).

## Paso 1 — Confirmar el token GitHub del vault (SIN imprimirlo)

El token ya existente se lee **en memoria** desde el vault y solo se muestra su
prefijo enmascarado:

```bash
python3 -c "import sys; sys.path.insert(0,'/workspaces/harness-1512/nuevos-trabajos'); import secrets_loader; print(secrets_loader.load_github_oauth()[:6]+'***')"
```

Si imprime `6 caracteres + ***`, el token está disponible. Variante que no
imprime ni el prefijo:

```bash
python3 -c "import sys; sys.path.insert(0,'/workspaces/harness-1512/nuevos-trabajos'); import secrets_loader; t=secrets_loader.load_github_oauth(); print('OK len=%d' % len(t) if t else 'MISSING')"
```

> ⚠️ **JAMÁS** pegues el token en un fichero, en el chat ni en logs. Solo se
> usa en memoria para alimentar `gh` por stdin.

## Paso 2 — Autenticar gh CLI con el token (sin mostrarlo)

```bash
TOKEN=$(python3 -c "import sys; sys.path.insert(0,'/workspaces/harness-1512/nuevos-trabajos'); import secrets_loader; print(secrets_loader.load_github_oauth())")
printf '%s' "$TOKEN" | gh auth login --with-token
unset TOKEN
gh auth status
```

`gh auth login --with-token` lee el token de stdin: nunca aparece en la línea
de comandos ni en disco.

## Paso 3 — Crear el repo privado y hacer push

```bash
cd /workspaces/harness-1512/nuevos-trabajos/ci-github-staging
git init -b main
git add .
git commit -m "CI batería de ahorro (netAmount=0, sin secretos)"
gh repo create ahorro-ci-1512 --private --source=. --remote=origin --push
```

Equivalente en dos pasos:

```bash
gh repo create ahorro-ci-1512 --private
git remote add origin "https://github.com/<tu-usuario>/ahorro-ci-1512.git"
git push -u origin main
```

## Paso 4 — Ver el CI

```bash
gh run list --repo <tu-usuario>/ahorro-ci-1512
gh run watch --repo <tu-usuario>/ahorro-ci-1512
```

El workflow `battery-ahorro` arranca con cada push y puede lanzarse a mano con
`workflow_dispatch` (pestaña Actions → Run workflow). Umbrales: ≤15 min por
job; sin secretos; sin llamadas a red.

## Notas de seguridad

* No hay claves en este repo (verificado con grep antes del sync).
* `secrets_loader.py` en CI no tiene vault: `_load_vault()` devuelve `None` y
  todas las funciones de carga devuelven `None`/`{}` — los tests no lo usan.
* Los `sk-...` que aparecen en `costo-token-real/test_cost_meter.py` son
  claves sintéticas de prueba (el propio test verifica que nunca se impriman).
* `ahorro_runner.py` + `test_ahorro_runner.py` (WIP de otra lane) llaman a la
  API real (`https://api.deepseek.com/user/balance`) y quedan **excluidos del
  staging por `sync.sh`** (bloque 3): la batería del CI no toca red. Si esa
  lane los termina sin red, basta borrar el bloque 3 de `sync.sh`.
