# EVIDENCIA-CI-LOCAL — Batería del carril de ahorro (simulación local del CI)

- Fecha (UTC): 2026-09-15T03:53:46Z (última re-ejecución ~03:58Z)
- Host: `codespaces-3e4aba` (Linux 6.8.0-1064-azure x86_64)
- Python local: 3.14.2 · Python en CI (workflow): 3.12 (`ubuntu-latest`)
- Directorio de trabajo: `/workspaces/harness-1512/nuevos-trabajos/ci-github-staging`
- Coste: 0 (ejecución local + workflow en runner público gratuito, netAmount=0)

## 1. Sync re-ejecutable (`bash sync.sh`, ejecutado 2 veces seguidas)

Ambas ejecuciones: `OK: staging sin vault ni claves. Batería lista para CI.`
Copiados: `ahorro-core-main/` (23 ficheros del origen), `costo-token-real/`
(9), `secrets_loader.py`; purga `__pycache__`. Guardas: bloquea `vault-*.json`,
`secrets/`, `*.key`, `*.pem` y patrones de claves reales por contenido.

**Exclusión documentada (bloque 3 de sync.sh):** `ahorro_runner.py` +
`test_ahorro_runner.py` — WIP de otra lane aparecido en el origen durante este
trabajo — llaman a la API real `https://api.deepseek.com/user/balance`. Al
ejecutarlo, su test falla con respuesta real `{'http': 401, 'is_available':
None}` (68 tests, 1 failure). Como la batería del CI no debe tocar red, sync.sh
los excluye del staging (nadie más los importa: solo su propio test). No se
modificó el original. Si la lane los termina sin red, basta borrar el bloque 3.

## 2. Batería (comandos exactos del workflow, estado FINAL del staging)

| # | Comando | Resultado | Exit | Tiempo |
|---|---|---|---|---|
| 1 | `python3 -m unittest discover -s ahorro-core-main -p 'test_*.py'` | **Ran 55 tests … OK** | 0 | 0.039 s |
| 2 | `python3 -m unittest discover -s costo-token-real -p 'test_*.py'` | **Ran 38 tests … OK** | 0 | 0.085 s |
| 3 | `python3 ahorro-core-main/fuzz_semantic_cache_props.py 100` | **12/12 propiedades PASAN (100 iteraciones c/u)** | 0 | <0.1 s |

Total: **93 tests unitarios OK + 12/12 propiedades fuzz OK**, 3/3 comandos con
exit 0. Salidas crudas: `/tmp/batt1c.log`, `/tmp/batt2c.log`, `/tmp/batt3c.log`.

Propiedades fuzz verificadas: p1-normalize-idempotente, p2-hash-determinista,
p3-hit-exacto, p4-mutacion-1-char-miss, p5-cache-vacio-miss, p6-max-entries,
p7-ttl-expirado-miss, p8-contadores-coherentes, p9-put-idempotente,
p10-tokens-saved-propaga, p11-sin-colisiones-observables, p12-stats-entries-real.

## 3. secrets_loader.py SIN vault (simulación exacta del runner de CI)

Con `HOME` apuntando a un directorio vacío (el runner no tiene el vault):

```
load_github_oauth()  -> None
load_deepseek_keys() -> {}
load_ci_tokens()     -> {}
SIN EXCEPCION (fail-closed)
```

Los tests de la batería no importan secrets_loader y no tocan red.

## 4. Seguridad verificada

- grep de patrones de claves reales (`ghp_…`, `github_pat_…`, `sk-…` largos,
  `AIza…`) en los logs de la batería: **0 coincidencias**.
- grep de los mismos patrones en el contenido del staging: **0 coincidencias**
  (los `sk-SUPERSECRETO-123`/`sk-TESTCLAVE-88aa` de `test_cost_meter.py` son
  claves sintéticas cortas que el propio test verifica que nunca se impriman).
- Sin `vault-*.json` ni directorio `secrets/` en el staging; `__pycache__`
  purgado y cubierto por `.gitignore`.
- Token GitHub del vault: **disponible** (verificado en memoria, len=40, NO
  impreso). No se ejecutó push: pendiente del Paso 2–3 del README.

## 5. Hashes SHA-256 (receipts hash-bound, estado final)

```
d9a3b5b6a88d0fc8e011f7e17ddda5b46b66b611301a834a540e18134aa0afc3  .github/workflows/battery.yml
8e07336231abc14fd75e49aeb74db1a74f7ca3141a34b057deb919699397d471  README.md
a2aa09b953b3ee2e5e7b2f430a34613e5f92ccce2eb64cc2d6ab24bd8f5361e1  sync.sh
68633d24ccff4b38d789aa4503b74b0507a80990ea9bac694b897da226dda15b  .gitignore
e7cb6b92e0ea29bf6815a666a90abf8a10179a1beefd2adb88b729aa456b5a52  secrets_loader.py
dab66e21a7e4071f2ce2313ae242ae9068298a500ba2ef9e31aeb3ba7ca1efef  ahorro-core-main/account_pool.py
8926d7a90e326706125a044db41ee28481ea701a6d570c35a4d0a21a40a62d09  ahorro-core-main/budget_gate.py
9e6b45ddf6f9ba681c9fac591d1d4717877fda5cb32d5665cc98e91446ef74d3  ahorro-core-main/budget_weekly.py
89f0f5ab7b303012320adb44ec2fd9f631f35d27351647b74801b99273a2ef10  ahorro-core-main/cost_meter.py
aabc355b0d0ba74653b0e7ff36c3df240115a6b8a649d5378084a774759c3164  ahorro-core-main/dispatcher_multicuenta.py
6582925d071d00f4a1b5289530a7eeafa83515b8135e67f260b1866095369ecc  ahorro-core-main/fuzz_semantic_cache_props.py
f74c833bbf97e09e933c58d1d90e51d6af4fad346f9838170182160df42d8a58  ahorro-core-main/model_router.py
444b2c068626ebf15f0c8bbf8736fafd3913e3f0b2a3d1f38b0f97403df932ef  ahorro-core-main/operate_ahorro.py
8805d09bae48b4f567237546336a24204e511fdb82a076f46e3f06fe17b23f6c  ahorro-core-main/quality_gate.py
50bb087187c840ec15f942b3fce7d7b839ad5d21b687dd525a632c295f489ebd  ahorro-core-main/savings_pipeline.py
a3772bb2b4ef49e842567442240d1ded823a184c85dc13237332edb4e2f63dd3  ahorro-core-main/semantic_cache.py
512b667bbfd770f765aba24bb0254ddb4e9965429e053256333e220450e7c82f  ahorro-core-main/smoke_dispatcher_vault.py
bf0c91bbf0f44832fcd36b1a01741b8f98f361cad598f19242425cbdfef2f635  ahorro-core-main/smoke_real_dispatcher.py
5223549d5afdb8a4c02efa7b3472d0309397c3ff75843fbfd07404a113464e30  ahorro-core-main/test_account_pool.py
4777f1b988b99f229bad90e17e430569050c7c0f765c9e21b2af4dee8bfee0ff  ahorro-core-main/test_ahorro_core.py
493303c2dc9dbb3dfe9489f38d5b28d89422df6d013e66f0234dd0577416a529  ahorro-core-main/test_budget_weekly.py
543b100e7a773dcee71f698357bc73c8015e81dd2579bece8330a6d8c132adab  ahorro-core-main/test_dispatcher_multicuenta.py
5d71ec1c03e05d0b1219b994d290e4930d562d71b2d643f4454f4ae2edad780c  costo-token-real/cost_meter.py
4e5a34126630c6bbca5194e504f4dbea9993e61b32a91870794207588760fe4b  costo-token-real/generar_evidencia.py
4b7b1b578095062a70f6d213c9d74ec602afe22fc0cea31af2aaa6c4d7b6769d  costo-token-real/test_cost_meter.py
```

## 6. Conclusión

Batería lista para CI gratuito: workflow YAML válido (`battery-ahorro`, 1 job
`battery`, runner público ubuntu-latest), 3/3 pasos verdes en local, sin
secretos y sin red. Pendiente (humano/gh CLI, ver README): `gh auth login
--with-token` con el token del vault leído en memoria, `gh repo create
ahorro-ci-1512 --private` y push. Coste neto: 0.
