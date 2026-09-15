# EVIDENCIA-COSTO-TOKEN — métrica de coste por token con uso REAL

Carril: ismatv1512 (nuevos-trabajos) · Harness 1512 (Linux/Codespace)
Pieza: medición exacta del gasto DeepSeek (prioridad de ahorro >=95%).
Pasada de datos: 2026-09-15 (UTC) · datos REALES (no fixtures).

## 1. Estado: LISTO

| Pregunta | Respuesta |
|---|---|
| ¿Datos reales? | **SÍ** — 1.741 ficheros de sesión del harness, 10.222 eventos de uso facturable |
| ¿Dónde? | `/home/codespace/complete-1512/sessions/` (845 históricas) + almacenes vivos `~/.dsh-harness-1512*/sessions/` |
| ¿Fixtures? | No fueron necesarios (los tests usan fixtures sintéticos etiquetados, los resultados usan datos reales) |
| Tests | 38/38 OK (objetivo >=20) |
| Modelo observado | 100% `deepseek-v4-pro` (precios oficiales confirmados) |
| Líneas rotas | 0 de 1.015.922 |
| Textos volcados | 0 (10.545 líneas con texto descartadas sin leer; 0 contenidos impresos) |

## 2. Metodología (verificada, no supuesta)

- **Esquema de uso real** (verificado en `@deepseek-ai/dsh-llm-deepseek/lib/index.js`,
  `mapUsage`, y en los propios ficheros): `inputTokens = prompt_tokens − cacheRead`
  (entrada NO cacheada), `cacheReadTokens` (entrada leída de caché),
  `outputTokens = completion_tokens` (ya INCLUYE razonamiento; `reasoningTokens`
  es informativo y no se factura dos veces).
- **Precios**: tarifa oficial consultada el 2026-09-15 en
  https://api-docs.deepseek.com/quick_start/pricing, con tarifa PICO/VALLE por
  hora UTC (pico lun–vie 01:00–04:00 y 06:00–10:00 UTC; valle = pico/2):
  deepseek-v4-pro: miss 1,32/0,66 · hit 0,044/0,022 · salida 3,96/1,98 USD por 1M.
  Modelos legacy (deepseek-chat/reasoner) incluidos en la tabla con marca
  `PRECIO_A_CONFIRMAR` (ya no figuran en la página oficial).
- **Fórmula de coste por evento**: `coste = miss/1M·p_miss(h) + hit/1M·p_hit(h) + out/1M·p_out(h)`.
- **Ahorro ya realizado por caché** (real, contra línea base sin caché):
  `ahorro = hit/1M · (p_miss − p_hit)`.
- **Proyección** (parámetros explícitos, sin promesas de calidad):
  `extra_caché = max(0, hit_rate·(miss+hit) − hit)/1M · (p_miss − p_hit)`;
  `ahorro_router = fraccion · (coste_v4pro − coste_flash)` por evento.
- **Unidad de trabajo**: id de sesión (UUID anónimo; 71 unidades). Dedupe entre
  almacenes por id de sesión: 19.473 eventos duplicados descartados.
- **Seguridad**: el parser lee SOLO campos métricos de lista blanca; los textos
  y cabeceras se descartan sin conservarse (10.545 líneas contabilizadas).

## 3. Agregados REALES (totales, 2026-09-03 → 2026-09-15 UTC)

| Métrica | Valor |
|---|---|
| Llamadas facturables | 10.222 (1.583 adicionales con usage=0, sesiones antiguas sin `request/context`) |
| Tokens entrada NO cacheada (miss) | 10.908.923 |
| Tokens leídos de CACHÉ (hit) | **2.392.805.632** (hit-rate real = 99,55%) |
| Tokens salida | 8.184.547 |
| Tokens facturados totales | 2.411.899.102 |
| **Coste total real** | **80,45 USD** |
| Coste por token | 3,3·10⁻⁸ USD (33 nanodólares/token) |
| Coste por llamada | 0,007871 USD |
| Llamadas sin precio | 0 (todo deepseek-v4-pro, precios confirmados) |

Por modelo: deepseek-v4-pro — 10.222 llamadas — miss 10.908.923 · hit
2.392.805.632 · out 8.184.547 — 80,45 USD (confirmado).

Por día (UTC):
| Día | Llamadas | miss | hit | out | Coste USD |
|---|---|---|---|---|---|
| 2026-09-09 | 1.120 | 460.642 | 259.760.512 | 900.164 | 9,00 |
| 2026-09-13 | 6.469 | 8.196.079 | 1.764.008.576 | 5.546.286 | 55,20 |
| 2026-09-14 | 1.256 | 754.286 | 321.437.056 | 1.144.260 | 9,84 |
| 2026-09-15 | 475 | 1.497.916 | 47.599.488 | 593.837 | 6,42 |
| otros días (03/08/10/11/12) | 902 | 0 | 0 | 0 | 0,00 |

Top unidades de trabajo (ids anónimos, coste USD): session-d38a35ed… 12,06 ·
session-6a38e9d6… 11,99 · session-76c5387b… 11,50 · session-a1928fc4… 5,63 ·
session-b38edf31… 4,91 (71 unidades en total; detalle completo en el JSON).

## 4. Ahorro MEDIDO (la cifra que demuestra el objetivo)

| Concepto | USD |
|---|---|
| Línea base SIN caché (todo input a precio miss) | 1.654,78 |
| Coste REAL con caché | 80,45 |
| **Ahorro ya realizado por el cacheo de contexto** | **1.574,33 USD** |
| **% de ahorro real** | **95,14% — cumple el objetivo >=95% del propietario** |

## 5. Proyección de ahorro adicional (escenarios explícitos)

| Escenario | Ahorro adicional USD | % sobre coste actual |
|---|---|---|
| cache_90 (hit_rate 90%) | 4,09 | 5,08% |
| cache_95 (hit_rate 95%) | 4,63 | 5,76% |
| cache_95 + router 10% v4-pro→flash | 11,21 | 13,93% |
| cache_95 + router 25% v4-pro→flash | 21,07 | 26,19% |

(El margen de caché es pequeño porque el hit-rate real ya es 99,55%;
el siguiente palanca medible es el ROUTING v4-pro→flash, +6,57 USD por cada
10% de tokens ruteados manteniendo calidad.)

Ejemplo numérico de una llamada (hora pico, v4-pro): 1M miss + 1M salida =
1,32 + 3,96 = **5,28 USD**; la misma con 90% del input desde caché =
0,1·1,32 + 0,9·0,044 + 3,96 = 4,21 USD → ahorro del 20,3% en esa llamada.

## 6. sha256 de los artefactos

| Fichero | sha256 |
|---|---|
| EVIDENCIA-COSTO-TOKEN.json | 4d6d6b0431692013462449a2c88b026af4949a46c34aa6f4696a1ec0ba9c0c0a |
| cost_meter.py | 5d71ec1c03e05d0b1219b994d290e4930d562d71b2d643f4454f4ae2edad780c |
| test_cost_meter.py | 4b7b1b578095062a70f6d213c9d74ec602afe22fc0cea31af2aaa6c4d7b6769d |
| HALLAZGOS-DATOS-REALES.md | 23ac62c6e751f1dc8b77f8f6611c19d311f8d7f8bd54e1a69b0c14fb8337820e |
| informe-real-completo.json (1ª pasada) | 421165d6a131e260a9c9515ffeb7353c981f71ff9b1d4c10565eb5344a942501 |

## 7. Qué falta para operación continua (y cómo enchufarlo)

1. **Automatizar la medición**: ejecutar `python3 cost_meter.py
   <almacenes> --json-out <informe>` desde el watchdog/hub de `never-stop`
   (una pasada cada N minutos; los almacenes vivos crecen en tiempo real:
   por eso las cifras varían ligeramente entre pasadas).
2. **Actualizar precios**: refrescar la tabla oficial si DeepSeek cambia
   tarifas (configurable con `--precios precios.json`).
3. **Unidad de trabajo = agente**: mapear `session.id` → id de agente
   anonimizado (los descriptores `subagent/descriptor` ya llevan
   `agentProvider`/`agentModel`); el meter ya agrupa por unidad.
4. **Precios legacy**: confirmar/eliminar `PRECIO_A_CONFIRMAR` de
   deepseek-chat/reasoner (no aparecen en los datos reales actuales).

## Conclusión

**LISTO**: la métrica de coste por token con uso REAL está implementada,
testeada (38/38) y aplicada a 1.741 ficheros reales. Resultado medido:
80,45 USD de coste real con 1.574,33 USD ya ahorrados por caché
(**95,14% de ahorro**, objetivo >=95% cumplido), más proyecciones
explícitas de +4 a +21 USD con caché/router. Nada de textos ni credenciales
se leyó, conservó o imprimió.

---
sha256 de este documento (sin incluir esta linea final): 977618b75e608c31b617e0bacf037550eae2526b8e2e7ac052506637d71d34db
