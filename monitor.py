#!/usr/bin/env python3
"""
Monitor P2P USDT/BOB (Binance + Bybit)

Cada ejecución (GitHub Actions, cada ~5 min):
  1. Lee los anuncios P2P de Binance y Bybit (precio, volumen, calidad del anunciante).
  2. Guarda el historial y calcula análisis técnico (RSI, MACD, EMA, Bollinger, soporte/resistencia).
  3. Lee noticias de Bolivia (Google News RSS) y puntúa su efecto probable sobre el dólar.
  4. Detecta oportunidades: arbitraje Binance<->Bybit, margen como maker/taker (con comisiones).
  5. Envía alertas por Telegram y deja los datos listos para el panel web.

Solo usa la biblioteca estándar de Python.

Uso:
  python monitor.py --data datadir          # ejecución normal
  python monitor.py --demo --data /tmp/x    # datos simulados, sin red ni Telegram
  python monitor.py --dry-run --data datadir  # imprime las alertas en vez de enviarlas
"""
import argparse
import hashlib
import html as htmllib
import json
import math
import os
import random
import re
import statistics
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
try:  # retroalimentación semanal: si falta feedback.py el monitor sigue funcionando sin ella
    import feedback as FB
except Exception as _e:  # noqa: BLE001
    FB = None
    print("feedback.py no disponible:", _e, file=sys.stderr)
LPZ = timezone(timedelta(hours=-4))  # Bolivia

DEFAULTS = {
    "fee_binance_pct": 0.25,
    "fee_bybit_pct": 0.0,
    "capital_bob": 15000,
    "capital_usdt": 300,
    "trade_amount_bob": 3000,
    "usdt_transfer_fee": 1.0,
    "transfer_size_usdt": 300,
    "min_monthly_orders": 30,
    "min_finish_rate": 0.9,
    "exact_mode": True,
    "min_usdt_available": 30,
    "max_order_usdt_trivial": 9,
    "maker_rank": 3,
    "history_max": 4000,
    "live_seconds": 420,
    "live_refresh_seconds": 60,
    "alerts": {
        "move_pct_30m": 0.5,
        "move_pct_2h": 0.9,
        "arb_net_pct": 0.3,
        "maker_net_pct": 0.3,
        "taker_net_pct": 0.2,
        "price_above": None,
        "price_below": None,
        "rsi_overbought": 70,
        "rsi_oversold": 30,
        "cooldown_min": {"move": 45, "arb": 30, "maker": 60, "taker": 30,
                         "signal": 120, "level": 120, "news": 360, "rule": 0,
                         "zone": 120, "breakout": 120, "bollinger": 180, "daily": 0, "weekly": 0},
        "repeat_kinds": ["rule", "zone", "breakout", "maker"],  # se repiten para que no pasen desapercibidas
        "breakout_margin_pct": 0.10,     # cuánto debe pasar el piso/techo de 3 días para llamarlo ruptura
        "bollinger_min_width_pct": 0.25, # ancho mínimo de las bandas (si el precio está muy quieto, no avisa)
        "daily_hour": 7,                 # hora local (Bolivia) del resumen diario
        "repeat_times": 3,             # mensajes en total (el primero + repeticiones)
        "repeat_seconds": 15,          # segundos entre mensajes
    },
    "scenario_weights": {"rsi": 15, "macd": 20, "tendencia": 20, "momentum": 15, "profundidad": 10, "noticias": 20},
    "feedback": {"enabled": True, "horizon_h": 6, "long_horizon_h": 24, "move_thr_pct": 0.20, "long_thr_pct": 0.40,
                 "min_samples": 8, "weekday": 5, "hour": 8, "keep_days": 45},
    "news": {
        "cat_weight": {},   # multiplicador por categoría en el escenario, ej. {"freight": 0.5}
        "enabled": True, "max_age_hours": 72, "max_items": 6, "min_score": 7, "important_score": 11,
        "alert_max_age_hours": 6, "alert_gap_min": 90, "max_article_fetches": 10,
        # secciones de economía que se leen directamente (prioridad máxima)
        "sections": [
            {"name": "El Deber · Economía", "url": "https://eldeber.com.bo/economia", "domain": "eldeber.com.bo",
             "base": "https://eldeber.com.bo", "link_re": r"((?:https://eldeber\.com\.bo)?/(?:economia|dinero)/[a-z0-9\-]+_\d{9,12})(?=[\"'\s<?#)])"},
            {"name": "Red Uno · Economía", "url": "https://www.reduno.com.bo/economia", "domain": "reduno.com.bo",
             "base": "https://www.reduno.com.bo", "link_re": r"((?:https://www\.reduno\.com\.bo)?/economia/[a-z0-9\-]+-\d{12,14})(?=[\"'\s<?#)])"},
            {"name": "Unitel · Economía", "url": "https://unitel.bo/noticias/economia", "domain": "unitel.bo",
             "base": "https://unitel.bo", "link_re": r"((?:https://unitel\.bo)?/noticias/economia/[a-z0-9\-]+-[A-Z]{2}\d{6,10})(?=[\"'\s<?#)])"},
        ],
        # búsquedas complementarias (Google Noticias): UIF, ASFI, P2P, préstamos, petróleo, flete, etc.
        "searches": [
            {"q": "Bolivia UIF Unidad de Investigaciones Financieras", "when": "7d"},
            {"q": "Bolivia ASFI criptoactivos criptomonedas regulación", "when": "7d"},
            {"q": "Bolivia P2P USDT dólar cripto cuentas bancarias", "when": "7d"},
            {"q": 'Bolivia "tipo de cambio" dólar paralelo', "when": "2d"},
            {"q": 'Bolivia dólares reservas "Banco Central de Bolivia"', "when": "2d"},
            {"q": "Bolivia escasez de dólares divisas", "when": "2d"},
            {"q": 'Bolivia préstamo BID OR CAF OR "Banco Mundial" OR FMI desembolso', "when": "7d"},
            {"q": "Bolivia financiamiento externo crédito aprobado", "when": "7d"},
            {"q": "Bolivia bonos deuda calificación Fitch OR Moody's", "when": "7d"},
            {"q": "Bolivia diésel gasolina YPFB importación combustible", "when": "2d"},
            {"q": "Bolivia subvención combustibles decreto", "when": "3d"},
            {"q": "precio del petróleo Brent OPEP", "when": "1d"},
            {"q": "flete marítimo contenedores navieras", "when": "2d"},
            {"q": 'Mar Rojo OR "Canal de Suez" OR "Canal de Panamá" OR Ormuz navieras', "when": "2d"},
            {"q": "puertos Arica Iquique carga boliviana", "when": "3d"},
            {"q": "Bolivia bloqueos paro economía pérdidas", "when": "2d"},
            {"q": "Bolivia inflación PIB déficit fiscal", "when": "3d"},
            {"q": "site:eldeber.com.bo economía dólar", "when": "2d"},
            {"q": "site:reduno.com.bo economía dólar", "when": "2d"},
            {"q": "site:unitel.bo economía dólar", "when": "2d"},
        ],
    },
}


# ───────────────────────── utilidades ─────────────────────────

def deep_merge(base, extra):
    out = dict(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config():
    cfg = DEFAULTS
    p = ROOT / "config.json"
    if p.exists():
        cfg = deep_merge(DEFAULTS, json.loads(p.read_text(encoding="utf-8")))
    return cfg


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]


def fmt_local(dt):
    return dt.astimezone(LPZ).strftime("%d/%m %H:%M")


def num(v, default=None):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def norm(s):
    return strip_accents(s).lower()


def http(url, data=None, headers=None, timeout=20, retries=2):
    h = {"User-Agent": "Mozilla/5.0 (compatible; p2p-monitor/1.0)", "Accept": "*/*"}
    if headers:
        h.update(headers)
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        h["Content-Type"] = "application/json"
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=body, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def write_json(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


# ───────────────────────── fuentes P2P ─────────────────────────

def px(v, plat=None):
    """Precio como lo muestra cada app: Bybit con 3 decimales, Binance con 2."""
    return f"{v:.3f}" if plat == "bybit" else f"{v:.2f}"


def slim(a):
    return {"price": a["price"], "available": round(a["available"], 2), "min": a["min"], "max": a["max"],
            "merchant": a.get("merchant"), "orders": a.get("orders"), "rate": a.get("rate")}


def binance_ads(trade_type, rows=20):
    """trade_type BUY = tú compras USDT (anuncios de vendedores); SELL = tú vendes USDT."""
    payload = {"fiat": "BOB", "page": 1, "rows": rows, "tradeType": trade_type, "asset": "USDT",
               "countries": [], "proMerchantAds": False, "shieldMerchantAds": False,
               "publisherType": None, "payTypes": [], "classifies": ["mass", "profession"]}
    d = json.loads(http("https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search", payload))
    ads = []
    for it in d.get("data") or []:
        a, u = it.get("adv", {}), it.get("advertiser", {})
        price = num(a.get("price"))
        if not price:
            continue
        ads.append({"price": price, "available": num(a.get("tradableQuantity"), 0),
                    "min": num(a.get("minSingleTransAmount"), 0),
                    "max": num(a.get("dynamicMaxSingleTransAmount"), num(a.get("maxSingleTransAmount"), 1e12)),
                    "merchant": u.get("nickName"), "orders": num(u.get("monthOrderCount")),
                    "rate": num(u.get("monthFinishRate"))})
    return ads


def bybit_ads(side, rows=20):
    payload = {"userId": "", "tokenId": "USDT", "currencyId": "BOB", "payment": [], "side": str(side),
               "size": str(rows), "page": "1", "amount": "", "authMaker": False, "canTrade": False}
    d = json.loads(http("https://api2.bybit.com/fiat/otc/item/online", payload))
    items = (d.get("result") or {}).get("items") or []
    ads = []
    for i in items:
        price = num(i.get("price"))
        if not price:
            continue
        rate = num(i.get("recentExecuteRate"))
        if rate is not None and rate > 1:
            rate = rate / 100.0
        ads.append({"price": price, "available": num(i.get("lastQuantity"), num(i.get("quantity"), 0)),
                    "min": num(i.get("minAmount"), 0), "max": num(i.get("maxAmount"), 1e12),
                    "merchant": i.get("nickName"), "orders": num(i.get("recentOrderNum")), "rate": rate})
    return ads


def exact_filter(ads, cfg):
    """Modo exacto: el #1 del libro tal como lo ves en la app. Solo se descartan los anuncios de
    juguete: con menos de `min_usdt_available` USDT, o cuyo límite máximo por orden (en Bs) equivale
    a `max_order_usdt_trivial` USDT o menos (rangos de 1 a 9 USDT)."""
    m = cfg.get("min_usdt_available") or 0
    triv = cfg.get("max_order_usdt_trivial") or 0
    return [a for a in ads
            if (a.get("available") or 0) >= m
            and (a.get("max") or 0) > triv * a["price"]]


def quality_filter(ads, amount, cfg):
    if cfg.get("exact_mode"):
        return exact_filter(ads, cfg)
    q = [a for a in ads
         if (a.get("orders") is None or a["orders"] >= cfg["min_monthly_orders"])
         and (a.get("rate") is None or a["rate"] >= cfg["min_finish_rate"])]
    fit = [a for a in q if a["min"] <= amount <= a["max"]]
    if len(fit) >= 3:
        return fit
    if len(q) >= 3:
        return q
    return ads


def summarize(buy_ads, sell_ads, cfg, name, source=None):
    amount = cfg["trade_amount_bob"]
    buy = sorted(quality_filter(buy_ads, amount, cfg), key=lambda a: a["price"])
    sell = sorted(quality_filter(sell_ads, amount, cfg), key=lambda a: -a["price"])
    if not buy or not sell:
        raise RuntimeError(f"sin anuncios con ≥{cfg.get('min_usdt_available')} USDT disponibles y límite máx. "
                           f"mayor a {cfg.get('max_order_usdt_trivial')} USDT")
    r = max(1, min(cfg["maker_rank"], len(buy), len(sell)))
    med = lambda ads: statistics.median(a["price"] for a in ads)  # noqa: E731
    s = {"name": name, "source": source or name,
         "best_buy": buy[0]["price"], "best_sell": sell[0]["price"],
         "med_buy": med(buy), "med_sell": med(sell),
         "maker_sell_price": buy[r - 1]["price"], "maker_buy_price": sell[r - 1]["price"],
         "vol_buy": round(sum(a["available"] for a in buy), 2),
         "vol_sell": round(sum(a["available"] for a in sell), 2),
         "ads_buy": len(buy), "ads_sell": len(sell),
         "top_buy": [slim(a) for a in buy[:5]], "top_sell": [slim(a) for a in sell[:5]]}
    s["ref"] = (s["med_buy"] + s["med_sell"]) / 2
    return s


def collect_binance(cfg, errors):
    try:
        buy, sell = binance_ads("BUY"), binance_ads("SELL")
        if len(buy) < 2 or len(sell) < 2:
            raise RuntimeError("pocos anuncios")
        return summarize(buy, sell, cfg, "binance")
    except Exception as e:  # noqa: BLE001
        errors.append(f"Binance P2P: {e}")
        return None


def collect_bybit(cfg, errors):
    """No sabemos a priori qué 'side' de Bybit es compra: se deduce por la mediana de precios
    (la lista donde TÚ compras tiene precios más altos que donde TÚ vendes)."""
    try:
        a, b = bybit_ads(0), bybit_ads(1)
        if len(a) < 2 or len(b) < 2:
            raise RuntimeError("pocos anuncios")
        ma, mb = statistics.median(x["price"] for x in a), statistics.median(x["price"] for x in b)
        buy, sell = (a, b) if ma >= mb else (b, a)
        return summarize(buy, sell, cfg, "bybit")
    except Exception as e:  # noqa: BLE001
        errors.append(f"Bybit P2P: {e}")
        return None


def wavesoft_get(path, token):
    return json.loads(http("https://wavesoft.cloud" + path, headers={"Authorization": f"Bearer {token}"}))


def wavesoft_latest(cfg, token, errors):
    """Respaldo: cotización horaria de Binance P2P publicada por NextWave Software."""
    try:
        d = wavesoft_get("/api/v1/p2p?fiat=BOB", token)
        m = (d.get("markets") or [])[0]
        buy, sell = m["buy"], m["sell"]
        s = {"name": "binance", "source": "wavesoft (Binance, dato horario)",
             "best_buy": buy["best"], "best_sell": sell["best"],
             "med_buy": buy["median"], "med_sell": sell["median"],
             "maker_sell_price": buy["median"], "maker_buy_price": sell["median"],
             "vol_buy": buy.get("volume", 0), "vol_sell": sell.get("volume", 0),
             "ads_buy": buy.get("ads", 0), "ads_sell": sell.get("ads", 0),
             "top_buy": [], "top_sell": [], "ref": m.get("referencePrice") or (buy["median"] + sell["median"]) / 2}
        return s
    except Exception as e:  # noqa: BLE001
        errors.append(f"wavesoft: {e}")
        return None


def wavesoft_seed(token, errors):
    """Trae hasta 500 puntos horarios para que RSI/MACD funcionen desde el primer día."""
    try:
        d = wavesoft_get("/api/v1/p2p/history?fiat=BOB&limit=500&order=desc", token)
        rows = None
        if isinstance(d, list):
            rows = d
        else:
            for v in d.values():
                if isinstance(v, list) and v and isinstance(v[0], dict) and "referencePrice" in v[0]:
                    rows = v
                    break
        out = []
        for r in rows or []:
            t, p = r.get("capturedHour"), num(r.get("referencePrice"))
            if t and p:
                dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
                out.append({"t": iso(dt), "br": p, "seed": 1})
        return sorted(out, key=lambda x: x["t"])
    except Exception as e:  # noqa: BLE001
        errors.append(f"wavesoft historial: {e}")
        return []


# ───────────────────────── noticias ─────────────────────────
# Objetivo: pocas noticias, pero que de verdad muevan el tipo de cambio en Bolivia.
#   1) Se buscan con consultas dirigidas (cambiario, préstamos, combustibles, petróleo, flete, economía).
#   2) Cada titular recibe un puntaje de relevancia; lo que no habla de Bolivia (o de petróleo/flete
#      en un movimiento grande, con fuente seria) se descarta.
#   3) Se agrupan las repetidas (misma noticia en varios medios) y se muestran solo las más importantes.

UP_WORDS = {  # empujan el dólar/USDT al alza en Bolivia
    "escasez de dolar": 2, "falta de dolar": 2, "faltan dolares": 2, "devaluacion": 2, "dolar sube": 2,
    "dolar paralelo sube": 2, "sube el dolar": 2, "suben el dolar": 2, "dolar se dispara": 2,
    "reservas caen": 2, "caida de reservas": 2, "reservas bajan": 2, "reservas se agotan": 2,
    "bloqueo": 1, "bloqueos": 1, "paro": 1, "huelga": 1, "crisis": 1, "inflacion": 1, "deficit": 1,
    "subvencion": 1, "restriccion": 1, "incertidumbre": 1, "conflicto": 1, "protesta": 1,
    "desabastecimiento": 1, "falta de diesel": 2, "escasez de diesel": 2, "escasez de combustible": 2,
    "filas": 1, "racionamiento": 1, "sanciones": 1, "default": 2, "calificacion negativa": 2,
    "rebaja la nota": 2, "rebaja de calificacion": 2, "baja la calificacion": 2, "corrida": 2, "cepo": 1,
    "brent sube": 2, "petroleo sube": 2, "brent se dispara": 2, "petroleo se dispara": 2, "crudo sube": 2,
    "fletes suben": 2, "flete sube": 2, "fletes se disparan": 2, "encarece": 1, "encarecen": 1,
    "pago de deuda": 1, "vencimiento de bonos": 1, "mar rojo": 1, "ormuz": 1, "importacion de combustible": 1,
}
DOWN_WORDS = {  # empujan el dólar/USDT a la baja en Bolivia
    "dolar baja": 2, "baja el dolar": 2, "bajan el dolar": 2, "cae el dolar": 2, "dolar cae": 2,
    "liberacion de dolares": 2, "ingreso de dolares": 2, "llegan dolares": 2, "recibe dolares": 2,
    "mas dolares": 1, "oferta de dolares": 1, "desembolso": 2, "desembolsa": 2, "desembolsara": 1,
    "aprueba prestamo": 2, "aprueba credito": 2, "aprobo prestamo": 2, "aprueba financiamiento": 2,
    "financiamiento aprobado": 2, "prestamo aprobado": 2, "acuerdo con el fmi": 2, "acuerdo con el bid": 2,
    "fmi": 1, "credito": 1, "prestamo": 1, "estabilidad cambiaria": 2, "reservas suben": 2,
    "aumentan las reservas": 2, "exportaciones crecen": 1, "normaliza": 1, "calma": 1, "tranquilidad": 1,
    "unificacion cambiaria": 2, "inversion": 1, "reactivacion": 1, "llegada de diesel": 1,
    "abastecimiento normal": 1, "sube la calificacion": 2, "mejora la calificacion": 2,
    "petroleo baja": 1, "petroleo cae": 1, "brent cae": 1, "brent baja": 1, "crudo cae": 1,
    "fletes bajan": 1, "fletes caen": 1,
}


def score_headline(title):
    t = norm(title)
    up = sum(w for k, w in UP_WORDS.items() if k in t)
    down = sum(w for k, w in DOWN_WORDS.items() if k in t)
    return max(-3, min(3, up - down))


def _rx(terms):
    return re.compile(r"\b(?:" + "|".join(re.escape(t) for t in terms) + r")(?:s|es)?\b")


R_BOLIVIA = _rx(["bolivia", "boliviano", "boliviana", "bcb", "banco central de bolivia", "ypfb", "asfi",
                 "cochabamba", "el alto", "la paz", "tarija", "oruro", "potosi", "santa cruz de la sierra"])
TOPICS = {  # nombre: (regex, puntos)
    "fin": (_rx(["prestamo", "credito", "desembolso", "financiamiento", "bid", "caf", "fmi", "fondo monetario",
                 "banco mundial", "bono", "eurobono", "deuda", "calificacion", "fitch", "moody", "s&p",
                 "riesgo pais", "swap", "banco de desarrollo", "cooperacion", "donacion"]), 3),
    "reg": (_rx(["uif", "unidad de investigaciones financieras", "asfi", "gafi", "lavado de dinero",
                 "lavado de activos", "legitimacion de ganancias", "criptoactivo", "activo virtual",
                 "criptomoneda", "cripto", "p2p", "usdt", "tether", "stablecoin", "exchange", "binance", "bybit",
                 "congelamiento de cuentas", "cuentas congeladas", "registro de exchanges"]), 5),
    "fuel": (_rx(["diesel", "gasolina", "combustible", "ypfb", "subvencion", "gas natural", "gnv", "glp",
                  "surtidor", "hidrocarburo"]), 2),
    "fx": (_rx(["tipo de cambio", "dolar", "paralelo", "usdt", "divisa", "reservas internacionales", "reserva",
                "cambiario", "devaluacion", "cripto", "criptomoneda"]), 3),
    "oil": (_rx(["petroleo", "brent", "wti", "opep", "crudo", "barril"]), 3),
    "freight": (_rx(["flete", "contenedor", "naviera", "mar rojo", "canal de suez", "canal de panama", "ormuz",
                     "hormuz", "puerto", "arica", "iquique", "antofagasta", "transporte maritimo", "buque",
                     "libre transito"]), 3),
    "trade": (_rx(["exportacion", "importacion", "balanza comercial", "gas", "litio", "soya", "oro", "remesa",
                   "inversion extranjera", "mineria", "arancel"]), 2),
    "macro": (_rx(["inflacion", "pib", "deficit", "presupuesto", "paro", "bloqueo", "huelga", "decreto",
                   "ministro de economia", "ministerio de economia", "crisis", "escasez", "recesion", "movilizacion",
                   "estado de excepcion", "marcha",
                   "politica monetaria", "tasa de interes", "fiscal"]), 2),
}
CAT_LABEL = {"reg": "UIF, ASFI y P2P", "fin": "Préstamos y financiamiento", "oil": "Petróleo", "freight": "Flete y puertos",
             "fuel": "Combustibles", "fx": "Dólar y reservas", "trade": "Comercio exterior",
             "macro": "Economía de Bolivia"}
CAT_ORDER = ["reg", "fin", "oil", "freight", "fuel", "fx", "trade", "macro"]
R_MAGNITUDE = re.compile(r"\d+(?:[.,]\d+)?\s?%|\b(?:dispar|desplom|hund|maximo|minimo|record|crisis|interrump|"
                         r"ataque|cierre|colaps|recorte|escasez|salta|duplic|encarec|derrumb|sancion)\w*")
R_STRONG = re.compile(r"\b(?:dispar|desplom|hund|record|colaps|ataque|interrump|cierre|bloque|derrumb|sancion|crisis)\w*")


def strong_move(t):
    """Movimiento grande: una palabra fuerte o un porcentaje de 3% o más."""
    if R_STRONG.search(t):
        return True
    return any(float(p.replace(",", ".")) >= 3 for p in re.findall(r"(\d+(?:[.,]\d+)?)\s?%", t))


R_NEG = _rx(["futbol", "gol", "clasico", "vence a", "campeonato", "torneo", "seleccion", "liga", "copa", "eliminatorias",
             "clasifica", "jugador", "entrenador", "farandula", "horoscopo", "receta", "feminicidio", "asesinato",
             "bolivar", "the strongest", "oriente petrolero", "wilstermann", "blooming", "always ready",
             "puerto rico"])
R_NEG_FOREIGN = _rx(["bitcoin", "ethereum", "memecoin", "dogecoin", "solana"])  # cripto genérica: solo si es de Bolivia
R_ROUTINE = re.compile(r"asi se cotiza|a cuanto se cotiza|asi estara el dolar|como estara el dolar|dolar hoy|"
                       r"necesita cambiar dolares|cotizaciones? (?:para|de) (?:este|hoy)|cotizacion del dolar (?:hoy|este)")
R_FOREIGN = _rx(["argentina", "argentino", "venezuela", "venezolano", "peru", "peruano", "mexico", "mexicano",
                 "colombia", "colombiano", "chile", "chileno", "ecuador", "brasil", "paraguay", "uruguay",
                 "espana", "dolar blue", "peso argentino", "costa rica", "honduras", "guatemala", "cuba",
                 "nicaragua", "el salvador"])
GOOD_SRC = {"eldeber.com.bo", "reduno.com.bo", "unitel.bo", "lostiempos.com", "opinion.com.bo", "paginasiete.bo",
            "brujuladigital.net", "vision360.bo", "erbol.com.bo", "urgente.bo", "eju.tv", "correodelsur.com",
            "la-razon.com", "larazon.com", "ahoraelpueblo.bo", "abi.bo", "anf.com.bo", "bcb.gob.bo",
            "reuters.com", "bloomberg.com", "bloomberglinea.com", "apnews.com", "efe.com", "bbc.com", "ft.com",
            "wsj.com", "cnbc.com", "elpais.com", "swissinfo.ch", "dw.com", "france24.com", "americaeconomia.com",
            "bnamericas.com", "infobae.com", "expansion.com", "lloydslist.com", "splash247.com",
            "freightwaves.com", "tradewindsnews.com", "agenciaefe.com", "europapress.es", "asfi.gob.bo",
            "uif.gob.bo", "criptonoticias.com"}
PRIORITY_SRC = {"eldeber.com.bo", "reduno.com.bo", "unitel.bo"}  # secciones de economía que sigues
BOLIVIAN_SRC = {"eldeber.com.bo", "reduno.com.bo", "unitel.bo", "lostiempos.com", "opinion.com.bo",
                "paginasiete.bo", "brujuladigital.net", "vision360.bo", "erbol.com.bo", "urgente.bo", "eju.tv",
                "correodelsur.com", "la-razon.com", "larazon.com", "ahoraelpueblo.bo", "abi.bo", "anf.com.bo",
                "bcb.gob.bo", "asfi.gob.bo", "uif.gob.bo"}
JUNK_SRC = {"facebook.com", "youtube.com", "tiktok.com", "instagram.com", "x.com", "twitter.com", "medium.com",
            "blogspot.com", "wordpress.com", "t.me", "telegram.org", "pinterest.com", "reddit.com"}
STOP_TOKENS = set("para como pero sobre entre desde hasta este esta estos estas tras ante segun mas menos muy sus "
                  "del las los una unos unas con por que son fue han ser hay ano anos dia dias nuevo nueva "
                  "tras donde cuando esta estan sera podria podrian".split())


def domain_of(url):
    try:
        d = urllib.parse.urlparse(url).netloc.lower()
    except Exception:  # noqa: BLE001
        return ""
    return d[4:] if d.startswith("www.") else d


def dom_in(dom, group):
    return any(dom == g or dom.endswith("." + g) for g in group)


def title_tokens(title):
    return {w for w in re.findall(r"[a-z0-9]{4,}", norm(title)) if w not in STOP_TOKENS}


def jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


def ago(iso_t, now=None):
    mins = int(((now or now_utc()) - parse_iso(iso_t)).total_seconds() / 60)
    return f"hace {mins} min" if mins < 90 else f"hace {mins // 60} h"


def analyze_headline(title, dom, ncfg):
    """Devuelve None si no sirve, o dict con puntaje de relevancia, categoría y dirección."""
    t = norm(title)
    if R_NEG.search(t) or R_ROUTINE.search(t) or dom_in(dom, JUNK_SRC):
        return None
    bol_text = bool(R_BOLIVIA.search(t))
    bolivia = bol_text or dom_in(dom, BOLIVIAN_SRC)  # un medio boliviano ya implica Bolivia
    if not bolivia and R_NEG_FOREIGN.search(t):
        return None
    topics = {k: pts for k, (rx, pts) in TOPICS.items() if rx.search(t)}
    if not topics:
        return None
    if R_FOREIGN.search(t) and not bol_text and not ("oil" in topics or "freight" in topics):
        return None  # habla de otro país sin mencionar Bolivia
    good, prio = dom_in(dom, GOOD_SRC), dom_in(dom, PRIORITY_SRC)
    magnitude = bool(R_MAGNITUDE.search(t))
    if not bolivia:
        # Noticia del exterior: solo petróleo o flete, con un movimiento claro y fuente seria
        if not (("oil" in topics or "freight" in topics) and strong_move(t) and good):
            return None
        if "freight" in topics and "oil" not in topics and not re.search(
                r"flete|contenedor|naviera|mar rojo|canal|ormuz|hormuz", t):
            return None  # "puerto"/"buque" sueltos no bastan
    pts = min(6, sum(topics.values()))
    score = pts + (4 if bolivia else 0) + (2 if magnitude else 0) + (3 if prio else 2 if good else 0)
    cat = next(c for c in CAT_ORDER if c in topics)
    d = score_headline(title)
    if abs(d) >= 2:
        score += 1
    return {"relevance": score, "cat": cat, "cat_label": CAT_LABEL[cat], "score": d, "bolivia": bolivia}


# ── secciones de economía de El Deber, Red Uno y Unitel (lectura directa de la página) ──

def slug_text(url):
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    slug = re.sub(r"[_-](?:[A-Z]{2})?\d{6,}$", "", slug)  # quita el código/fecha final
    return slug.replace("-", " ").replace("_", " ")


def time_from_url(url):
    m = re.search(r"_(\d{10})$", url)  # El Deber: el número final es la hora de publicación (unix)
    if m:
        v = int(m.group(1))
        if 1.5e9 < v < 4.1e9:
            return datetime.fromtimestamp(v, timezone.utc)
    m = re.search(r"-(\d{12,14})$", url)  # Red Uno: AAAAMMDDhhmmss (hora de Bolivia)
    if m:
        d = m.group(1)
        try:
            ymd, rest = d[:8], d[8:].rjust(6, "0")
            return datetime.strptime(ymd + rest, "%Y%m%d%H%M%S").replace(tzinfo=LPZ).astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def meta_content(page, prop):
    for pat in (r'<meta[^>]+(?:property|name)=["\']' + prop + r'["\'][^>]*content=["\']([^"\']*)["\']',
                r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']' + prop + r'["\']'):
        m = re.search(pat, page, re.I)
        if m:
            return htmllib.unescape(m.group(1)).strip()
    return None


def parse_article(page):
    title = meta_content(page, "og:title")
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
        title = htmllib.unescape(m.group(1)).strip() if m else None
    if title:
        title = re.sub(r"\s*[|\-–—]\s*(?:El Deber|Red Uno|RedUno|Unitel)[^|]*$", "", title, flags=re.I).strip()
    ts = meta_content(page, "article:published_time") or meta_content(page, "og:article:published_time")
    if not ts:
        m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', page)
        ts = m.group(1) if m else None
    dt = None
    if ts:
        try:
            dt = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=LPZ)
            dt = dt.astimezone(timezone.utc)
        except ValueError:
            dt = None
    return title, dt


def extract_links(page, sec):
    page = page.replace("\\/", "/")
    out, seen = [], set()
    for m in re.finditer(sec["link_re"], page):
        path = m.group(1)
        url = path if path.startswith("http") else sec["base"] + path
        if url in seen:
            continue
        seen.add(url)
        atitle = None
        a = re.search(r'<a[^>]+href=["\'][^"\']*' + re.escape(path) + r'["\'][^>]*>(.*?)</a>', page, re.S | re.I)
        if a:
            txt = re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", a.group(1)))).strip()
            if len(txt) >= 25:
                atitle = txt
        out.append((url, atitle))
    return out


def fetch_sections(ncfg, errors, cache, now, cutoff):
    from concurrent.futures import ThreadPoolExecutor
    secs = ncfg.get("sections") or []
    if not secs:
        return []

    def get(url, timeout=12):
        try:
            return http(url, timeout=timeout, retries=1 if timeout > 10 else 0).decode("utf-8", "ignore")
        except Exception as e:  # noqa: BLE001
            return e

    with ThreadPoolExecutor(max_workers=4) as ex:
        pages = list(ex.map(lambda s: get(s["url"]), secs))
    pending, cands = [], []
    for sec, page in zip(secs, pages):
        if isinstance(page, Exception):
            errors.append(f"{sec['name']}: no se pudo leer la sección ({page})")
            continue
        links = extract_links(page, sec)
        if not links:
            errors.append(f"{sec['name']}: la página cambió y no encontré noticias (avísame para ajustarlo)")
        for url, atitle in links[:40]:
            if not analyze_headline(slug_text(url), sec["domain"], ncfg):
                continue  # primer filtro con el texto del enlace
            pending.append((sec, url, atitle))
    # título y hora reales: de la caché o abriendo la noticia (máx. N por ejecución)
    need = []
    for sec, url, atitle in pending:
        c = cache.get(url)
        if c and c.get("title") and c.get("time"):
            continue
        need.append((sec, url, atitle))
    need = need[: ncfg.get("max_article_fetches", 10)]
    with ThreadPoolExecutor(max_workers=5) as ex:
        arts = list(ex.map(lambda x: get(x[1], timeout=10), need))
    for (sec, url, atitle), page in zip(need, arts):
        title, dt = (None, None) if isinstance(page, Exception) else parse_article(page)
        dt = dt or time_from_url(url)
        cache[url] = {"title": title or atitle or slug_text(url), "time": iso(dt) if dt else None,
                      "seen": iso(now)}
    for sec, url, atitle in pending:
        c = cache.get(url)
        if not c:
            continue
        dt = parse_iso(c["time"]) if c.get("time") else (time_from_url(url) or now - timedelta(hours=12))
        if dt < cutoff:
            continue
        a = analyze_headline(c["title"], sec["domain"], ncfg)
        if not a:
            continue
        cands.append({"title": c["title"], "link": url, "source": sec["name"], "domain": sec["domain"],
                      "time": iso(dt), "toks": title_tokens(c["title"]), **a})
    if len(cache) > 300:
        for k in sorted(cache, key=lambda k: cache[k].get("seen", ""))[: len(cache) - 300]:
            del cache[k]
    return cands


def fetch_news(cfg, errors, cache=None):
    from concurrent.futures import ThreadPoolExecutor
    ncfg = cfg["news"]
    if not ncfg.get("enabled"):
        return []
    now = now_utc()
    cutoff = now - timedelta(hours=ncfg["max_age_hours"])

    def one(s):
        url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(f"{s['q']} when:{s.get('when', '2d')}")
               + "&hl=es-419&gl=BO&ceid=BO:es-419")
        try:
            return s, ET.fromstring(http(url, timeout=12, retries=1))
        except Exception as e:  # noqa: BLE001
            return s, e

    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(one, ncfg["searches"]))
    ok = 0
    cands = []
    try:
        cands += fetch_sections(ncfg, errors, cache if cache is not None else {}, now, cutoff)
    except Exception as e:  # noqa: BLE001
        errors.append(f"Secciones de economía: {e}")
    n_direct = len(cands)
    for s, root in results:
        if isinstance(root, Exception):
            errors.append(f"Noticias ({s['q'][:28]}…): {root}")
            continue
        ok += 1
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            src_el = it.find("source")
            source = (src_el.text or "").strip() if src_el is not None else ""
            dom = domain_of(src_el.get("url", "")) if src_el is not None else ""
            if source and title.endswith(" - " + source):
                title = title[: -len(source) - 3].strip()
            if not title:
                continue
            try:
                dt = parsedate_to_datetime(it.findtext("pubDate"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except Exception:  # noqa: BLE001
                dt = now
            if dt < cutoff:
                continue
            a = analyze_headline(title, dom, ncfg)
            if not a:
                continue
            cands.append({"title": title, "link": (it.findtext("link") or "").strip(), "source": source,
                          "domain": dom, "time": iso(dt), "toks": title_tokens(title), **a})
    if ncfg["searches"] and ok == 0 and n_direct == 0:
        errors.append("Noticias: ninguna fuente respondió")

    # agrupar la misma noticia publicada por varios medios
    cands.sort(key=lambda c: (c["relevance"], c["time"]), reverse=True)
    groups = []
    for c in cands:
        for g in groups:
            if jaccard(c["toks"], g["toks"]) >= 0.5:
                g["members"].append(c)
                g["toks"] |= c["toks"]
                break
        else:
            groups.append({"toks": set(c["toks"]), "members": [c]})
    items = []
    for g in groups:
        m = g["members"]
        best = max(m, key=lambda c: (c["relevance"], c["time"]))
        doms = {c["domain"] or c["source"] for c in m}
        n_src = len(doms)
        rel = best["relevance"] + min(3, n_src - 1)
        if rel < ncfg["min_score"]:
            continue
        important = rel >= ncfg["important_score"]
        sc = best["score"]
        items.append({"title": best["title"], "link": best["link"], "source": best["source"], "time": best["time"],
                      "cat": best["cat"], "cat_label": best["cat_label"], "score": sc, "relevance": rel,
                      "n_sources": n_src, "important": important,
                      "impact": "alto" if important else "medio",
                      "direction": "sube" if sc > 0 else ("baja" if sc < 0 else "neutral"),
                      "sig": sorted(best["toks"])[:14]})
    items.sort(key=lambda x: (x["important"], x["relevance"], x["time"]), reverse=True)
    return items[: ncfg["max_items"]]


def news_line(n, now=None):
    arrow = {"sube": "↑ sube USD", "baja": "↓ baja USD", "neutral": "• neutral"}[n["direction"]]
    star = "⭐ " if n.get("important") else ""
    multi = f" · {n['n_sources']} medios" if n.get("n_sources", 1) > 1 else ""
    return (f"{star}[{n.get('cat_label', '')}] {arrow}\n{n['title']}\n"
            f"{n.get('source', '')}{multi} · {ago(n['time'], now)}\n{n.get('link', '')}")


# ───────────────────────── análisis técnico ─────────────────────────

def ema(vals, n):
    k = 2 / (n + 1)
    e, out = vals[0], [vals[0]]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(vals, n=14):
    if len(vals) < n + 1:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = vals[i] - vals[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(vals)):
        d = vals[i] - vals[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    return 100 - 100 / (1 + ag / al)


def macd(vals):
    if len(vals) < 35:
        return None
    line = [a - b for a, b in zip(ema(vals, 12), ema(vals, 26))]
    sig = ema(line, 9)
    hist = [l - s for l, s in zip(line, sig)]
    return {"line": line[-1], "signal": sig[-1], "hist": hist[-1], "hist_prev": hist[-2]}


def bollinger(vals, n=20, k=2.0):
    if len(vals) < n:
        return None
    w = vals[-n:]
    m, sd = statistics.mean(w), statistics.pstdev(w)
    up, lo = m + k * sd, m - k * sd
    pct_b = (vals[-1] - lo) / (up - lo) if up > lo else 0.5
    return {"mid": m, "upper": up, "lower": lo, "pct_b": pct_b}


def hourly_series(hist):
    by = {}
    for r in hist:
        p = r.get("br") or r.get("yr")
        if p:
            by[r["t"][:13]] = (r["t"], p)
    keys = sorted(by)
    return [(by[k][0], by[k][1]) for k in keys]


def pct_change_since(hist, minutes, price_now, now):
    """Variación % respecto al registro más cercano a 'hace X minutos' (tolerancia 40%)."""
    target = now - timedelta(minutes=minutes)
    best, best_d = None, None
    for r in reversed(hist):
        p = r.get("br") or r.get("yr")
        if not p or r.get("seed"):
            continue
        d = abs((parse_iso(r["t"]) - target).total_seconds())
        if best_d is None or d < best_d:
            best, best_d = p, d
        if parse_iso(r["t"]) < target - timedelta(minutes=minutes * 0.4):
            break
    if best is None or best_d > minutes * 60 * 0.4:
        return None
    return (price_now / best - 1) * 100


def technical_analysis(hist, price_now, now):
    hs = hourly_series(hist)
    closes = [p for _, p in hs]
    ta = {"n_hourly": len(closes), "price": price_now}
    ta["change_30m_pct"] = pct_change_since(hist, 30, price_now, now)
    ta["change_1h_pct"] = pct_change_since(hist, 60, price_now, now)
    ta["change_2h_pct"] = pct_change_since(hist, 120, price_now, now)
    if len(closes) >= 25:
        ta["change_24h_pct"] = (closes[-1] / closes[-25] - 1) * 100
    else:
        ta["change_24h_pct"] = None
    if len(closes) >= 3:
        ta["change_3h_pct"] = (closes[-1] / closes[-4] - 1) * 100 if len(closes) >= 4 else None
    ta["rsi"] = rsi(closes)
    ta["macd"] = macd(closes)
    ta["bollinger"] = bollinger(closes)
    if len(closes) >= 20:
        ta["ema20"] = ema(closes, 20)[-1]
    if len(closes) >= 50:
        ta["ema50"] = ema(closes, 50)[-1]
    win = closes[-72:] if closes else []
    if len(win) >= 12:
        ta["support"] = min(win)
        ta["resistance"] = max(win)
        prev = closes[-73:-1]  # sin la última hora: para saber si el precio ACTUAL los rompe
        if len(prev) >= 12:
            ta["support_prev"] = min(prev)
            ta["resistance_prev"] = max(prev)
    ta["sufficient"] = len(closes) >= 35
    return ta


def depth_pressure(platforms):
    """Presión de oferta/demanda. >0: más demanda de USDT que oferta (sube); <0: lo contrario."""
    vals = []
    for p in platforms.values():
        tot = p["vol_buy"] + p["vol_sell"]
        if tot > 0:
            vals.append((p["vol_sell"] - p["vol_buy"]) / tot)
    return statistics.mean(vals) if vals else 0.0


def clamp(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


def build_scenario(ta, news, pressure, weights=None, cat_weight=None):
    comps, reasons = [], []
    W = dict({"rsi": 15, "macd": 20, "tendencia": 20, "momentum": 15, "profundidad": 10, "noticias": 20}, **(weights or {}))
    CW = cat_weight or {}

    def add(name, value, weight, text):
        comps.append((name, value, weight))
        if abs(value) > 0.05 and text:
            reasons.append(text)

    r = ta.get("rsi")
    if r is not None:
        v = 0.6 if r < 30 else 0.2 if r < 40 else -0.6 if r > 70 else -0.2 if r > 60 else 0.0
        add("rsi", v, W["rsi"], f"RSI {r:.0f}: " + ("sobrevendido, posible rebote" if v > 0.3 else
                                              "zona baja" if v > 0 else
                                              "sobrecomprado, posible corrección" if v < -0.3 else "zona alta"))
    m = ta.get("macd")
    if m:
        h, hp = m["hist"], m["hist_prev"]
        v = 1.0 if (h > 0 and h > hp) else 0.3 if h > 0 else -1.0 if (h < 0 and h < hp) else -0.3
        add("macd", v, W["macd"], "MACD " + ("alcista y acelerando" if v == 1 else "positivo pero perdiendo fuerza" if v == 0.3
                                      else "bajista y acelerando" if v == -1 else "negativo pero mejorando"))
    e20, e50, price = ta.get("ema20"), ta.get("ema50"), ta.get("price")
    if e20 and price:
        v = 0.0
        v += 0.5 if price > e20 else -0.5
        if e50:
            v += 0.5 if e20 > e50 else -0.5
        add("tendencia", v, W["tendencia"], "Tendencia " + ("alcista (precio sobre medias)" if v > 0 else "bajista (precio bajo medias)"))
    mom = ta.get("change_3h_pct")
    if mom is not None:
        v = clamp(mom / 0.6)
        add("momentum", v, W["momentum"], f"Momentum 3h {mom:+.2f}%")
    add("profundidad", clamp(pressure * 2), W["profundidad"],
        "Más demanda que oferta de USDT en los libros" if pressure > 0.1 else
        "Más oferta que demanda de USDT en los libros" if pressure < -0.1 else "")
    if news:
        recent = [n for n in news if n["score"] != 0][:10]
        s = sum(n["score"] * CW.get(n.get("cat"), 1.0) * (1.5 if n.get("important") else 1) for n in recent)
        v = clamp(s / 4)
        add("noticias", v, W["noticias"], ("Noticias con sesgo alcista para el dólar" if v > 0 else "Noticias con sesgo bajista para el dólar")
            if abs(v) > 0.05 else "")
    used_w = sum(w for _, _, w in comps) or 1
    score = sum(v * w for _, v, w in comps) / used_w * 100
    nn = [(n_, v, w) for n_, v, w in comps if n_ != "noticias"]
    score_nn = sum(v * w for _, v, w in nn) / (sum(w for _, _, w in nn) or 1) * 100 if nn else 0.0
    label = "ALZA" if score >= 25 else "BAJA" if score <= -25 else "ESTABLE"
    nz = [v for _, v, _ in comps if abs(v) > 0.05]
    agree = sum(1 for v in nz if (v > 0) == (score > 0)) / len(nz) if nz else 0
    conf = "alta" if (agree >= 0.8 and len(comps) >= 5 and abs(score) >= 40) else \
           "media" if (agree >= 0.6 and len(comps) >= 4) else "baja"
    if not ta.get("sufficient"):
        conf = "baja"
        reasons.append("Historial corto: RSI/MACD aún poco fiables (se afinan solos con el tiempo)")
    return {"label": label, "score": round(score, 1), "confidence": conf,
            "strong": abs(score) >= 50, "reasons": reasons,
            "score_nn": round(score_nn, 1), "comps": {n_: round(v, 2) for n_, v, _ in comps}}


# ───────────────────────── oportunidades ─────────────────────────

def opportunities(platforms, cfg):
    fee = {"binance": cfg["fee_binance_pct"] / 100, "bybit": cfg["fee_bybit_pct"] / 100}
    cap = cfg["capital_bob"]
    out = {"taker": [], "maker": [], "arbitrage": []}
    for n, p in platforms.items():
        f = fee[n]
        g = p["best_sell"] / p["best_buy"] - 1
        net = p["best_sell"] * (1 - f) / (p["best_buy"] * (1 + f)) - 1
        out["taker"].append({"platform": n, "buy": p["best_buy"], "sell": p["best_sell"],
                             "gross_pct": g * 100, "net_pct": net * 100, "est_bob": net * cap})
        mg = p["maker_sell_price"] / p["maker_buy_price"] - 1
        mn = p["maker_sell_price"] * (1 - f) / (p["maker_buy_price"] * (1 + f)) - 1
        out["maker"].append({"platform": n, "buy_price": p["maker_buy_price"], "sell_price": p["maker_sell_price"],
                             "gross_pct": mg * 100, "net_pct": mn * 100, "est_bob": mn * cap})
    if "binance" in platforms and "bybit" in platforms:
        tf = cfg["usdt_transfer_fee"] / max(cfg["transfer_size_usdt"], 1)
        for a, b in (("binance", "bybit"), ("bybit", "binance")):
            pa, pb = platforms[a], platforms[b]
            net0 = pb["best_sell"] * (1 - fee[b]) / (pa["best_buy"] * (1 + fee[a])) - 1
            out["arbitrage"].append({"buy_on": a, "sell_on": b, "buy": pa["best_buy"], "sell": pb["best_sell"],
                                     "net_pct": net0 * 100, "net_after_transfer_pct": (net0 - tf) * 100,
                                     "est_bob": net0 * cap, "est_bob_after_transfer": (net0 - tf) * cap})
    return out


def build_signals(ta, scen, cfg):
    sig = []
    a = cfg["alerts"]
    r, price = ta.get("rsi"), ta.get("price")
    sup, res = ta.get("support"), ta.get("resistance")
    near = lambda lvl: lvl and abs(price / lvl - 1) <= 0.003  # noqa: E731
    if r is not None and r <= a["rsi_oversold"] and near(sup):
        sig.append({"type": "compra", "text": f"Zona de posible COMPRA: RSI {r:.0f} y precio cerca del soporte {sup:.2f}."})
    if r is not None and r >= a["rsi_overbought"] and near(res):
        sig.append({"type": "venta", "text": f"Zona de posible VENTA: RSI {r:.0f} y precio cerca de la resistencia {res:.2f}."})
    if scen["label"] == "BAJA" and scen["strong"]:
        sig.append({"type": "cuidado", "text": "Tendencia bajista fuerte: evita comprar USDT ahora; espera RSI bajo y precio estable unas horas."})
    if scen["label"] == "ALZA" and scen["strong"]:
        sig.append({"type": "cuidado", "text": "Tendencia alcista fuerte: si tienes USDT, podrías esperar antes de vender; si debes comprar, hazlo por tramos."})
    return sig


# ───────────────────────── alertas ─────────────────────────

def money(x):
    return f"{x:,.2f}"


class Alerter:
    def __init__(self, cfg, state, send, now):
        self.cfg, self.state, self.send, self.now = cfg, state, send, now
        self.last = state.setdefault("alert_last", {})
        self.log = state.setdefault("log", [])
        self.queue = []  # repeticiones pendientes: [momento_epoch, texto]
        self.events = []  # señales disparadas en esta ejecución (para la retroalimentación)

    def pending(self):
        return bool(self.queue)

    def next_due(self):
        return min((q[0] for q in self.queue), default=None)

    def flush_due(self, force=False):
        """Envía las repeticiones cuyo momento ya llegó (o todas, esperando entre ellas, si force)."""
        while self.queue:
            self.queue.sort(key=lambda q: q[0])
            due, text = self.queue[0]
            wait = due - time.time()
            if wait > 0:
                if not force:
                    return
                time.sleep(min(wait, 20))
            self.queue.pop(0)
            try:
                self.send(text)
            except Exception as e:  # noqa: BLE001
                print("repetición de alerta:", e, file=sys.stderr)

    def muted(self):
        return self.now.timestamp() < self.state.get("muted_until", 0)

    def fire(self, key, kind, text, cd_min=None, ev=None):
        if kind in self.state.get("auto_off", []) or self.muted():
            return False
        cd = (self.cfg["alerts"]["cooldown_min"].get(kind, 60) if cd_min is None else cd_min) * 60
        t = self.now.timestamp()
        if t - self.last.get(key, 0) < cd:
            return False
        self.last[key] = t
        self.log.append({"t": iso(self.now), "kind": kind, "text": text})
        del self.log[:-60]
        self.send(text)
        if ev:
            self.events.append(ev)
        a = self.cfg["alerts"]
        n, gap = int(a.get("repeat_times") or 1), float(a.get("repeat_seconds") or 15)
        if kind in a.get("repeat_kinds", []) and n > 1:
            for i in range(2, n + 1):
                self.queue.append([time.time() + gap * (i - 1), f"🔁 Alerta repetida {i}/{n}\n{text}"])
        return True


def evaluate_opp_alerts(al, cfg, opps):
    a = cfg["alerts"]
    for o in opps["arbitrage"]:
        if o["net_pct"] >= a["arb_net_pct"]:
            al.fire(f"arb:{o['buy_on']}", "arb",
                    f"💱 ARBITRAJE: compra en {o['buy_on'].title()} a {px(o['buy'], o['buy_on'])} y vende en {o['sell_on'].title()} a {px(o['sell'], o['sell_on'])}\n"
                    f"Neto {o['net_pct']:+.2f}% (≈ Bs {o['est_bob']:+,.0f} con tu capital; "
                    f"{o['net_after_transfer_pct']:+.2f}% si mueves USDT por la red).")
    for o in opps["taker"]:
        if o["net_pct"] >= a["taker_net_pct"]:
            al.fire(f"taker:{o['platform']}", "taker",
                    f"⚡ {o['platform'].title()}: compra {px(o['buy'], o['platform'])} / vende {px(o['sell'], o['platform'])} → neto {o['net_pct']:+.2f}% "
                    f"(≈ Bs {o['est_bob']:+,.0f}). Ejecuta rápido, estos cruces duran poco.")
    for o in opps["maker"]:
        if o["net_pct"] >= a["maker_net_pct"]:
            al.fire(f"maker:{o['platform']}", "maker",
                    f"🧩 SPREAD en {o['platform'].title()}: publica compra ≈ {px(o['buy_price'], o['platform'])} y venta ≈ {px(o['sell_price'], o['platform'])} "
                    f"→ neto {o['net_pct']:+.2f}% por ciclo (≈ Bs {o['est_bob']:+,.0f}).")


def evaluate_breakout(al, cfg, ta, price):
    """Ruptura: el precio ACTUAL pasa el piso o el techo de los últimos 3 días."""
    m = cfg["alerts"]["breakout_margin_pct"] / 100
    sup, res = ta.get("support_prev"), ta.get("resistance_prev")
    if sup and price <= sup * (1 - m):
        al.fire("breakout:down", "breakout",
                f"💥 RUPTURA de SOPORTE: el USDT cayó a {price:.3f}, bajo el piso de 3 días ({sup:.3f}).\n"
                "La señal de compra por soporte ya no vale: evita comprar en la caída y espera que se estabilice. "
                f"Si en 1 o 2 horas vuelve sobre {sup:.3f}, fue una falsa ruptura.",
                ev={"typ": "ruptura_soporte", "p": price})
    if res and price >= res * (1 + m):
        al.fire("breakout:up", "breakout",
                f"💥 RUPTURA de RESISTENCIA: el USDT subió a {price:.3f}, sobre el techo de 3 días ({res:.3f}).\n"
                "La señal de venta por techo pierde validez y el alza puede continuar: si vendes, hazlo por tramos; "
                f"si piensas comprar, no persigas el precio. Si vuelve bajo {res:.3f}, fue una falsa ruptura.",
                ev={"typ": "ruptura_resistencia", "p": price})


def evaluate_bollinger(al, cfg, ta, price):
    """Bandas de Bollinger (20 h): el precio toca una banda → está 'estirado' respecto a su promedio."""
    b = ta.get("bollinger")
    if not b or not price or b["mid"] <= 0:
        return
    if (b["upper"] - b["lower"]) / b["mid"] * 100 < cfg["alerts"]["bollinger_min_width_pct"]:
        return
    r = ta.get("rsi")
    rtxt = "" if r is None else f" RSI {r:.0f}"
    if price >= b["upper"]:
        conf = "" if r is None else (" (confirma sobrecompra)" if r >= 65 else " (el RSI no confirma, cuidado)")
        al.fire("boll:up", "bollinger",
                f"📈 BOLLINGER banda SUPERIOR: {price:.3f} sobre {b['upper']:.3f}; está estirado frente a su promedio "
                f"de 20 h ({b['mid']:.3f}).{rtxt}{conf}\nSuele corregir hacia el promedio, salvo tendencia fuerte: "
                "buena zona para vender o no comprar.", ev={"typ": "bollinger_sup", "p": price})
    elif price <= b["lower"]:
        conf = "" if r is None else (" (confirma sobreventa)" if r <= 35 else " (el RSI no confirma, cuidado)")
        al.fire("boll:down", "bollinger",
                f"📉 BOLLINGER banda INFERIOR: {price:.3f} bajo {b['lower']:.3f}; está estirado a la baja frente a su "
                f"promedio de 20 h ({b['mid']:.3f}).{rtxt}{conf}\nSuele rebotar hacia el promedio, salvo tendencia "
                "bajista fuerte: buena zona para comprar por tramos.", ev={"typ": "bollinger_inf", "p": price})


def daily_text(cfg, hist, ta, scen, platforms, news, now):
    loc = now.astimezone(LPZ)
    L = [f"☀️ Buenos días · P2P USDT/BOB · {DIAS[loc.weekday()]} {loc.strftime('%d/%m')}"]
    for n in ("binance", "bybit"):
        p = platforms.get(n)
        if p:
            f = cfg[f"fee_{n}_pct"] / 100
            L.append(f"{PLAT[n]}: compra {px(p['best_buy'], n)} · venta {px(p['best_sell'], n)} · "
                     f"spread {spread_pct(p):+.2f}% (neto maker {maker_net_pct(p, f) * 100:+.2f}%)")
    day0 = loc.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    day1 = day0 + timedelta(days=1)
    pts = []
    for r in hist:
        pr = r.get("br") or r.get("yr")
        if pr and not r.get("seed") and day0 <= parse_iso(r["t"]).astimezone(LPZ) < day1:
            pts.append(pr)
    if len(pts) >= 5:
        L.append(f"Ayer: abrió {pts[0]:.3f} · cerró {pts[-1]:.3f} ({(pts[-1] / pts[0] - 1) * 100:+.2f}%) · "
                 f"mín {min(pts):.3f} · máx {max(pts):.3f} (rango {(max(pts) / min(pts) - 1) * 100:.2f}%)")
    if ta.get("support"):
        L.append(f"Mapa de hoy: soporte {ta['support']:.3f} · resistencia {ta['resistance']:.3f}"
                 + (f" · RSI {ta['rsi']:.0f}" if ta.get("rsi") is not None else ""))
    L.append(f"Escenario: {scen['label']} (puntaje {scen['score']:+.0f}, confianza {scen['confidence']})")
    for r in scen["reasons"][:3]:
        L.append(f"  • {r}")
    top = [n for n in news if n.get("important")][:2] or news[:1]
    if top:
        L.append("Noticias clave:")
        for n in top:
            arrow = {"sube": "↑", "baja": "↓", "neutral": "•"}[n["direction"]]
            L.append(f"  {arrow} {n['title']} ({n.get('source', '')})")
    L.append("No es asesoría financiera.")
    return "\n".join(L)


def maybe_daily(al, cfg, hist, ta, scen, platforms, news, now):
    loc = now.astimezone(LPZ)
    day = loc.date().isoformat()
    if al.state.get("daily_sent") == day or not (cfg["alerts"]["daily_hour"] <= loc.hour < 12):
        return
    if al.fire("daily", "daily", daily_text(cfg, hist, ta, scen, platforms, news, now), cd_min=0):
        al.state["daily_sent"] = day


def maybe_weekly(al, fb, cfg, now):
    """Sábado por la mañana: informe de aciertos de la semana."""
    fc = cfg["feedback"]
    loc = now.astimezone(LPZ)
    key = loc.date().isoformat()
    if al.state.get("weekly_sent") == key or loc.weekday() != fc["weekday"] or loc.hour < fc["hour"]:
        return
    text = FB.report_text(fb, now, fc)
    if al.fire("weekly", "weekly", text, cd_min=0):
        al.state["weekly_sent"] = key
        fb["reports"].append({"t": iso(now), "text": text})


def evaluate_alerts(al, cfg, ta, scen, opps, news, platforms, price, prev_label):
    a = cfg["alerts"]
    for win, key, thr in ((30, "change_30m_pct", a["move_pct_30m"]), (120, "change_2h_pct", a["move_pct_2h"])):
        ch = ta.get(key)
        if ch is not None and abs(ch) >= thr:
            up = ch > 0
            al.fire(f"move:{'up' if up else 'down'}", "move",
                    f"{'📈 SUBE' if up else '📉 BAJA'} el USDT: {ch:+.2f}% en {win} min → {price:.3f} Bs")
    if a.get("price_above") and price >= a["price_above"]:
        al.fire("level:above", "level", f"🔔 USDT superó {a['price_above']} Bs (ahora {price:.3f})")
    if a.get("price_below") and price <= a["price_below"]:
        al.fire("level:below", "level", f"🔔 USDT cayó bajo {a['price_below']} Bs (ahora {price:.3f})")
    evaluate_opp_alerts(al, cfg, opps)
    if scen["strong"] and scen["label"] != prev_label:
        al.fire(f"signal:{scen['label']}", "signal",
                f"🧭 Escenario {scen['label']} (score {scen['score']:+.0f}, confianza {scen['confidence']}).\n- " +
                "\n- ".join(scen["reasons"][:4]))
    for s in build_signals(ta, scen, cfg):
        if s["type"] in ("compra", "venta"):
            al.fire(f"zone:{s['type']}", "zone", ("🟢 " if s["type"] == "compra" else "🔴 ") + s["text"],
                    ev={"typ": f"zona_{s['type']}", "p": price})
    evaluate_breakout(al, cfg, ta, price)
    evaluate_bollinger(al, cfg, ta, price)
    # noticias: solo las clave, nuevas (últimas horas) y con efecto claro; una cada ~90 min como máximo
    ncfg = cfg["news"]
    sent = al.state.setdefault("news_sent", [])
    for n in news:
        if not n.get("important") or (n["direction"] == "neutral" and n.get("cat") != "reg"):
            continue
        if (al.now - parse_iso(n["time"])).total_seconds() > ncfg["alert_max_age_hours"] * 3600:
            continue
        sig = set(n.get("sig") or [])
        if any(jaccard(sig, set(s["sig"])) >= 0.5 for s in sent):
            continue
        if al.fire("news:global", "news", "📰 Noticia clave\n" + news_line(n, al.now), cd_min=ncfg["alert_gap_min"]):
            sent.append({"sig": sorted(sig), "t": int(al.now.timestamp())})
            del sent[:-120]
        break


# ───────────────────────── Telegram ─────────────────────────

class Telegram:
    def __init__(self, token, chat, dry):
        self.token, self.chat, self.dry = token, chat, dry

    @property
    def enabled(self):
        return bool(self.token and self.chat)

    def api(self, method, payload, timeout=15, retries=1):
        return json.loads(http(f"https://api.telegram.org/bot{self.token}/{method}", payload,
                                timeout=timeout, retries=retries))

    def send(self, text, kb=None, edit=None):
        """Envía un mensaje (o edita uno existente si se da 'edit'). 'kb' = botones."""
        if self.dry or not self.enabled:
            print("[TG]", text.replace("\n", " | ")[:400])
            return None
        payload = {"chat_id": self.chat, "text": text[:4000], "disable_web_page_preview": True}
        if kb:
            payload["reply_markup"] = kb
        try:
            if edit:
                payload["message_id"] = edit
                return self.api("editMessageText", payload, retries=0)
            return self.api("sendMessage", payload)
        except Exception as e:  # noqa: BLE001
            body = ""
            try:
                body = e.read().decode("utf-8", "ignore")
            except Exception:  # noqa: BLE001
                pass
            if edit and "not modified" in body:
                return None
            if edit:
                return self.send(text, kb)  # el mensaje viejo ya no se puede editar: enviar uno nuevo
            print("Telegram error:", e, body[:200], file=sys.stderr)
            return None

    def answer(self, cb_id, text=""):
        if self.dry or not self.enabled:
            return
        try:
            self.api("answerCallbackQuery", {"callback_query_id": cb_id, "text": text}, retries=0)
        except Exception:  # noqa: BLE001
            pass


def make_report(kind, latest):
    pl, sc, ta, op = latest["platforms"], latest["scenario"], latest["ta"], latest["opportunities"]
    t = fmt_local(parse_iso(latest["generated_at"]))
    lines = []
    if kind in ("full", "opps"):
        if kind == "full":
            lines.append(f"📊 P2P USDT/BOB · {t}")
            for n, p in pl.items():
                lines.append(f"{n.title()}: compra {px(p['best_buy'], n)} · venta {px(p['best_sell'], n)} · ref {p['ref']:.3f}")
            ch = lambda v: f"{v:+.2f}%" if v is not None else "s/d"  # noqa: E731
            lines.append(f"Cambio: 30m {ch(ta.get('change_30m_pct'))} · 1h {ch(ta.get('change_1h_pct'))} · 24h {ch(ta.get('change_24h_pct'))}")
            lines.append(f"🧭 Escenario dominante: {sc['label']} (score {sc['score']:+.0f}, confianza {sc['confidence']})")
            for r in sc["reasons"][:5]:
                lines.append(f"  • {r}")
            if ta.get("rsi") is not None:
                lines.append(f"RSI {ta['rsi']:.0f}" + (f" · soporte {ta['support']:.2f} · resistencia {ta['resistance']:.2f}" if ta.get("support") else ""))
            for s in latest["signals"]:
                lines.append(f"➡ {s['text']}")
            lines.append("")
        lines.append("💱 Oportunidades (netas de comisión):")
        for o in op["arbitrage"]:
            lines.append(f"  Arbitraje {o['buy_on'].title()}→{o['sell_on'].title()}: {o['net_pct']:+.2f}% (≈ Bs {o['est_bob']:+,.0f})")
        for o in op["taker"]:
            lines.append(f"  Cruce directo {o['platform'].title()}: {o['net_pct']:+.2f}% (≈ Bs {o['est_bob']:+,.0f})")
        for o in op["maker"]:
            lines.append(f"  Maker {o['platform'].title()}: {o['net_pct']:+.2f}% por ciclo (≈ Bs {o['est_bob']:+,.0f})")
    if kind in ("full", "news"):
        if kind == "full":
            lines.append("")
        lines.append("📰 Noticias clave (Bolivia, préstamos, petróleo y flete):")
        top = latest["news"][: (3 if kind == "full" else 6)]
        if not top:
            lines.append("  Sin noticias importantes por ahora.")
        for i, n in enumerate(top, 1):
            lines.append(f"{i}) " + news_line(n))
    if kind == "full":
        lines.append("\nNo es asesoría financiera.")
    return "\n".join(lines)


# ───────────────────────── alertas del usuario + bot con botones ─────────────────────────

PLAT = {"binance": "Binance", "bybit": "Bybit"}
PLAT_CODES = {"b": ["binance"], "y": ["bybit"], "a": ["binance", "bybit"]}
PLAT_WORDS = {"binance": ["binance"], "bybit": ["bybit"], "ambas": ["binance", "bybit"],
              "ambos": ["binance", "bybit"], "todas": ["binance", "bybit"], "ambas.": ["binance", "bybit"]}
UP_WORDS_CMD = {"sube", "subir", "sobre", "arriba", "mayor", "encima", "supera", "alza"}
DOWN_WORDS_CMD = {"baja", "bajar", "bajo", "abajo", "menor", "debajo", "cae", "caiga"}
AUTO = {"move": "Movimiento del precio", "arb": "Arbitraje Binance↔Bybit", "taker": "Cruce directo",
        "maker": "Spread como maker", "signal": "Cambio de escenario", "news": "Noticias de impacto",
        "zone": "Zona de compra/venta (RSI)", "breakout": "Ruptura de soporte/resistencia",
        "bollinger": "Bandas de Bollinger", "daily": "Resumen diario", "weekly": "Informe semanal de aciertos"}
AUTO_SHORT = {"move": "Movimiento", "arb": "Arbitraje", "taker": "Cruce", "maker": "Maker",
              "signal": "Escenario", "news": "Noticias", "zone": "Zonas", "breakout": "Rupturas",
              "bollinger": "Bollinger", "daily": "Diario", "weekly": "Semanal"}
AUTO_ALIASES = {"movimiento": "move", "mov": "move", "arbitraje": "arb", "cruce": "taker", "taker": "taker",
                "maker": "maker", "senales": "signal", "senal": "signal", "escenario": "signal", "noticias": "news",
                "noticia": "news", "zona": "zone", "zonas": "zone", "ruptura": "breakout", "rupturas": "breakout",
                "bollinger": "bollinger", "diario": "daily", "resumen": "daily", "semanal": "weekly",
                "semana": "weekly", "informe": "weekly"}
FOREVER = 4102444800  # año 2100: "pausa hasta que yo la reanude"
MAX_RULES = 20
UI_VERSION = 3

BOT_COMMANDS = [
    ("menu", "Menú principal"), ("precios", "Precios y spread Binance/Bybit"), ("alertas", "Mis alertas activas"),
    ("nueva", "Crear una alerta con botones"), ("compra", "Alerta de precio de compra"),
    ("venta", "Alerta de precio de venta"), ("spread", "Ver spread o crear alerta de spread"),
    ("desactivar", "Desactivar una alerta o todas"), ("activar", "Activar una alerta o todas"),
    ("borrar", "Borrar una alerta"), ("pausar", "Pausar todas las alertas"),
    ("estado", "Análisis del mercado"), ("noticias", "Noticias que mueven el dólar"),
    ("semana", "Aciertos del sistema (informe semanal)"),
]
COMMAND_WORDS = {"compra", "venta", "spread", "desactivar", "activar", "borrar", "quitar", "eliminar", "pausar",
                 "reanudar", "mov", "precios", "precio", "alertas", "alerta", "menu", "ayuda", "help", "estado",
                 "noticias", "oportunidades", "nueva", "start", "analisis", "apagar", "encender", "semana",
                 "informe", "aciertos"}
KEYWORDS = [("aciertos", "semana"), ("informe semanal", "semana"), ("analisis mercado p2p", "estado"), ("mis alertas", "alertas"), ("nueva alerta", "nueva"),
            ("precios", "precios"), ("noticias", "noticias"), ("oportunidades", "oportunidades"),
            ("reanudar", "reanudar"), ("pausar", "pausar"), ("analisis", "estado")]

HELP_TEXT = (
    "🤖 Comandos\n\n"
    "Ver\n"
    "/precios → compra, venta y spread en Binance y Bybit\n"
    "/alertas → tus alertas activas (con botones)\n"
    "/estado → análisis del mercado · /noticias\n"
    "/semana → qué tanto acertó el sistema (informe semanal, también llega solo los sábados)\n\n"
    "Crear alertas\n"
    "/nueva → paso a paso con botones\n"
    "/compra binance sube 12 → avisa si el precio de COMPRA llega a 12 o más\n"
    "/compra bybit baja 11.50 → avisa si baja a 11.50 o menos\n"
    "/venta binance sube 12 · /venta bybit baja 11.5\n"
    "/spread binance 1.2 → avisa si el spread llega a 1.2% o más\n"
    "(en vez de binance o bybit puedes escribir «ambas»)\n\n"
    "Administrar\n"
    "/desactivar 3 · /activar 3 · /borrar 3 (3 = número de la alerta)\n"
    "/desactivar todas · /activar todas · /borrar todas\n"
    "/desactivar movimiento (también: arbitraje, cruce, maker, escenario, zonas, rupturas, bollinger, diario, semanal, noticias)\n"
    "/pausar 2h · /reanudar\n"
    "/mov 0.3 → sensibilidad del aviso de movimiento (% en 30 min)\n\n"
    "Compra = lo que pagas por cada USDT. Venta = lo que recibes por cada USDT. "
    "Spread = (compra ÷ venta − 1) en %."
)


def btn(text, data):
    return {"text": text, "callback_data": data}


def ikb(*rows):
    return {"inline_keyboard": [list(r) for r in rows]}


HOME = btn("🏠 Menú", "m")
MAIN_KB = ikb([btn("💱 Precios y spread", "p"), btn("🔔 Mis alertas", "l")],
              [btn("➕ Nueva alerta", "n"), btn("⏸ Pausar", "z")],
              [btn("📊 Análisis", "an"), btn("📰 Noticias", "nw")],
              [btn("🎯 Aciertos del sistema", "wk")])
REPLY_KB = {"keyboard": [[{"text": "💱 Precios"}, {"text": "🔔 Mis alertas"}],
                         [{"text": "➕ Nueva alerta"}, {"text": "⏸ Pausar"}],
                         [{"text": "📊 Análisis"}, {"text": "📰 Noticias"}]],
            "resize_keyboard": True, "is_persistent": True}


def parse_float(s):
    try:
        v = float(str(s).replace("%", "").replace(",", ".").strip())
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def spread_pct(p):
    """Spread = cuánto más caro es comprar que vender: (compra ÷ venta − 1) × 100."""
    return (p["best_buy"] / p["best_sell"] - 1) * 100


def maker_net_pct(p, fee):
    """Margen neto de comisión si publicas tus anuncios en el tope de cada lado."""
    return (p["best_buy"] * (1 - fee)) / (p["best_sell"] * (1 + fee)) - 1


def rule_text(r):
    p = PLAT[r["platform"]]
    if r["type"] == "spread":
        return f"{p} · spread ≥ {r['level']:g}%"
    side = "COMPRA" if r["side"] == "compra" else "VENTA"
    verb = "sube de" if r["dir"] == "sube" else "baja de"
    return f"{p} · precio de {side} {verb} {px(r['level'], r['platform'])}"


def rule_value(r, platforms):
    p = (platforms or {}).get(r["platform"])
    if not p:
        return None
    if r["type"] == "spread":
        return spread_pct(p)
    return p["best_buy"] if r["side"] == "compra" else p["best_sell"]


def rule_hit(r, v):
    if r["type"] == "spread" or r["dir"] == "sube":
        return v >= r["level"]
    return v <= r["level"]


def fmt_val(r, v):
    return f"{v:.2f}%" if r["type"] == "spread" else px(v, r["platform"])


def rule_alert_text(r, v, platforms, cfg):
    p = platforms[r["platform"]]
    f = cfg[f"fee_{r['platform']}_pct"] / 100
    pn = r["platform"]
    ctx = f"Compra {px(p['best_buy'], pn)} · Venta {px(p['best_sell'], pn)}"
    if r["type"] == "spread":
        body = f"Spread ahora: {v:.2f}% (neto tras comisión ≈ {maker_net_pct(p, f) * 100:+.2f}%)\n{ctx}"
    else:
        body = f"Ahora: {px(v, pn)} Bs\n{ctx}"
    return f"🔔 Alerta #{r['id']}: {rule_text(r)}\n{body}"


def evaluate_rules(al, state, platforms):
    """Dispara las alertas del usuario al cruzar su nivel; se rearman cuando el precio vuelve atrás."""
    for r in state.get("rules", []):
        if not r.get("active", True):
            r["on"] = False
            continue
        v = rule_value(r, platforms)
        if v is None:
            continue
        if rule_hit(r, v):
            if not r.get("on") and al.fire(f"rule:{r['id']}", "rule", rule_alert_text(r, v, platforms, al.cfg)):
                r["on"] = True
        else:
            r["on"] = False


def prices_text(platforms, cfg, ts, stale=False):
    L = [f"💱 USDT/BOB · {fmt_local(ts)}" + (" (dato anterior)" if stale else ""),
         "Compra = lo que pagas por USDT · Venta = lo que recibes"]
    for n in ("binance", "bybit"):
        p = platforms.get(n)
        L.append("")
        if not p:
            L.append(f"{PLAT[n]}: sin datos")
            continue
        f = cfg[f"fee_{n}_pct"] / 100
        L += [PLAT[n],
              f"  Compra: {px(p['best_buy'], n)}   Venta: {px(p['best_sell'], n)}",
              f"  Spread: {spread_pct(p):+.2f}% (Bs {p['best_buy'] - p['best_sell']:+.3f})",
              f"  Neto tras comisión si publicas tus anuncios: {maker_net_pct(p, f) * 100:+.2f}%"]
    arb = opportunities(platforms, cfg)["arbitrage"]
    if arb:
        L += ["", "Entre plataformas (neto de comisión)"]
        for a in arb:
            L.append(f"  {PLAT[a['buy_on']]} → {PLAT[a['sell_on']]}: {a['net_pct']:+.2f}% (≈ Bs {a['est_bob']:+,.0f})")
    return "\n".join(L)


class Bot:
    """Menú y comandos de Telegram. Guarda todo en 'state' (se publica en la rama data)."""

    def __init__(self, tg, cfg, state, ctx):
        self.tg, self.cfg, self.state, self.ctx = tg, cfg, state, ctx
        state.setdefault("rules", [])
        state.setdefault("next_id", 1)
        state.setdefault("auto_off", [])
        state.setdefault("muted_until", 0)
        if not state.get("mig_zone"):  # las zonas de compra/venta ahora tienen su propio interruptor
            if "signal" in state["auto_off"] and "zone" not in state["auto_off"]:
                state["auto_off"].append("zone")
            state["mig_zone"] = True

    # ── lectura de mensajes ──────────────────────────────────────────

    def poll(self, timeout=0):
        try:
            r = self.tg.api("getUpdates", {"offset": self.state.get("tg_offset", 0), "timeout": timeout,
                                           "allowed_updates": ["message", "callback_query"]},
                            timeout=timeout + 12, retries=0)
        except Exception as e:  # noqa: BLE001
            print("Telegram getUpdates error:", e, file=sys.stderr)
            return None
        n = 0
        for u in r.get("result") or []:
            self.state["tg_offset"] = u["update_id"] + 1
            try:
                self.process_update(u)
            except Exception as e:  # noqa: BLE001
                print("bot error:", repr(e), file=sys.stderr)
            n += 1
        return n

    def process_update(self, u):
        if "callback_query" in u:
            cb = u["callback_query"]
            msg = cb.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) != str(self.tg.chat):
                return
            self.tg.answer(cb.get("id"))
            self.on_callback(cb.get("data") or "", msg.get("message_id"))
            return
        m = u.get("message") or {}
        if str((m.get("chat") or {}).get("id")) != str(self.tg.chat):
            return
        if time.time() - m.get("date", 0) > 3600:
            return
        if (m.get("text") or "").strip():
            self.on_text(m["text"].strip())

    # ── texto y comandos ─────────────────────────────────────────────

    def on_text(self, raw):
        t = norm(raw).strip()
        t = re.sub(r"^(/\w+)@\w+", r"\1", t)
        parts = t.split()
        if not parts:
            return
        pend = self.state.get("pending")
        if pend and not t.startswith("/") and parse_float(t) is not None:
            self.state["pending"] = None
            return self.add_rules(pend["t"], PLAT_CODES[pend["p"]], pend.get("d"), parse_float(t))
        if t.startswith("/"):
            cmd, args = parts[0][1:], parts[1:]
        elif parts[0] in COMMAND_WORDS:
            cmd, args = parts[0], parts[1:]
        else:
            cmd, args = None, []
            for key, c in KEYWORDS:
                if key in t:
                    cmd = c
                    break
        self.state["pending"] = None
        h = {"start": lambda: self.menu(start=True), "menu": self.menu, "ayuda": self.help, "help": self.help,
             "precios": self.show_prices, "precio": self.show_prices, "alertas": self.show_alerts,
             "alerta": self.show_alerts, "nueva": self.wizard_start,
             "estado": lambda: self.report("full"), "analisis": lambda: self.report("full"),
             "oportunidades": lambda: self.report("opps"), "noticias": lambda: self.report("news"),
             "reanudar": self.resume, "semana": self.weekly_report, "informe": self.weekly_report,
             "aciertos": self.weekly_report}.get(cmd)
        if h:
            return h()
        if cmd in ("compra", "venta"):
            return self.cmd_price(cmd, args)
        if cmd == "spread":
            return self.cmd_spread(args) if args else self.show_prices()
        if cmd in ("desactivar", "apagar"):
            return self.cmd_toggle(args, False)
        if cmd in ("activar", "encender"):
            return self.cmd_toggle(args, True)
        if cmd in ("borrar", "quitar", "eliminar"):
            return self.cmd_delete(args)
        if cmd == "pausar":
            return self.cmd_pause(args)
        if cmd == "mov":
            return self.cmd_mov(args)
        self.tg.send("No entendí 🤔 Usa los botones o escribe /ayuda.", MAIN_KB)

    def parse_args(self, args):
        plats, d, level = None, None, None
        for a in args:
            if a in PLAT_WORDS:
                plats = PLAT_WORDS[a]
            elif a in UP_WORDS_CMD:
                d = "u"
            elif a in DOWN_WORDS_CMD:
                d = "d"
            elif parse_float(a) is not None:
                level = parse_float(a)
        return plats or ["binance", "bybit"], d, level

    def cmd_price(self, side, args):
        plats, d, level = self.parse_args(args)
        if d is None or level is None:
            return self.tg.send(f"Uso: /{side} binance sube 12   ·   /{side} bybit baja 11.50\n"
                                "(en vez de binance o bybit puedes poner «ambas»)", ikb([HOME]))
        self.add_rules("c" if side == "compra" else "v", plats, d, level)

    def cmd_spread(self, args):
        plats, _, level = self.parse_args(args)
        if level is None:
            return self.tg.send("Uso: /spread binance 1.2   ·   /spread ambas 1\n"
                                "Te aviso cuando el spread llegue a ese % o más.", ikb([HOME]))
        self.add_rules("s", plats, None, level)

    def find_rule(self, arg):
        if arg.isdigit():
            for r in self.state["rules"]:
                if r["id"] == int(arg):
                    return r
        return None

    def cmd_toggle(self, args, on):
        st, verb = self.state, ("activada" if on else "desactivada")
        if not args:
            return self.tg.send("Uso: /desactivar 3 · /desactivar movimiento · /desactivar todas\n"
                                "(el número lo ves en /alertas)", ikb([btn("🔔 Mis alertas", "l"), HOME]))
        w = args[0]
        kb = ikb([btn("🔔 Mis alertas", "l"), HOME])
        if w in ("todas", "todo", "todos"):
            if on:
                st["muted_until"], st["auto_off"] = 0, []
                for r in st["rules"]:
                    r["active"] = True
                return self.tg.send("✅ Todas las alertas activadas.", kb)
            st["muted_until"] = FOREVER
            return self.tg.send("🔇 Todas las alertas desactivadas (tus configuraciones se conservan).\n"
                                "Para volver: /activar todas", kb)
        r = self.find_rule(w)
        if r:
            r["active"], r["on"] = on, False
            extra = "\n⚠️ Ojo: las alertas están en pausa general (/activar todas)." if on and self.muted() else ""
            return self.tg.send(f"{'✅' if on else '⏹'} Alerta #{r['id']} {verb}: {rule_text(r)}{extra}", kb)
        key = AUTO_ALIASES.get(w)
        if key:
            if on and key in st["auto_off"]:
                st["auto_off"].remove(key)
            elif not on and key not in st["auto_off"]:
                st["auto_off"].append(key)
            return self.tg.send(f"{'✅' if on else '⏹'} {AUTO[key]}: {verb}.", kb)
        self.tg.send("No encontré esa alerta. Mira los números en /alertas.", kb)

    def cmd_delete(self, args):
        st, kb = self.state, ikb([btn("🔔 Mis alertas", "l"), HOME])
        if not args:
            return self.tg.send("Uso: /borrar 3 · /borrar todas", kb)
        if args[0] in ("todas", "todo", "todos"):
            n = len(st["rules"])
            st["rules"] = []
            return self.tg.send(f"🗑 Borré {n} alerta(s) tuya(s).", kb)
        r = self.find_rule(args[0])
        if not r:
            return self.tg.send("No encontré esa alerta. Mira los números en /alertas.", kb)
        st["rules"].remove(r)
        self.tg.send(f"🗑 Alerta #{r['id']} borrada: {rule_text(r)}", kb)

    def cmd_pause(self, args):
        if not args:
            return self.pause_menu()
        m = re.match(r"^(\d+(?:[.,]\d+)?)(h|hr|hrs|hora|horas|m|min|minutos?)?$", "".join(args))
        if not m:
            return self.tg.send("Uso: /pausar 2h · /pausar 30m", ikb([HOME]))
        n = float(m.group(1).replace(",", "."))
        secs = n * 60 if (m.group(2) or "h").startswith("m") else n * 3600
        self.pause_for(secs)

    def cmd_mov(self, args):
        v = parse_float(args[0]) if args else None
        if v is None or not (0.05 <= v <= 5):
            cur = self.cfg["alerts"]["move_pct_30m"]
            return self.tg.send(f"Uso: /mov 0.3 → avisa si el precio se mueve 0.3% en 30 min (y 0.6% en 2 h).\n"
                                f"Ahora: {cur}% (entre 0.05 y 5).", ikb([HOME]))
        st = self.state.setdefault("settings", {})
        st["move_pct_30m"], st["move_pct_2h"] = round(v, 3), round(v * 2, 3)
        self.cfg["alerts"].update(st)
        self.tg.send(f"✅ Aviso de movimiento: {v:g}% en 30 min o {v * 2:g}% en 2 h.", ikb([HOME]))

    # ── pausa ────────────────────────────────────────────────────────

    def muted(self):
        return time.time() < self.state.get("muted_until", 0)

    def pause_for(self, secs, edit=None):
        self.state["muted_until"] = time.time() + secs
        until = datetime.fromtimestamp(self.state["muted_until"], timezone.utc)
        self.tg.send(f"⏸ Alertas en pausa hasta las {fmt_local(until)}. Para volver antes: /reanudar",
                     ikb([btn("▶ Reanudar", "u"), HOME]), edit)

    def pause_menu(self, edit=None):
        self.tg.send("⏸ ¿Por cuánto tiempo pauso TODAS las alertas?",
                     ikb([btn("1 h", "z:1"), btn("4 h", "z:4"), btn("8 h", "z:8")],
                         [btn("Hasta que las reanude", "z:f")], [btn("▶ Reanudar ahora", "u"), HOME]), edit)

    def resume(self, edit=None):
        self.state["muted_until"] = 0
        self.tg.send("▶ Alertas reanudadas.", ikb([btn("🔔 Mis alertas", "l"), HOME]), edit)

    # ── pantallas ────────────────────────────────────────────────────

    def help(self, edit=None):
        self.tg.send(HELP_TEXT, ikb([HOME]), edit)

    def menu(self, start=False, edit=None):
        n = sum(1 for r in self.state["rules"] if r.get("active", True))
        if start:
            self.tg.send("👋 Menú actualizado: nuevas alertas (rupturas, Bollinger, resumen diario) y el informe de aciertos /semana.\n"
                         "Usa los botones de abajo, o escribe «/» para ver todos los comandos.", REPLY_KB)
        txt = "🤖 Monitor P2P USDT/BOB\n"
        txt += "🔇 Alertas en pausa\n" if self.muted() else ""
        txt += f"Tienes {n} alerta(s) tuya(s) activa(s).\n¿Qué quieres hacer?"
        self.tg.send(txt, MAIN_KB, None if start else edit)

    def show_prices(self, edit=None):
        platforms, ts, stale = self.ctx["refresh"]()
        kb = ikb([btn("🔄 Actualizar", "p"), btn("➕ Alerta", "n")], [btn("🔔 Mis alertas", "l"), HOME])
        if not platforms:
            return self.tg.send("No pude leer los precios ahora (Binance y Bybit no respondieron). Prueba en un minuto.",
                                kb, edit)
        self.tg.send(prices_text(platforms, self.cfg, ts, stale), kb, edit)

    def report(self, kind):
        lat = self.ctx["latest"]()
        if not lat or "scenario" not in lat:
            return self.tg.send("Todavía no tengo datos de mercado completos. Prueba en unos minutos.", ikb([HOME]))
        self.tg.send(make_report(kind, lat), ikb([HOME]))

    def weekly_report(self, edit=None):
        fn = self.ctx.get("weekly")
        self.tg.send(fn() if fn else "Informe no disponible.", ikb([HOME]), edit)

    def show_alerts(self, edit=None):
        st, a = self.state, self.cfg["alerts"]
        mu = st.get("muted_until", 0)
        L = ["🔔 Tus alertas"]
        if time.time() < mu:
            L.append("🔇 TODAS en pausa " + ("hasta que las reanudes" if mu >= FOREVER - 1 else
                                            f"hasta las {fmt_local(datetime.fromtimestamp(mu, timezone.utc))}"))
        else:
            L.append("✅ Funcionando")
        L += ["", "Las tuyas:"]
        if not st["rules"]:
            L.append("  (ninguna todavía) → toca ➕ Nueva alerta")
        for r in st["rules"]:
            tail = " · ya disparada (se rearma sola)" if r.get("on") and r.get("active", True) else ""
            L.append(f"#{r['id']} {'✅' if r.get('active', True) else '⏹'} {rule_text(r)}{tail}")
        detail = {"move": f"({a['move_pct_30m']:g}% en 30 min · {a['move_pct_2h']:g}% en 2 h)",
                  "arb": f"(neto ≥ {a['arb_net_pct']:g}%)", "taker": f"(neto ≥ {a['taker_net_pct']:g}%)",
                  "maker": f"(neto ≥ {a['maker_net_pct']:g}%)",
                  "zone": f"(RSI ≥{a['rsi_overbought']} / ≤{a['rsi_oversold']} cerca del techo / piso)",
                  "breakout": f"(>{a['breakout_margin_pct']:g}% fuera del rango de 3 días)",
                  "bollinger": "(precio en la banda de 20 h)",
                  "daily": f"(cada día {a['daily_hour']}:00)", "weekly": "(sábados)"}
        L += ["", "Automáticas:"]
        for k, name in AUTO.items():
            rp = f" ×{a['repeat_times']}" if k in a.get("repeat_kinds", []) else ""
            L.append(f"{'⏹' if k in st['auto_off'] else '✅'} {name} {detail.get(k, '')}{rp}".rstrip())
        rows = []
        for r in st["rules"]:
            rows.append([btn(f"{'⏹ Pausar' if r.get('active', True) else '▶ Activar'} #{r['id']}", f"t:{r['id']}"),
                         btn(f"🗑 Borrar #{r['id']}", f"d:{r['id']}")])
        ks = list(AUTO)
        for i in range(0, len(ks), 3):
            rows.append([btn(f"{'⏹' if k in st['auto_off'] else '✅'} {AUTO_SHORT[k]}", f"x:{k}") for k in ks[i:i + 3]])
        rows.append([btn("➕ Nueva alerta", "n"),
                     btn("▶ Reanudar" if time.time() < mu else "⏸ Pausar todo", "u" if time.time() < mu else "z")])
        rows.append([HOME])
        self.tg.send("\n".join(L), ikb(*rows), edit)

    # ── asistente de nueva alerta ────────────────────────────────────

    def cur_price(self, plat, typ):
        p = (self.ctx["platforms"]() or {}).get(plat)
        return None if not p else (p["best_buy"] if typ == "c" else p["best_sell"])

    def wizard_start(self, edit=None):
        self.state["pending"] = None
        self.tg.send("➕ Nueva alerta\n\n• Precio de COMPRA: lo que pagas por cada USDT.\n"
                     "• Precio de VENTA: lo que recibes por cada USDT.\n"
                     "• Spread: diferencia % entre ambos (más alto = más margen para ti).\n\n"
                     "¿Sobre qué quieres la alerta?",
                     ikb([btn("Precio de compra", "n:c"), btn("Precio de venta", "n:v")], [btn("Spread", "n:s")],
                         [HOME]), edit)

    def wizard(self, a, edit):
        if len(a) < 2 or a[1] not in ("c", "v", "s"):
            return self.wizard_start(edit)
        typ = a[1]
        tname = {"c": "precio de COMPRA", "v": "precio de VENTA", "s": "spread"}[typ]
        if len(a) == 2:
            return self.tg.send(f"Alerta de {tname}\n¿En qué plataforma?",
                                ikb([btn("Binance", f"n:{typ}:b"), btn("Bybit", f"n:{typ}:y"),
                                     btn("Ambas", f"n:{typ}:a")], [btn("⬅️ Atrás", "n"), HOME]), edit)
        plat = a[2]
        if plat not in PLAT_CODES:
            return self.wizard_start(edit)
        plats = PLAT_CODES[plat]
        pname = {"b": "Binance", "y": "Bybit", "a": "Binance y Bybit"}[plat]
        if typ == "s":
            if len(a) == 3:
                self.state["pending"] = {"t": "s", "p": plat}
                return self.tg.send(f"Spread en {pname}\nTe aviso cuando sea igual o mayor a:\n"
                                    "Elige un valor o escribe el % (ej. 1.2).\n"
                                    "💡 Con comisión de 0.25% por lado, bajo 0.5% no hay ganancia.",
                                    ikb([btn(f"{v}%", f"n:s:{plat}:{v}") for v in ("0.5", "1", "1.5", "2")],
                                        [btn("⬅️ Atrás", "n:s"), HOME]), edit)
            self.state["pending"] = None
            return self.add_rules("s", plats, None, parse_float(a[3]), edit)
        if len(a) == 3:
            return self.tg.send(f"{pname} · {tname}\n¿Cuándo te aviso?",
                                ikb([btn("📈 Cuando sube de", f"n:{typ}:{plat}:u"),
                                     btn("📉 Cuando baja de", f"n:{typ}:{plat}:d")],
                                    [btn("⬅️ Atrás", f"n:{typ}"), HOME]), edit)
        d = a[3]
        if d not in ("u", "d"):
            return self.wizard_start(edit)
        if len(a) == 4:
            cur = self.cur_price(plats[0], typ)
            self.state["pending"] = {"t": typ, "p": plat, "d": d}
            sign = 1 if d == "u" else -1
            rows = []
            if cur:
                rows.append([btn(px(cur + sign * x, plats[0]), f"n:{typ}:{plat}:{d}:{px(cur + sign * x, plats[0])}")
                             for x in (0.05, 0.10, 0.20)])
            rows.append([btn("⬅️ Atrás", f"n:{typ}:{plat}"), HOME])
            return self.tg.send(f"{pname} · {tname} {'sube de' if d == 'u' else 'baja de'} …\n"
                                + (f"Ahora está en {px(cur, plats[0])}\n" if cur else "")
                                + "Elige un nivel o escribe el precio (ej. 12.00).", ikb(*rows), edit)
        self.state["pending"] = None
        self.add_rules(typ, plats, d, parse_float(a[4]), edit)

    def add_rules(self, typ, plats, d, level, edit=None):
        kb = ikb([btn("🔔 Mis alertas", "l"), btn("➕ Otra", "n")], [HOME])
        if level is None or level <= 0 or (typ == "s" and level > 50) or (typ != "s" and level > 10000):
            return self.tg.send("Ese valor no es válido. Ejemplo: 12.00 (o 1.2 para un spread en %).", kb, edit)
        st, platforms = self.state, self.ctx["platforms"]()
        made, notes = [], []
        for pl in plats:
            if len(st["rules"]) >= MAX_RULES:
                notes.append(f"Máximo {MAX_RULES} alertas: borra alguna para crear más.")
                break
            r = {"id": st["next_id"], "type": "spread" if typ == "s" else "price", "platform": pl,
                 "level": round(level, 4), "active": True, "on": False}
            if typ != "s":
                r["side"], r["dir"] = ("compra" if typ == "c" else "venta"), ("sube" if d == "u" else "baja")
            v = rule_value(r, platforms)
            if v is not None and rule_hit(r, v):
                r["on"] = True
                notes.append(f"Ojo: en {PLAT[pl]} ya está en {fmt_val(r, v)}, o sea que ya se cumple. "
                             "Te aviso la próxima vez que vuelva a cruzar ese nivel.")
            st["next_id"] += 1
            st["rules"].append(r)
            made.append(r)
        if not made:
            return self.tg.send("\n".join(notes), kb, edit)
        txt = "✅ Alerta creada:\n" + "\n".join(f"#{r['id']} {rule_text(r)}" for r in made)
        if notes:
            txt += "\n\n" + "\n".join(notes)
        self.tg.send(txt, kb, edit)

    # ── botones ──────────────────────────────────────────────────────

    def on_callback(self, data, mid):
        a, st = data.split(":"), self.state
        k = a[0]
        if k == "m":
            return self.menu(edit=mid)
        if k == "p":
            return self.show_prices(edit=mid)
        if k == "l":
            return self.show_alerts(edit=mid)
        if k == "n":
            return self.wizard(a, mid)
        if k in ("t", "d") and len(a) == 2 and a[1].isdigit():
            r = self.find_rule(a[1])
            if r and k == "t":
                r["active"], r["on"] = not r.get("active", True), False
            elif r:
                st["rules"].remove(r)
            return self.show_alerts(edit=mid)
        if k == "x" and len(a) == 2 and a[1] in AUTO:
            if a[1] in st["auto_off"]:
                st["auto_off"].remove(a[1])
            else:
                st["auto_off"].append(a[1])
            return self.show_alerts(edit=mid)
        if k == "z":
            if len(a) == 1:
                return self.pause_menu(mid)
            if a[1] == "f":
                st["muted_until"] = FOREVER
                return self.show_alerts(edit=mid)
            if a[1].isdigit():
                return self.pause_for(int(a[1]) * 3600, mid)
        if k == "u":
            return self.resume(mid)
        if k == "an":
            return self.report("full")
        if k == "nw":
            return self.report("news")
        if k == "op":
            return self.report("opps")
        if k == "wk":
            return self.weekly_report()

    # ── primera vez tras actualizar ──────────────────────────────────

    def first_run_setup(self):
        if self.state.get("ui_v") == UI_VERSION:
            return
        self.state["ui_v"] = UI_VERSION
        try:
            self.tg.api("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in BOT_COMMANDS]},
                        retries=0)
        except Exception as e:  # noqa: BLE001
            print("setMyCommands:", e, file=sys.stderr)
        self.menu(start=True)


# ───────────────────────── modo demo ─────────────────────────

def demo_ads(base, rnd, n=14):
    def one(price):
        return {"price": round(price, 2), "available": rnd.uniform(80, 1500), "min": 100.0,
                "max": rnd.choice([3000, 8000, 20000]), "merchant": f"Trader{rnd.randint(10, 99)}",
                "orders": rnd.randint(40, 900), "rate": rnd.uniform(0.93, 1.0)}
    buy = [one(base * (1 + 0.0010 * i + rnd.uniform(-0.0006, 0.0006))) for i in range(n)]
    sell = [one(base * (1 - 0.0004 - 0.0011 * i + rnd.uniform(-0.0006, 0.0006))) for i in range(n)]
    return buy, sell


def demo_data(cfg, hist_len_h=340):
    rnd = random.Random(7)
    now = now_utc().replace(second=0, microsecond=0)
    p, series = 11.55, []
    for h in range(hist_len_h, 0, -1):
        p *= 1 + rnd.gauss(0.0002, 0.0014)
        series.append((now - timedelta(hours=h), p))
    hist = [{"t": iso(t), "br": round(v, 4), "seed": 1} for t, v in series]
    for k in range(288, 0, -1):
        p *= 1 + rnd.gauss(0.00005, 0.0004)
        t = now - timedelta(minutes=5 * k)
        hist.append({"t": iso(t), "br": round(p, 4), "bb": round(p * 1.002, 3), "bs": round(p * 0.999, 3),
                     "yr": round(p * 1.001, 4), "yb": round(p * 1.003, 3), "ys": round(p * 0.9995, 3)})
    pl = {}
    for name, off in (("binance", 0.0), ("bybit", 0.004)):
        b, s = demo_ads(p * (1 + off), rnd)
        pl[name] = summarize(b, s, cfg, name)
    mk = lambda t, h, sc, cat, lab, imp: {  # noqa: E731
        "title": t, "link": "https://example.com", "source": "Demo", "time": iso(now - timedelta(hours=h)),
        "score": sc, "cat": cat, "cat_label": lab, "relevance": 12 if imp else 8, "n_sources": 2 if imp else 1,
        "important": imp, "impact": "alto" if imp else "medio",
        "direction": "sube" if sc > 0 else ("baja" if sc < 0 else "neutral"), "sig": t.lower().split()[:8]}
    news = [mk("Demo: BID aprueba préstamo para Bolivia", 3, -2, "fin", CAT_LABEL["fin"], True),
            mk("Demo: Persisten bloqueos y escasez de dólares en Bolivia", 5, 3, "fx", CAT_LABEL["fx"], True),
            mk("Demo: Fletes de contenedores suben 20% por el Mar Rojo", 9, 2, "freight", CAT_LABEL["freight"], False)]
    return pl, news, hist


# ───────────────────────── programa principal ─────────────────────────

def get_platforms(cfg, errors, ws_token):
    platforms, used_ws = {}, False
    b = collect_binance(cfg, errors)
    if b is None and ws_token:
        b = wavesoft_latest(cfg, ws_token, errors)
        used_ws = b is not None
    if b:
        platforms["binance"] = b
    y = collect_bybit(cfg, errors)
    if y:
        platforms["bybit"] = y
    return platforms, used_ws


def live_loop(bot, al, state, cfg, refresh, deadline, data):
    """Mantiene el bot escuchando Telegram (respuesta casi inmediata) y revisa tus alertas cada minuto."""
    tick = time.time() + cfg["live_refresh_seconds"]
    while True:
        remaining = deadline - time.time()
        if remaining <= 4:
            break
        try:
            al.flush_due()
            due = al.next_due()
            # con repeticiones pendientes no se espera Telegram más de lo necesario
            wait = 20 if due is None else max(0, min(20, due - time.time()))
            n = bot.poll(timeout=int(max(0, min(wait, remaining - 3))))
            if n is None:
                time.sleep(3)
            elif n:
                write_json(data / "state.json", state)
            al.flush_due()
            if time.time() >= tick and deadline - time.time() > 10:
                tick = time.time() + cfg["live_refresh_seconds"]
                platforms, _ts, stale = refresh()
                if platforms and not stale:
                    al.now = now_utc()
                    evaluate_rules(al, state, platforms)
                    evaluate_opp_alerts(al, cfg, opportunities(platforms, cfg))
                    write_json(data / "state.json", state)
        except Exception as e:  # noqa: BLE001
            print("live error:", repr(e), file=sys.stderr)
            time.sleep(3)
    al.flush_due(force=True)  # que no se pierdan repeticiones al terminar la ejecución


def main():
    t0 = time.time()
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data", help="carpeta con el estado previo y salida")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--live", type=int, default=None, help="segundos escuchando Telegram (0 = no escuchar)")
    args = ap.parse_args()

    cfg = json.loads(json.dumps(load_config()))
    data = Path(args.data)
    data.mkdir(parents=True, exist_ok=True)
    now = now_utc().replace(microsecond=0)
    errors = []

    hist = read_json(data / "history.json", [])
    state = read_json(data / "state.json", {})
    cfg["alerts"].update(state.get("settings", {}))
    fb = None
    if FB and cfg["feedback"].get("enabled"):
        fb = read_json(data / "feedback.json", None) or FB.new_store()
        for k_, v_ in FB.new_store().items():
            fb.setdefault(k_, v_)
    live = args.live if args.live is not None else int(os.environ.get("LIVE_SECONDS") or cfg["live_seconds"])
    tg = Telegram(os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID"), args.dry_run or args.demo)
    ws_token = os.environ.get("WAVESOFT_TOKEN")
    used_wavesoft = False
    news, latest = [], read_json(data / "latest.json", {})

    if args.demo:
        platforms, news, hist = demo_data(cfg)
    else:
        platforms, used_wavesoft = get_platforms(cfg, errors, ws_token)
        news = fetch_news(cfg, errors, state.setdefault("news_cache", {}))
        if ws_token and not state.get("seeded"):
            seed = wavesoft_seed(ws_token, errors)
            if seed:
                have = {r["t"][:13] for r in hist}
                hist = sorted(hist + [r for r in seed if r["t"][:13] not in have], key=lambda r: r["t"])
                state["seeded"] = True
                used_wavesoft = True

    al = Alerter(cfg, state, tg.send, now)

    if not platforms:
        latest["status"] = {"ok": False, "errors": errors, "checked_at": iso(now)}
        write_json(data / "latest.json", latest)
        print("Sin datos de ninguna plataforma:", errors, file=sys.stderr)
    else:
        # registro del historial
        rec = {"t": iso(now)}
        if "binance" in platforms:
            p = platforms["binance"]
            rec.update({"br": round(p["ref"], 4), "bb": p["best_buy"], "bs": p["best_sell"]})
        if "bybit" in platforms:
            p = platforms["bybit"]
            rec.update({"yr": round(p["ref"], 4), "yb": p["best_buy"], "ys": p["best_sell"]})
        hist.append(rec)
        hist = hist[-cfg["history_max"]:]

        price = rec.get("br") or rec.get("yr")
        ta = technical_analysis(hist, price, now)
        pressure = depth_pressure(platforms)
        scen = build_scenario(ta, news, pressure, cfg.get("scenario_weights"), cfg["news"].get("cat_weight"))
        opps = opportunities(platforms, cfg)
        signals = build_signals(ta, scen, cfg)
        prev_label = state.get("scenario_label")

        latest = {
            "generated_at": iso(now),
            "config": {k: cfg[k] for k in ("fee_binance_pct", "fee_bybit_pct", "capital_bob", "capital_usdt",
                                           "trade_amount_bob", "usdt_transfer_fee", "transfer_size_usdt")},
            "price": price,
            "platforms": platforms,
            "ta": ta,
            "scenario": scen,
            "opportunities": opps,
            "signals": signals,
            "news": news[:15],
            "alerts": [],
            "status": {"ok": True, "errors": errors, "demo": args.demo, "wavesoft": used_wavesoft,
                       "telegram": tg.enabled, "checked_at": iso(now)},
        }

        evaluate_alerts(al, cfg, ta, scen, opps, news, platforms, price, prev_label)
        evaluate_rules(al, state, platforms)
        maybe_daily(al, cfg, hist, ta, scen, platforms, news, now)
        if fb is not None:
            try:
                FB.log_pred(fb, now, price, scen, ta, news)
                FB.log_news_events(fb, now, price, news)
                for ev in al.events:
                    FB.log_event(fb, now, ev["typ"], ev["p"])
                FB.resolve(fb, hist, now, cfg["feedback"])
                FB.prune(fb, now, cfg["feedback"]["keep_days"])
                maybe_weekly(al, fb, cfg, now)
            except Exception as e:  # noqa: BLE001
                errors.append(f"Retroalimentación: {e}")
            write_json(data / "feedback.json", fb)
        state["scenario_label"] = scen["label"] if scen["strong"] else (prev_label or scen["label"])
        latest["alerts"] = list(reversed(state.get("log", [])))[:30]

        # series ligeras para el gráfico del panel
        m5 = [r for r in hist if not r.get("seed")][-288:]
        chart = {"m5": m5, "h1": [{"t": t, "p": p} for t, p in hourly_series(hist)][-336:]}
        write_json(data / "latest.json", latest)
        write_json(data / "chart.json", chart)
        write_json(data / "history.json", hist)
        print(f"OK {fmt_local(now)} ref={price:.3f} escenario={scen['label']}({scen['score']:+.0f}) "
              f"plataformas={list(platforms)} errores={len(errors)}")

    # ── Telegram interactivo: menú, botones y alertas del usuario ──
    cur = {"platforms": platforms, "ts": now}

    def refresh():
        p, _ = get_platforms(cfg, [], ws_token)
        if p:
            cur["platforms"], cur["ts"] = p, now_utc()
        return cur["platforms"], cur["ts"], not p

    def weekly():
        if fb is None:
            return "El informe de aciertos no está activo (falta feedback.py en el repositorio)."
        return FB.report_text(fb, now_utc(), cfg["feedback"], partial=True)

    bot = Bot(tg, cfg, state, {"platforms": lambda: cur["platforms"], "refresh": refresh,
                               "latest": lambda: latest, "weekly": weekly})
    if tg.enabled and not args.demo and not args.dry_run:
        bot.first_run_setup()
        if live > 0:
            live_loop(bot, al, state, cfg, refresh, t0 + live, data)
        else:
            bot.poll(0)
            al.flush_due(force=True)
    write_json(data / "state.json", state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
