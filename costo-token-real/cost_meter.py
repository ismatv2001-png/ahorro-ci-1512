#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cost_meter.py — Métrica de coste por token con uso REAL del harness DeepSeek.

Pieza del carril ismatv1512 (nuevos-trabajos), prioridad de ahorro (>=95%).
Aporta la MEDICIÓN EXACTA en dólares que demuestra el ahorro del cacheo de
contexto y del routing de modelos, a partir de los registros de uso reales
del harness (sesiones en formato JSONL / JSONL.zstd).

PRINCIPIOS DE SEGURIDAD (inamovibles):
  * NUNCA conserva ni imprime textos: el parser lee SOLO campos métricos de
    una lista blanca (nº de tokens, modelo, timestamps, ids anónimos).
    Cualquier otro campo (prompts, respuestas, cabeceras, claves) se
    descarta en el acto y solo se cuenta como "descartado".
  * Trabaja con conteos agregados (totales, por modelo, por día, por unidad).

SEMÁNTICA DE USO DEL HARNESS (verificada en el código del adaptador
@deepseek-ai/dsh-llm-deepseek, función mapUsage, 2026-09-15):
  inputTokens      = prompt_tokens - cacheRead   -> entrada NO cacheada (miss)
  cacheReadTokens  = prompt_cache_hit_tokens     -> entrada leída de caché (hit)
  cacheWriteTokens = escrituras de caché (informativas; ya incluidas en input)
  outputTokens     = completion_tokens (INCLUYE tokens de razonamiento)
  reasoningTokens  = completion_tokens_details.reasoning_tokens (informativo,
                     subconjunto de outputTokens; NO se suma de nuevo)

COSTE FACTURABLE (por evento):
  coste = input/1M * precio_miss(hora) + cacheRead/1M * precio_hit(hora)
          + output/1M * precio_out(hora)
donde precio(hora) elige tarifa PICO o VALLE según la hora UTC del evento.

AHORRO YA REALIZADO POR CACHÉ (real, contra línea base sin caché):
  ahorro_cache = cacheRead/1M * (precio_miss - precio_hit)

PROYECCIÓN DE AHORRO ADICIONAL (parámetros explícitos, sin promesas):
  * hit_rate_objetivo  : fracción de entrada total que se serviría de caché.
    tokens_adicionales_caché = max(0, hit_rate * (input + cacheRead) - cacheRead)
    ahorro_adicional  = tokens_adicionales / 1M * (precio_miss - precio_hit)
  * fraccion_router    : fracción de tokens del modelo caro (v4-pro) que se
    podrían rutar al modelo barato (flash) sin perder calidad.
    ahorro_router = fraccion * (coste_v4pro - coste_flash), evento a evento.

PRECIOS: tabla configurable (PRECIOS por defecto = tarifa oficial DeepSeek
consultada el 2026-09-15 en https://api-docs.deepseek.com/quick_start/pricing).
Los precios de modelos legacy (deepseek-chat/reasoner) ya no figuran en la
página oficial: se marcan con la constante PRECIO_A_CONFIRMAR y
"confirmado": false. Se pueden sobreescribir con un JSON vía --precios.

Solo stdlib. Para leer *.jsonl.zstd se invoca el binario `zstd` (subprocess);
si no existe, esos ficheros se cuentan como no leídos (sin datos inventados).
"""

import argparse
import datetime
import hashlib
import json
import math
import os
import subprocess
import sys
from collections import Counter, defaultdict

__version__ = "1.0.0"

# --------------------------------------------------------------------------
# Constantes de precios
# --------------------------------------------------------------------------

PRECIO_A_CONFIRMAR = "PRECIO_A_CONFIRMAR"

FUENTE_PRECIOS = "https://api-docs.deepseek.com/quick_start/pricing (consulta 2026-09-15)"

# Tarifa oficial actual (USD por 1M tokens). Pico/valle según hora UTC.
# Pico: lun-vie 01:00-04:00 y 06:00-10:00 UTC; resto = valle (valle = pico/2).
PRECIOS_OFICIALES = {
    "deepseek-v4-pro": {
        "input_miss": {"pico": 1.32, "valle": 0.66},
        "cache_hit": {"pico": 0.044, "valle": 0.022},
        "output": {"pico": 3.96, "valle": 1.98},
        "confirmado": True,
        "origen": FUENTE_PRECIOS,
    },
    "deepseek-flash": {
        "input_miss": {"pico": 0.30, "valle": 0.15},
        "cache_hit": {"pico": 0.006, "valle": 0.003},
        "output": {"pico": 1.20, "valle": 0.60},
        "confirmado": True,
        "origen": FUENTE_PRECIOS,
    },
    # Precios legacy (DeepSeek-V3/R1). Ya NO figuran en la página oficial
    # (2026): PRECIO_A_CONFIRMAR antes de usarlos en facturación.
    "deepseek-chat": {
        "input_miss": 0.27, "cache_hit": 0.07, "output": 1.10,
        "confirmado": False,
        "origen": "tarifa historica V3 (PRECIO_A_CONFIRMAR)",
    },
    "deepseek-reasoner": {
        "input_miss": 0.55, "cache_hit": 0.14, "output": 2.19,
        "confirmado": False,
        "origen": "tarifa historica R1 (PRECIO_A_CONFIRMAR)",
    },
}

# Alias documentados por DeepSeek: los nombres retirados se facturan al
# precio del modelo que los sirve.
ALIAS_MODELO = {
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-v4-flash-vision-exp": "deepseek-flash",
}

# Ventanas pico oficiales (UTC, lunes-viernes): [1,4) y [6,10) horas.
PICO_VENTANAS = ((1, 4), (6, 10))

MODELO_POR_DEFECTO = "deepseek-v4-pro"  # agent-default-model del harness


# --------------------------------------------------------------------------
# Estructuras de datos
# --------------------------------------------------------------------------

class EventoUso:
    """Un evento de uso facturable. NINGÚN texto se almacena aquí."""
    __slots__ = ("modelo", "ts_ms", "input_tokens", "cache_hit_tokens",
                 "cache_write_tokens", "output_tokens", "reasoning_tokens",
                 "unidad", "origen_modelo", "flags")

    def __init__(self, modelo, ts_ms, input_tokens=0, cache_hit_tokens=0,
                 cache_write_tokens=0, output_tokens=0, reasoning_tokens=0,
                 unidad="desconocida", origen_modelo="desconocido",
                 flags=None):
        self.modelo = modelo
        self.ts_ms = ts_ms
        self.input_tokens = input_tokens
        self.cache_hit_tokens = cache_hit_tokens
        self.cache_write_tokens = cache_write_tokens
        self.output_tokens = output_tokens
        self.reasoning_tokens = reasoning_tokens
        self.unidad = unidad
        self.origen_modelo = origen_modelo
        self.flags = list(flags or [])

    @property
    def prompt_total_tokens(self):
        return self.input_tokens + self.cache_hit_tokens

    @property
    def tokens_facturados(self):
        return self.prompt_total_tokens + self.output_tokens

    def fecha_utc(self):
        if self.ts_ms is None:
            return "sin-fecha"
        return datetime.datetime.fromtimestamp(
            self.ts_ms / 1000.0, datetime.timezone.utc
        ).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
# Normalización y lectura de campos métricos (lista blanca estricta)
# --------------------------------------------------------------------------

def _entero_no_negativo(v):
    """Coerce a int >= 0. Devuelve (valor, ok)."""
    if v is None:
        return 0, False
    if isinstance(v, bool):
        return 0, False
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return 0, False
    if n < 0:
        return 0, False
    return n, True


def _ts_ms_de(v):
    """Normaliza un timestamp (ms) desde valor o lista de candidatos."""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f <= 0:
        return None
    if f < 1e12:  # segundos -> ms
        f *= 1000.0
    return int(f)


def normalizar_modelo(nombre):
    """Normaliza y resuelve alias de nombre de modelo. Sin modelo -> None."""
    if not isinstance(nombre, str):
        return None
    m = nombre.strip().lower()
    if not m:
        return None
    return ALIAS_MODELO.get(m, m)


def parsear_linea_uso(linea, *, modelo_actual=None, unidad="desconocida",
                      modelo_por_defecto=MODELO_POR_DEFECTO):
    """Parsea UNA línea JSON de un log de uso.

    Acepta dos formatos:
      A) Evento del harness: {"type":"assistant/message","time":ms,
         "data":{"model"?:..., "usage":{"inputTokens","outputTokens",
         "cacheReadTokens","cacheWriteTokens","reasoningTokens"}}}
         y actualiza el modelo vigente con {"type":"request/context",
         "data":{"model":...}}.
      B) Línea genérica DeepSeek: {"model":..., "ts"|"timestamp"|"created":...,
         "usage":{"prompt_tokens","completion_tokens",
         "prompt_cache_hit_tokens","prompt_cache_miss_tokens", ...}}.

    Devuelve (evento|None, accion, flags_linea). 'accion' indica qué se hizo:
    'uso', 'contexto', 'ignorada', 'rota', 'sin-uso'.
    NUNCA se conserva ningún texto ni campo fuera de la lista blanca métrica.
    """
    try:
        d = json.loads(linea)
    except (ValueError, TypeError):
        return None, "rota", []

    if not isinstance(d, dict):
        return None, "ignorada", []

    tipo = d.get("type")
    data = d.get("data")
    flags = []

    # ---- Formato A (eventos del harness) --------------------------------
    if tipo == "request/context" and isinstance(data, dict):
        m = normalizar_modelo(data.get("model"))
        # marcador: devolvemos evento None con accion 'contexto' y el modelo
        return {"modelo": m}, "contexto", flags

    if tipo == "assistant/message" and isinstance(data, dict):
        uso = data.get("usage")
        if not isinstance(uso, dict):
            return None, "sin-uso", flags
        inp, ok1 = _entero_no_negativo(uso.get("inputTokens"))
        outp, ok2 = _entero_no_negativo(uso.get("outputTokens"))
        hit, ok3 = _entero_no_negativo(uso.get("cacheReadTokens"))
        write, ok4 = _entero_no_negativo(uso.get("cacheWriteTokens"))
        reas, ok5 = _entero_no_negativo(uso.get("reasoningTokens"))
        if not all((ok1, ok2, ok3, ok4, ok5)):
            flags.append("campos_uso_parciales")
        modelo = normalizar_modelo(data.get("model")) or modelo_actual or modelo_por_defecto
        origen = "explicito" if isinstance(data.get("model"), str) else (
            "contexto" if modelo_actual else "por-defecto")
        ev = EventoUso(modelo, _ts_ms_de(d.get("time")), inp, hit, write,
                       outp, reas, unidad=unidad, origen_modelo=origen,
                       flags=flags)
        return ev, "uso", flags

    if isinstance(tipo, str):
        # resto de eventos del harness (chunks, tool/call, headers, ...):
        # se descartan SIN leer su contenido.
        return None, "ignorada", []

    # ---- Formato B (línea genérica con 'usage') -------------------------
    uso = d.get("usage")
    if isinstance(uso, dict):
        prompt = None
        miss = None
        hit = None
        p, _ = _entero_no_negativo(uso.get("prompt_tokens"))
        h, _ = _entero_no_negativo(
            uso.get("prompt_cache_hit_tokens",
                    uso.get("prompt_tokens_details", {}).get("cached_tokens")
                    if isinstance(uso.get("prompt_tokens_details"), dict)
                    else None))
        m_, _ = _entero_no_negativo(uso.get("prompt_cache_miss_tokens"))
        c, _ = _entero_no_negativo(uso.get("completion_tokens"))
        if uso.get("prompt_cache_miss_tokens") is not None:
            miss = m_
            if h is None:
                h = max(0, p - m_)
        elif p is not None:
            miss = max(0, p - (h or 0))
        if miss is None:
            miss = 0
        ts = _ts_ms_de(d.get("ts", d.get("timestamp", d.get("created"))))
        modelo = normalizar_modelo(d.get("model")) or modelo_actual or modelo_por_defecto
        ev = EventoUso(modelo, ts, miss, h or 0, 0, c, 0, unidad=unidad,
                       origen_modelo="explicito", flags=flags)
        return ev, "uso", flags

    return None, "ignorada", []


# --------------------------------------------------------------------------
# Lectura de ficheros de sesión (JSONL / JSONL.zstd) y de directorios
# --------------------------------------------------------------------------

def _descomprimir_zstd(path):
    """Devuelve texto descomprimido vía binario zstd (stdlib subprocess)."""
    proc = subprocess.run(["zstd", "-dc", path], capture_output=True,
                          timeout=300)
    if proc.returncode != 0:
        raise OSError("zstd fallo con codigo %s" % proc.returncode)
    return proc.stdout.decode("utf-8", "replace")


def unidad_desde_path(path):
    """Unidad de trabajo anonimizada: UUID de sesión si existe; si no,
    hash sha256[:16] del nombre (la ruta NUNCA se conserva en claro)."""
    base = os.path.basename(os.path.dirname(path)) or os.path.basename(path)
    if base.startswith("session-") or base.startswith(".session-"):
        base = base[1:] if base.startswith(".") else base
    # los ids de sesión del harness son UUIDs (ya anónimos)
    import re
    if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", base) or \
       base.startswith("session-"):
        return base
    return "u-" + hashlib.sha256(base.encode("utf-8", "replace")).hexdigest()[:16]


def leer_fichero_sesion(path, *, modelo_por_defecto=MODELO_POR_DEFECTO):
    """Lee un fichero de sesión (JSONL plano o .zstd) y devuelve
    (eventos, estadisticas). Los textos se descartan línea a línea;
    'descartados_con_texto' cuenta líneas que CONTENÍAN texto y no se
    conservó (evidencia de no-volcado)."""
    stats = {"lineas": 0, "rotas": 0, "ignoradas": 0, "sin_uso": 0,
             "contextos": 0, "descartados_con_texto": 0, "zstd": False}
    eventos = []
    try:
        if path.endswith(".zstd"):
            stats["zstd"] = True
            texto = _descomprimir_zstd(path)
        else:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                texto = fh.read()
    except OSError as e:
        stats["error"] = type(e).__name__
        return eventos, stats

    unidad = unidad_desde_path(path)
    # si existe evento 'session' con id propio, ese id es la unidad real
    unidad_explicita = None
    modelo_actual = None
    for linea in texto.splitlines():
        if not linea.strip():
            continue
        stats["lineas"] += 1
        try:
            d = json.loads(linea)
        except ValueError:
            stats["rotas"] += 1
            continue
        if isinstance(d, dict) and d.get("type") == "session":
            sid = d.get("id")
            if isinstance(sid, str) and sid:
                unidad_explicita = sid
        res, accion, _fl = parsear_linea_uso(
            linea, modelo_actual=modelo_actual, unidad=unidad,
            modelo_por_defecto=modelo_por_defecto)
        if accion == "contexto":
            stats["contextos"] += 1
            if isinstance(res, dict) and res.get("modelo"):
                modelo_actual = res["modelo"]
            continue
        if accion == "rota":
            stats["rotas"] += 1
            continue
        if accion == "ignorada":
            stats["ignoradas"] += 1
            # ¿contenía texto que NO conservamos? (evidencia anti-volcado)
            if isinstance(d, dict):
                data = d.get("data")
                if isinstance(data, dict) and ("text" in data or
                                               "content" in data or
                                               "header" in data):
                    stats["descartados_con_texto"] += 1
            continue
        if accion == "sin-uso":
            stats["sin_uso"] += 1
            continue
        if accion == "uso" and res is not None:
            if unidad_explicita:
                res.unidad = unidad_explicita
            eventos.append(res)
    return eventos, stats


def leer_rutas(rutas, *, modelo_por_defecto=MODELO_POR_DEFECTO,
               deduplicar_unidades=True):
    """Lee ficheros o directorios (búsqueda recursiva de *.jsonl / *.zstd).
    Deduplica por unidad de trabajo (misma sesión en dos almacenes cuenta
    una vez). Devuelve (eventos, estadisticas_globales)."""
    ficheros = []
    for r in rutas:
        if os.path.isdir(r):
            for root, _dirs, files in os.walk(r):
                for f in files:
                    if f.endswith(".jsonl") or f.endswith(".jsonl.zstd"):
                        ficheros.append(os.path.join(root, f))
        elif os.path.isfile(r):
            ficheros.append(r)
    ficheros = sorted(set(ficheros))

    eventos = []
    stats = defaultdict(int)
    stats["ficheros_totales"] = len(ficheros)
    stats["ficheros_leidos"] = 0
    stats["ficheros_error"] = 0
    vistos = set()  # unidades vistas en ficheros ANTERIORES (dedupe)
    for p in ficheros:
        evs, st = leer_fichero_sesion(p, modelo_por_defecto=modelo_por_defecto)
        if "error" in st:
            stats["ficheros_error"] += 1
            continue
        stats["ficheros_leidos"] += 1
        for k, v in st.items():
            stats[k] += v
        unidades_fichero = set()
        for ev in evs:
            if deduplicar_unidades and ev.unidad in vistos:
                stats["eventos_duplicados_descartados"] += 1
                continue
            eventos.append(ev)
            unidades_fichero.add(ev.unidad)
        vistos.update(unidades_fichero)
    stats["eventos_uso"] = len(eventos)
    return eventos, dict(stats)


# --------------------------------------------------------------------------
# Precios y horario pico/valle
# --------------------------------------------------------------------------

def cargar_precios(ruta_json=None):
    """Tabla de precios: la oficial por defecto + sobreescritura opcional."""
    import copy
    precios = copy.deepcopy(PRECIOS_OFICIALES)
    if ruta_json:
        with open(ruta_json, "r", encoding="utf-8") as fh:
            extra = json.load(fh)
        for modelo, p in extra.items():
            if modelo in precios:
                precios[modelo].update(p)
            else:
                precios[modelo] = p
    return precios


def es_hora_pico(ts_ms):
    """True si el timestamp cae en ventanas pico oficiales (UTC, lun-vie)."""
    if ts_ms is None:
        return None
    dt = datetime.datetime.fromtimestamp(ts_ms / 1000.0,
                                         datetime.timezone.utc)
    if dt.weekday() >= 5:  # sáb/dom -> valle
        return False
    h = dt.hour + dt.minute / 60.0 + dt.second / 3600.0
    return any(inicio <= h < fin for inicio, fin in PICO_VENTANAS)


def _tarifa(entrada, pico):
    """Extrae el precio USD/M de una entrada de tabla (escalar o pico/valle)."""
    if isinstance(entrada, dict):
        clave = "pico" if pico else "valle"
        if clave in entrada:
            return entrada[clave]
        return None
    if isinstance(entrada, (int, float)):
        return float(entrada)
    return None


def precio_evento(precios, modelo, ts_ms):
    """Devuelve (dict de precios USD/M, flags) para un evento.
    Si el modelo no tiene precio: (None, [flag])."""
    m = normalizar_modelo(modelo)
    if m not in precios:
        return None, ["modelo_sin_precio"]
    p = precios[m]
    pico = es_hora_pico(ts_ms)
    flags = []
    if pico is None:
        pico = False
        flags.append("sin_timestamp_valle")
    res = {
        "input_miss": _tarifa(p.get("input_miss"), pico),
        "cache_hit": _tarifa(p.get("cache_hit"), pico),
        "output": _tarifa(p.get("output"), pico),
        "pico": bool(pico),
        "confirmado": bool(p.get("confirmado", False)),
        "origen": p.get("origen", "personalizado"),
    }
    if res["input_miss"] is None or res["output"] is None:
        return None, ["modelo_sin_precio"]
    if res["cache_hit"] is None:
        res["cache_hit"] = res["input_miss"]
        flags.append("sin_precio_cache_usa_miss")
    if not res["confirmado"]:
        flags.append(PRECIO_A_CONFIRMAR)
    return res, flags


def coste_evento(evento, precios):
    """Coste USD de un evento. Devuelve (coste|None, detalle|None, flags)."""
    pre, flags = precio_evento(precios, evento.modelo, evento.ts_ms)
    if pre is None:
        return None, None, flags
    M = 1_000_000.0
    coste = (
        evento.input_tokens / M * pre["input_miss"]
        + evento.cache_hit_tokens / M * pre["cache_hit"]
        + evento.output_tokens / M * pre["output"]
    )
    detalle = {
        "input_miss_usd": evento.input_tokens / M * pre["input_miss"],
        "cache_hit_usd": evento.cache_hit_tokens / M * pre["cache_hit"],
        "output_usd": evento.output_tokens / M * pre["output"],
        "pico": pre["pico"],
    }
    return coste, detalle, flags


# --------------------------------------------------------------------------
# Agregación (solo conteos y sumas; jamás textos)
# --------------------------------------------------------------------------

def agregar(eventos, precios):
    """Agrega eventos en totales, por modelo, por día y por unidad.
    Devuelve un dict JSON-serializable."""
    totales = {
        "llamadas": 0, "tokens_input_miss": 0, "tokens_cache_hit": 0,
        "tokens_output": 0, "tokens_facturados": 0,
        "coste_usd": 0.0, "llamadas_sin_precio": 0,
        "tokens_sin_precio": 0, "flags": Counter(),
    }
    por_modelo = defaultdict(lambda: {
        "llamadas": 0, "tokens_input_miss": 0, "tokens_cache_hit": 0,
        "tokens_output": 0, "coste_usd": 0.0, "confirmado": None,
        "origen": None, "flags": Counter()})
    por_dia = defaultdict(lambda: {
        "llamadas": 0, "tokens_input_miss": 0, "tokens_cache_hit": 0,
        "tokens_output": 0, "coste_usd": 0.0})
    por_unidad = defaultdict(lambda: {
        "llamadas": 0, "tokens_facturados": 0, "coste_usd": 0.0})

    for ev in eventos:
        coste, _det, flags = coste_evento(ev, precios)
        # totales
        totales["llamadas"] += 1
        totales["tokens_input_miss"] += ev.input_tokens
        totales["tokens_cache_hit"] += ev.cache_hit_tokens
        totales["tokens_output"] += ev.output_tokens
        totales["tokens_facturados"] += ev.tokens_facturados
        if coste is None:
            totales["llamadas_sin_precio"] += 1
            totales["tokens_sin_precio"] += ev.tokens_facturados
        else:
            totales["coste_usd"] += coste
        for f in flags:
            totales["flags"][f] += 1
        # por modelo
        pm = por_modelo[ev.modelo]
        pm["llamadas"] += 1
        pm["tokens_input_miss"] += ev.input_tokens
        pm["tokens_cache_hit"] += ev.cache_hit_tokens
        pm["tokens_output"] += ev.output_tokens
        if coste is not None:
            pm["coste_usd"] += coste
        if pm["confirmado"] is None:
            p, _ = precio_evento(precios, ev.modelo, ev.ts_ms)
            pm["confirmado"] = bool(p["confirmado"]) if p else None
            pm["origen"] = p["origen"] if p else None
        for f in flags:
            pm["flags"][f] += 1
        # por día
        pd = por_dia[ev.fecha_utc()]
        pd["llamadas"] += 1
        pd["tokens_input_miss"] += ev.input_tokens
        pd["tokens_cache_hit"] += ev.cache_hit_tokens
        pd["tokens_output"] += ev.output_tokens
        if coste is not None:
            pd["coste_usd"] += coste
        # por unidad
        pu = por_unidad[ev.unidad]
        pu["llamadas"] += 1
        pu["tokens_facturados"] += ev.tokens_facturados
        if coste is not None:
            pu["coste_usd"] += coste

    def cerrar(d):
        d = dict(d)
        for k, v in d.items():
            if isinstance(v, Counter):
                d[k] = dict(v)
        return d

    unidades = sorted(por_unidad.items(), key=lambda kv: -kv[1]["coste_usd"])
    resumen_unidades = {
        "n_unidades": len(por_unidad),
        "top_10": [{"unidad": u, **cerrar(v)} for u, v in unidades[:10]],
    }

    totales["usd_por_token"] = (
        totales["coste_usd"] /
        (totales["tokens_facturados"] - totales["tokens_sin_precio"])
        if (totales["tokens_facturados"] - totales["tokens_sin_precio"]) > 0
        else None)
    totales["usd_por_llamada"] = (
        totales["coste_usd"] /
        (totales["llamadas"] - totales["llamadas_sin_precio"])
        if (totales["llamadas"] - totales["llamadas_sin_precio"]) > 0
        else None)

    return {
        "totales": cerrar(totales),
        "por_modelo": {m: cerrar(v) for m, v in sorted(por_modelo.items())},
        "por_dia": {d: cerrar(v) for d, v in sorted(por_dia.items())},
        "por_unidad": resumen_unidades,
    }


# --------------------------------------------------------------------------
# Ahorro: realizado (cache real) y proyección (cache + router)
# --------------------------------------------------------------------------

def ahorro_cache_realizado(eventos, precios):
    """Ahorro real ya obtenido por el cacheo de contexto frente a una
    línea base sin caché. Fórmula por evento:
        cacheRead/1M * (precio_miss - precio_hit)."""
    total = 0.0
    por_modelo = defaultdict(float)
    for ev in eventos:
        pre, _fl = precio_evento(precios, ev.modelo, ev.ts_ms)
        if pre is None:
            continue
        a = ev.cache_hit_tokens / 1_000_000.0 * (
            pre["input_miss"] - pre["cache_hit"])
        total += a
        por_modelo[ev.modelo] += a
    return {"ahorro_cache_realizado_usd": total,
            "por_modelo": dict(por_modelo)}


def proyeccion_ahorro(eventos, precios, *, hit_rate_objetivo=0.90,
                      fraccion_router=0.0,
                      modelo_caro="deepseek-v4-pro",
                      modelo_barato="deepseek-flash"):
    """Proyección de ahorro adicional con parámetros EXPLÍCITOS.

    hit_rate_objetivo: fracción de entrada total servida desde caché.
      tokens_extra = max(0, hit_rate*(input+cacheRead) - cacheRead)
      ahorro_extra = tokens_extra/1M * (precio_miss - precio_hit)
    fraccion_router: fracción de tokens del modelo caro rutable al barato.
      ahorro_router = fraccion * (coste_caro - coste_barato), por evento.
    Devuelve dict con fórmulas y resultados (sin promesas de calidad)."""
    ahorro_cache_extra = 0.0
    ahorro_router = 0.0
    tokens_input_total = 0
    tokens_cache_total = 0
    n_eventos_caros = 0
    for ev in eventos:
        pre, _fl = precio_evento(precios, ev.modelo, ev.ts_ms)
        if pre is None:
            continue
        tokens_input_total += ev.input_tokens
        tokens_cache_total += ev.cache_hit_tokens
        total_in = ev.prompt_total_tokens
        extra = max(0.0, hit_rate_objetivo * total_in - ev.cache_hit_tokens)
        ahorro_cache_extra += extra / 1_000_000.0 * (
            pre["input_miss"] - pre["cache_hit"])
        if normalizar_modelo(ev.modelo) == modelo_caro:
            pre_bar, _ = precio_evento(precios, modelo_barato, ev.ts_ms)
            if pre_bar is not None:
                coste_caro = (ev.input_tokens / 1e6 * pre["input_miss"]
                              + ev.cache_hit_tokens / 1e6 * pre["cache_hit"]
                              + ev.output_tokens / 1e6 * pre["output"])
                coste_barato = (ev.input_tokens / 1e6 * pre_bar["input_miss"]
                                + ev.cache_hit_tokens / 1e6 * pre_bar["cache_hit"]
                                + ev.output_tokens / 1e6 * pre_bar["output"])
                ahorro_router += fraccion_router * max(0.0, coste_caro - coste_barato)
                n_eventos_caros += 1
    return {
        "parametros": {
            "hit_rate_objetivo": hit_rate_objetivo,
            "fraccion_router": fraccion_router,
            "modelo_caro": modelo_caro,
            "modelo_barato": modelo_barato,
        },
        "formula_ahorro_cache": "max(0, hit_rate*(input+cacheRead)-cacheRead)/1M*(precio_miss-precio_hit)",
        "formula_ahorro_router": "fraccion_router*(coste_modelo_caro-coste_modelo_barato)",
        "ahorro_cache_adicional_usd": ahorro_cache_extra,
        "ahorro_router_usd": ahorro_router,
        "ahorro_proyectado_total_usd": ahorro_cache_extra + ahorro_router,
        "tokens_input_considerados": tokens_input_total,
        "tokens_cache_considerados": tokens_cache_total,
        "eventos_modelo_caro": n_eventos_caros,
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _resumen_texto(rep, proy, ahorro, stats, fuentes):
    """Resumen agregado en texto plano (NUNCA contiene textos de usuario)."""
    t = rep["totales"]
    lineas = []
    lineas.append("== COST-METER (agregados, sin textos) ==")
    lineas.append("fuentes: " + ", ".join(fuentes))
    lineas.append("ficheros leidos: %d de %d (errores: %d)"
                  % (stats.get("ficheros_leidos", 0),
                     stats.get("ficheros_totales", 0),
                     stats.get("ficheros_error", 0)))
    lineas.append("lineas: %d | rotas: %d | eventos uso: %d | descartados con texto: %d"
                  % (stats.get("lineas", 0), stats.get("rotas", 0),
                     t["llamadas"], stats.get("descartados_con_texto", 0)))
    lineas.append("TOTAL: %d llamadas | input(miss)=%s hit=%s out=%s | coste=%.6f USD"
                  % (t["llamadas"], t["tokens_input_miss"],
                     t["tokens_cache_hit"], t["tokens_output"], t["coste_usd"]))
    if t["usd_por_token"] is not None:
        lineas.append("USD/token=%.8f | USD/llamada=%.6f" %
                      (t["usd_por_token"], t["usd_por_llamada"]))
    if t["llamadas_sin_precio"]:
        lineas.append("AVISO: %d llamadas sin precio (%d tokens) excluidas del coste"
                      % (t["llamadas_sin_precio"], t["tokens_sin_precio"]))
    lineas.append("por modelo:")
    for m, v in rep["por_modelo"].items():
        marca = "" if v.get("confirmado") else " " + PRECIO_A_CONFIRMAR
        lineas.append("  %-24s %6d llamadas | in=%s hit=%s out=%s | %.6f USD%s"
                      % (m, v["llamadas"], v["tokens_input_miss"],
                         v["tokens_cache_hit"], v["tokens_output"],
                         v["coste_usd"], marca))
    lineas.append("por dia (UTC):")
    for d, v in rep["por_dia"].items():
        lineas.append("  %s %6d llamadas | in=%s hit=%s out=%s | %.6f USD"
                      % (d, v["llamadas"], v["tokens_input_miss"],
                         v["tokens_cache_hit"], v["tokens_output"],
                         v["coste_usd"]))
    lineas.append("ahorro cache YA realizado: %.6f USD" %
                  ahorro["ahorro_cache_realizado_usd"])
    lineas.append("proyeccion adicional (hit_rate=%.2f, router=%.2f): %.6f USD"
                  % (proy["parametros"]["hit_rate_objetivo"],
                     proy["parametros"]["fraccion_router"],
                     proy["ahorro_proyectado_total_usd"]))
    return "\n".join(lineas)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Metrica de coste por token con uso real (sin textos).")
    ap.add_argument("rutas", nargs="+",
                    help="ficheros .jsonl/.zstd o directorios a escanear")
    ap.add_argument("--precios", default=None,
                    help="JSON de precios que sobreescribe la tabla oficial")
    ap.add_argument("--hit-rate-objetivo", type=float, default=0.90)
    ap.add_argument("--fraccion-router", type=float, default=0.0)
    ap.add_argument("--json-out", default=None,
                    help="escribe el informe completo (agregados) en JSON")
    ap.add_argument("--modelo-por-defecto", default=MODELO_POR_DEFECTO)
    args = ap.parse_args(argv)

    eventos, stats = leer_rutas(
        args.rutas, modelo_por_defecto=args.modelo_por_defecto)
    precios = cargar_precios(args.precios)
    rep = agregar(eventos, precios)
    ahorro = ahorro_cache_realizado(eventos, precios)
    proy = proyeccion_ahorro(eventos, precios,
                             hit_rate_objetivo=args.hit_rate_objetivo,
                             fraccion_router=args.fraccion_router)
    informe = {"generado_utc": datetime.datetime.now(
        datetime.timezone.utc).isoformat(),
        "fuentes": args.rutas, "estadisticas": stats, "precios": precios,
        "agregados": rep, "ahorro": ahorro, "proyeccion": proy,
        "version": __version__}
    texto = _resumen_texto(rep, proy, ahorro, stats, args.rutas)
    print(texto)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(informe, fh, indent=2, ensure_ascii=False,
                      default=lambda o: list(o) if isinstance(o, set) else str(o))
        print("informe JSON ->", args.json_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
