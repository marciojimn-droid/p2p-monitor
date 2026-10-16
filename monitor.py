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
import json
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
    "maker_rank": 3,
    "history_max": 4000,
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
                         "signal": 120, "level": 120, "news": 360},
    },
    "news": {"enabled": True, "max_age_hours": 48, "queries": []},
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


def quality_filter(ads, amount, cfg):
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

UP_WORDS = {  # empujan el dólar/USDT al alza
    "escasez de dolar": 2, "falta de dolar": 2, "faltan dolares": 2, "devaluacion": 2, "dolar sube": 2,
    "dolar paralelo sube": 2, "suben el dolar": 2, "sube el dolar": 2, "bloqueo": 1, "bloqueos": 1, "paro": 1,
    "crisis": 1, "inflacion": 1, "reservas caen": 2, "caida de reservas": 2, "reservas bajan": 2,
    "deficit": 1, "subvencion": 1, "restriccion": 1, "incertidumbre": 1, "conflicto": 1, "protesta": 1,
    "desabastecimiento": 1, "filas": 1, "racionamiento": 1, "sanciones": 1, "default": 2,
    "calificacion negativa": 2, "rebaja de calificacion": 2, "tension": 1, "corrida": 2, "cepo": 1,
}
DOWN_WORDS = {  # empujan el dólar/USDT a la baja
    "dolar baja": 2, "baja el dolar": 2, "bajan el dolar": 2, "cae el dolar": 2, "liberacion de dolares": 2,
    "ingreso de dolares": 2, "mas dolares": 1, "oferta de dolares": 1, "credito": 1, "desembolso": 2,
    "prestamo": 1, "fmi": 1, "acuerdo": 1, "estabilidad cambiaria": 2, "reservas suben": 2,
    "aumentan las reservas": 2, "exportaciones crecen": 1, "normaliza": 1, "calma": 1, "tranquilidad": 1,
    "unificacion cambiaria": 2, "inversion": 1, "reactivacion": 1, "abastecimiento normal": 1,
}


def score_headline(title):
    t = norm(title)
    up = sum(w for k, w in UP_WORDS.items() if k in t)
    down = sum(w for k, w in DOWN_WORDS.items() if k in t)
    return max(-3, min(3, up - down))


def fetch_news(cfg, errors):
    news_cfg = cfg["news"]
    if not news_cfg.get("enabled"):
        return []
    seen, items = set(), []
    cutoff = now_utc() - timedelta(hours=news_cfg["max_age_hours"])
    ok = 0
    for q in news_cfg["queries"]:
        url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q + " when:2d")
               + "&hl=es-419&gl=BO&ceid=BO:es-419")
        try:
            root = ET.fromstring(http(url, timeout=15, retries=1))
            ok += 1
        except Exception as e:  # noqa: BLE001
            errors.append(f"Noticias ({q[:25]}…): {e}")
            continue
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            key = re.sub(r"\W+", " ", norm(title.rsplit(" - ", 1)[0]))[:90]
            if not title or key in seen:
                continue
            try:
                dt = parsedate_to_datetime(it.findtext("pubDate"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except Exception:  # noqa: BLE001
                dt = now_utc()
            if dt < cutoff:
                continue
            seen.add(key)
            src = (it.findtext("source") or "").strip()
            sc = score_headline(title)
            items.append({"title": title, "link": (it.findtext("link") or "").strip(), "source": src,
                          "time": iso(dt), "score": sc,
                          "impact": "alto" if abs(sc) >= 2 else ("medio" if abs(sc) == 1 else "bajo"),
                          "direction": "sube" if sc > 0 else ("baja" if sc < 0 else "neutral")})
    if news_cfg["queries"] and ok == 0:
        errors.append("Noticias: ninguna fuente respondió")
    items.sort(key=lambda x: x["time"], reverse=True)
    return items[:40]


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


def build_scenario(ta, news, pressure):
    comps, reasons = [], []

    def add(name, value, weight, text):
        comps.append((name, value, weight))
        if abs(value) > 0.05 and text:
            reasons.append(text)

    r = ta.get("rsi")
    if r is not None:
        v = 0.6 if r < 30 else 0.2 if r < 40 else -0.6 if r > 70 else -0.2 if r > 60 else 0.0
        add("rsi", v, 15, f"RSI {r:.0f}: " + ("sobrevendido, posible rebote" if v > 0.3 else
                                              "zona baja" if v > 0 else
                                              "sobrecomprado, posible corrección" if v < -0.3 else "zona alta"))
    m = ta.get("macd")
    if m:
        h, hp = m["hist"], m["hist_prev"]
        v = 1.0 if (h > 0 and h > hp) else 0.3 if h > 0 else -1.0 if (h < 0 and h < hp) else -0.3
        add("macd", v, 20, "MACD " + ("alcista y acelerando" if v == 1 else "positivo pero perdiendo fuerza" if v == 0.3
                                      else "bajista y acelerando" if v == -1 else "negativo pero mejorando"))
    e20, e50, price = ta.get("ema20"), ta.get("ema50"), ta.get("price")
    if e20 and price:
        v = 0.0
        v += 0.5 if price > e20 else -0.5
        if e50:
            v += 0.5 if e20 > e50 else -0.5
        add("tendencia", v, 20, "Tendencia " + ("alcista (precio sobre medias)" if v > 0 else "bajista (precio bajo medias)"))
    mom = ta.get("change_3h_pct")
    if mom is not None:
        v = clamp(mom / 0.6)
        add("momentum", v, 15, f"Momentum 3h {mom:+.2f}%")
    add("profundidad", clamp(pressure * 2), 10,
        "Más demanda que oferta de USDT en los libros" if pressure > 0.1 else
        "Más oferta que demanda de USDT en los libros" if pressure < -0.1 else "")
    if news:
        recent = [n for n in news if n["score"] != 0][:10]
        s = sum(n["score"] for n in recent)
        v = clamp(s / 4)
        add("noticias", v, 20, ("Noticias con sesgo alcista para el dólar" if v > 0 else "Noticias con sesgo bajista para el dólar")
            if abs(v) > 0.05 else "")
    used_w = sum(w for _, _, w in comps) or 1
    score = sum(v * w for _, v, w in comps) / used_w * 100
    label = "ALZA" if score >= 25 else "BAJA" if score <= -25 else "ESTABLE"
    nz = [v for _, v, _ in comps if abs(v) > 0.05]
    agree = sum(1 for v in nz if (v > 0) == (score > 0)) / len(nz) if nz else 0
    conf = "alta" if (agree >= 0.8 and len(comps) >= 5 and abs(score) >= 40) else \
           "media" if (agree >= 0.6 and len(comps) >= 4) else "baja"
    if not ta.get("sufficient"):
        conf = "baja"
        reasons.append("Historial corto: RSI/MACD aún poco fiables (se afinan solos con el tiempo)")
    return {"label": label, "score": round(score, 1), "confidence": conf,
            "strong": abs(score) >= 50, "reasons": reasons}


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

    def fire(self, key, kind, text):
        cd = self.cfg["alerts"]["cooldown_min"].get(kind, 60) * 60
        t = self.now.timestamp()
        if t - self.last.get(key, 0) < cd:
            return False
        self.last[key] = t
        self.log.append({"t": iso(self.now), "kind": kind, "text": text})
        del self.log[:-60]
        self.send(text)
        return True


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
    for o in opps["arbitrage"]:
        if o["net_pct"] >= a["arb_net_pct"]:
            al.fire(f"arb:{o['buy_on']}", "arb",
                    f"💱 ARBITRAJE: compra en {o['buy_on'].title()} a {o['buy']:.2f} y vende en {o['sell_on'].title()} a {o['sell']:.2f}\n"
                    f"Neto {o['net_pct']:+.2f}% (≈ Bs {o['est_bob']:+,.0f} con tu capital; "
                    f"{o['net_after_transfer_pct']:+.2f}% si mueves USDT por la red).")
    for o in opps["taker"]:
        if o["net_pct"] >= a["taker_net_pct"]:
            al.fire(f"taker:{o['platform']}", "taker",
                    f"⚡ {o['platform'].title()}: compra {o['buy']:.2f} / vende {o['sell']:.2f} → neto {o['net_pct']:+.2f}% "
                    f"(≈ Bs {o['est_bob']:+,.0f}). Ejecuta rápido, estos cruces duran poco.")
    for o in opps["maker"]:
        if o["net_pct"] >= a["maker_net_pct"]:
            al.fire(f"maker:{o['platform']}", "maker",
                    f"🧩 SPREAD en {o['platform'].title()}: publica compra ≈ {o['buy_price']:.2f} y venta ≈ {o['sell_price']:.2f} "
                    f"→ neto {o['net_pct']:+.2f}% por ciclo (≈ Bs {o['est_bob']:+,.0f}).")
    if scen["strong"] and scen["label"] != prev_label:
        al.fire(f"signal:{scen['label']}", "signal",
                f"🧭 Escenario {scen['label']} (score {scen['score']:+.0f}, confianza {scen['confidence']}).\n- " +
                "\n- ".join(scen["reasons"][:4]))
    for s in build_signals(ta, scen, cfg):
        if s["type"] in ("compra", "venta"):
            al.fire(f"signal:{s['type']}", "signal", ("🟢 " if s["type"] == "compra" else "🔴 ") + s["text"])
    for n in news:
        if abs(n["score"]) >= 2:
            h = hashlib.md5(n["title"].encode()).hexdigest()[:10]
            if al.fire(f"news:{h}", "news",
                       f"📰 Noticia de impacto {n['direction'].upper()} para el dólar:\n{n['title']}\n{n['link']}"):
                break  # máximo una noticia por ejecución


# ───────────────────────── Telegram ─────────────────────────

class Telegram:
    def __init__(self, token, chat, dry):
        self.token, self.chat, self.dry = token, chat, dry

    @property
    def enabled(self):
        return bool(self.token and self.chat)

    def api(self, method, payload):
        return json.loads(http(f"https://api.telegram.org/bot{self.token}/{method}", payload, timeout=15, retries=1))

    def send(self, text):
        if self.dry or not self.enabled:
            print("[ALERTA]", text.replace("\n", " | "))
            return
        try:
            self.api("sendMessage", {"chat_id": self.chat, "text": text[:4000], "disable_web_page_preview": True})
        except Exception as e:  # noqa: BLE001
            print("Telegram error:", e, file=sys.stderr)

    def poll_commands(self, state, make_report):
        if not self.enabled or self.dry:
            return
        try:
            r = self.api("getUpdates", {"offset": state.get("tg_offset", 0), "timeout": 0,
                                        "allowed_updates": ["message"]})
        except Exception as e:  # noqa: BLE001
            print("Telegram getUpdates error:", e, file=sys.stderr)
            return
        for u in (r.get("result") or [])[-8:]:
            state["tg_offset"] = u["update_id"] + 1
            m = u.get("message") or {}
            if str((m.get("chat") or {}).get("id")) != str(self.chat):
                continue
            if time.time() - m.get("date", 0) > 1800:
                continue
            t = norm(m.get("text") or "")
            if t.startswith("/start") or t.startswith("/ayuda") or t.startswith("/help"):
                self.send("Comandos:\n/estado  (o escribe: análisis mercado P2P)\n/oportunidades\n/noticias")
            elif "analisis mercado p2p" in t or t.startswith("/estado") or t.startswith("/analisis"):
                self.send(make_report("full"))
            elif t.startswith("/oportunidades"):
                self.send(make_report("opps"))
            elif t.startswith("/noticias"):
                self.send(make_report("news"))
        if r.get("result"):
            state["tg_offset"] = r["result"][-1]["update_id"] + 1


def make_report(kind, latest):
    pl, sc, ta, op = latest["platforms"], latest["scenario"], latest["ta"], latest["opportunities"]
    t = fmt_local(parse_iso(latest["generated_at"]))
    lines = []
    if kind in ("full", "opps"):
        if kind == "full":
            lines.append(f"📊 P2P USDT/BOB · {t}")
            for n, p in pl.items():
                lines.append(f"{n.title()}: compra {p['best_buy']:.2f} · venta {p['best_sell']:.2f} · ref {p['ref']:.3f}")
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
        lines.append("📰 Noticias recientes:")
        for n in latest["news"][:6]:
            lines.append(f"  [{n['direction']}] {n['title']}")
    if kind == "full":
        lines.append("\nNo es asesoría financiera.")
    return "\n".join(lines)


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
    news = [
        {"title": "Demo: Gobierno anuncia desembolso de crédito externo para reforzar reservas", "link": "#",
         "source": "Demo", "time": iso(now - timedelta(hours=3)), "score": 2, "impact": "alto", "direction": "baja"},
        {"title": "Demo: Persisten bloqueos y escasez de dólares en el mercado", "link": "#",
         "source": "Demo", "time": iso(now - timedelta(hours=5)), "score": 3, "impact": "alto", "direction": "sube"},
        {"title": "Demo: Banco Central informa sobre tipo de cambio oficial", "link": "#",
         "source": "Demo", "time": iso(now - timedelta(hours=9)), "score": 0, "impact": "bajo", "direction": "neutral"},
    ]
    return pl, news, hist


# ───────────────────────── programa principal ─────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data", help="carpeta con el estado previo y salida")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    data = Path(args.data)
    data.mkdir(parents=True, exist_ok=True)
    now = now_utc().replace(microsecond=0)
    errors = []

    hist = read_json(data / "history.json", [])
    state = read_json(data / "state.json", {})
    tg = Telegram(os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID"), args.dry_run or args.demo)
    ws_token = os.environ.get("WAVESOFT_TOKEN")
    used_wavesoft = False

    if args.demo:
        platforms, news, demo_hist = demo_data(cfg)
        hist = demo_hist
    else:
        platforms = {}
        b = collect_binance(cfg, errors)
        if b is None and ws_token:
            b = wavesoft_latest(cfg, ws_token, errors)
            used_wavesoft = b is not None
        if b:
            platforms["binance"] = b
        y = collect_bybit(cfg, errors)
        if y:
            platforms["bybit"] = y
        news = fetch_news(cfg, errors)
        if ws_token and not state.get("seeded"):
            seed = wavesoft_seed(ws_token, errors)
            if seed:
                have = {r["t"][:13] for r in hist}
                hist = sorted(hist + [r for r in seed if r["t"][:13] not in have], key=lambda r: r["t"])
                state["seeded"] = True
                used_wavesoft = True

    if not platforms:
        latest = read_json(data / "latest.json", {})
        latest["status"] = {"ok": False, "errors": errors, "checked_at": iso(now)}
        write_json(data / "latest.json", latest)
        write_json(data / "state.json", state)
        print("Sin datos de ninguna plataforma:", errors, file=sys.stderr)
        return 0

    # registro del historial
    ref = (platforms.get("binance") or platforms.get("bybit"))["ref"]
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
    scen = build_scenario(ta, news, pressure)
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

    al = Alerter(cfg, state, tg.send, now)
    evaluate_alerts(al, cfg, ta, scen, opps, news, platforms, price, prev_label)
    state["scenario_label"] = scen["label"] if scen["strong"] else (prev_label or scen["label"])
    latest["alerts"] = list(reversed(state.get("log", [])))[:30]

    tg.poll_commands(state, lambda kind: make_report(kind, latest))

    # series ligeras para el gráfico del panel
    m5 = [r for r in hist if not r.get("seed")][-288:]
    chart = {"m5": m5, "h1": [{"t": t, "p": p} for t, p in hourly_series(hist)][-336:]}

    write_json(data / "latest.json", latest)
    write_json(data / "chart.json", chart)
    write_json(data / "history.json", hist)
    write_json(data / "state.json", state)
    print(f"OK {fmt_local(now)} ref={price:.3f} escenario={scen['label']}({scen['score']:+.0f}) "
          f"plataformas={list(platforms)} errores={len(errors)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
