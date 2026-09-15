# HALLAZGOS-DATOS-REALES — métrica de coste por token (carril ismatv1512)

Fecha del sondeo: 2026-09-15 (UTC). Solo lectura; ningún contenido de sesión
se ha volcado. Este documento lista rutas exactas y qué existe / no existe.

## 1. ¿Hay datos de uso REALES? — SÍ

Sí. El harness DeepSeek persiste en disco el uso real de cada llamada a la
API. Se verificó leyendo el esquema desde el código del harness y sondeando
ficheros reales sin imprimir textos.

### 1.1 Qué campos de uso existen (esquema verificado en código y datos)

- Eventos `assistant/message` con dict `usage`:
  `inputTokens` (entrada NO cacheada = cache miss),
  `outputTokens` (salida, INCLUYE razonamiento),
  `cacheReadTokens` (entrada leída de caché = cache hit),
  `cacheWriteTokens` (escrituras de caché, informativo),
  `reasoningTokens` (informativo, subconjunto de outputTokens).
- Fuente del mapeo: `@deepseek-ai/dsh-llm-deepseek/lib/index.js`, función
  `mapUsage`: `inputTokens = prompt_tokens - cacheRead`,
  `outputTokens = completion_tokens`.
- Evento `request/context` con `model` (nombre de modelo, no sensible).
- Cada línea lleva `time` en ms epoch (UTC).

### 1.2 Dónde están los datos reales (rutas exactas)

| Ruta | Qué es | Estado |
|---|---|---|
| `/home/codespace/complete-1512/sessions/` | 845 sesiones históricas originales (restauración verificada) en `session.jsonl.zstd`, una por subdirectorio | 123 MB, procesable |
| `/home/codespace/complete-1512/manifest.json` | Manifiesto (ids, sha256, bytes, fechas) de esas 845 sesiones | Solo metadatos |
| `/home/codespace/.dsh-harness-1512-complete/sessions/` | Almacén vivo del harness (Linux), incluye la sesión actual de este carril | 128 MB, procesable |
| `/home/codespace/.dsh-harness-1512-recent72/sessions/` | Almacén "recent 72h" | 76 MB, procesable |
| `/home/codespace/.dsh-harness-1512/sessions/` | Almacén pequeño (6 sesiones de codespaces-blank) | 2 MB, procesable |
| `/home/codespace/.dsh-harness-1512-complete/storages/session_projcache/` | Caché de proyecciones con `tokenUsage` acumulado por sesión (`uncachedInputTokens`, `outputTokens`, `cacheReadTokens`, `cacheWriteTokens`) | 504 KB, agregados ya precomputados por el propio harness |

### 1.3 Censo agregado previo (complete-1512, 845 sesiones, sin volcar nada)

- Sesiones con eventos de uso: 59 de 845.
- Eventos de uso: 9.747.
- Modelos observados: `deepseek-v4-pro` (8.164 eventos explícitos),
  1.583 eventos en sesiones sin `request/context` → se atribuyen al modelo
  por defecto del harness (`deepseek-v4-pro`, según settings.yaml
  `agent-default-model`), marcados con `origen_modelo=por-defecto`.
- Rango temporal: 2026-09-03 03:53 UTC → 2026-09-14 23:37 UTC.
- Líneas rotas: 0.
- Formato: JSONL comprimido con zstd (`session.jsonl.zstd`); el binario
  `zstd` está disponible en `/opt/conda/bin/zstd`.

### 1.4 Qué NO existe (no inventar datos)

- No hay ficheros `usage*.json` / `*.jsonl` de telemetría dentro del checkout
  `/home/codespace/.local/lib/node_modules/@deepseek-ai/dsh/` (buscado por
  nombre y por contenido: 0 resultados de `prompt_tokens`/`completion_tokens`).
- El adaptador `@deepseek-ai/dsh-llm-deepseek` NO declara precios en USD:
  el coste en dólares lo aporta esta pieza (`cost_meter.py`) con la tarifa
  oficial pública (tabla configurable).
- `applied.jsonl` (reparación de archivos) NO es uso: es registro de
  hashes de reparación (excluido como fuente de uso).
- No hay logs de uso en `/workspaces/harness-1512/nuevos-trabajos` fuera de
  este subdirectorio (los `.pyc` de `port-ahorro-deepseek` son código de otro
  carril, no datos; no se han tocado).

## 2. Dónde enchufar logs reales (para operación continua)

`cost_meter.py` acepta cualquier mezcla de ficheros/directorios:

```
python3 cost_meter.py /home/codespace/complete-1512/sessions \
  /home/codespace/.dsh-harness-1512-complete/sessions \
  --json-out informe.json
```

- Detecta `*.jsonl` y `*.jsonl.zstd` recursivamente.
- Deduplica por id de sesión (evento `session.id`; si no existe, por UUID
  del directorio), para no contar dos veces la misma sesión en dos almacenes.
- Admite también líneas genéricas con formato DeepSeek crudo
  (`usage.prompt_tokens` / `prompt_cache_hit_tokens` / `completion_tokens`).
- Precios sobreescribibles con `--precios precios.json`.

## 3. Seguridad aplicada en el sondeo

- Nunca se imprimió contenido de sesiones: solo claves de esquema, tipos y
  valores numéricos de métricas (tokens, time, model).
- `request/header` se inspeccionó solo por nombres de clave (podía contener
  cabeceras de autorización): ningún valor leído ni mostrado.
- `settings.yaml` se leyó enmascarando claves/tokens (regex sobre
  key/token/secret/password/apiKey).

## 4. Conclusión

DATOS REALES ENCONTRADOS: SÍ — 845 sesiones históricas + almacenes vivos del
harness con 9.747+ eventos de uso facturable (deepseek-v4-pro), con cache
hits ya registrados. La métrica de coste se calcula sobre estos datos; los
detalles numéricos están en EVIDENCIA-COSTO-TOKEN.md / .json.
