"""Retroalimentación del monitor: registra predicciones y señales, mide después si se cumplieron
y arma el informe semanal (sábado). Solo biblioteca estándar.

Idea: cada hora se guarda qué predijo el sistema (escenario, puntaje de cada indicador, puntaje
sin noticias, noticias vigentes). Cada vez que salta una señal (zona de compra/venta, ruptura,
Bollinger) o aparece una noticia importante se guarda como "evento". A las 6 h y a las 24 h se
anota cuánto se movió realmente el precio. Con eso se calcula qué acertó, qué indicador ayudó o
perjudicó y si las noticias mejoraron o empeoraron el escenario.
"""
import bisect
from datetime import datetime, timedelta, timezone

LPZ = timezone(timedelta(hours=-4))
DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
T = "%Y-%m-%dT%H:%M:%SZ"

DEFAULTS = {
    "enabled": True,
    "horizon_h": 6,          # horizonte principal para medir aciertos
    "long_horizon_h": 24,    # horizonte largo
    "move_thr_pct": 0.20,    # movimiento mínimo (%) para considerar que "subió" o "bajó" a 6 h
    "long_thr_pct": 0.40,    # idem a 24 h
    "min_samples": 8,        # muestras mínimas para opinar sobre un indicador
    "weekday": 5,            # 5 = sábado
    "hour": 8,               # hora local de Bolivia del informe
    "keep_days": 45,
}

COMP_NAMES = {"rsi": "RSI", "macd": "MACD", "tendencia": "Tendencia (medias)", "momentum": "Momentum 3 h",
              "profundidad": "Libro de órdenes", "noticias": "Noticias"}
SIG_NAMES = {"zona_compra": "Zona de compra", "zona_venta": "Zona de venta",
             "ruptura_soporte": "Ruptura de soporte", "ruptura_resistencia": "Ruptura de resistencia",
             "bollinger_sup": "Bollinger banda superior", "bollinger_inf": "Bollinger banda inferior"}
# movimiento que se espera a favor de cada señal (+1 sube, -1 baja)
SIG_DIR = {"zona_compra": 1, "zona_venta": -1, "ruptura_soporte": -1, "ruptura_resistencia": 1,
           "bollinger_sup": -1, "bollinger_inf": 1}


def _p(s):
    return datetime.strptime(s, T).replace(tzinfo=timezone.utc)


def _i(dt):
    return dt.astimezone(timezone.utc).strftime(T)


def label_of(score):
    return "ALZA" if score >= 25 else "BAJA" if score <= -25 else "ESTABLE"


def hit(label, chg, thr):
    if label == "ALZA":
        return chg >= thr
    if label == "BAJA":
        return chg <= -thr
    return abs(chg) < thr


def _jac(a, b):
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if a | b else 0.0


# ───────────────────────── registro ─────────────────────────

def new_store():
    return {"preds": [], "events": [], "reports": [], "news_seen": []}


def log_pred(fb, now, price, scen, ta, news):
    """Una predicción por hora."""
    if fb["preds"] and fb["preds"][-1]["t"][:13] == _i(now)[:13]:
        return False
    fb["preds"].append({
        "t": _i(now), "p": round(price, 4), "lab": scen["label"], "sc": scen["score"],
        "nn": scen.get("score_nn"), "conf": scen.get("confidence"), "comps": scen.get("comps", {}),
        "rsi": None if ta.get("rsi") is None else round(ta["rsi"], 1),
        "news": [{"cat": n.get("cat"), "dir": n.get("direction"), "imp": bool(n.get("important"))}
                 for n in (news or [])[:6]],
    })
    return True


def log_event(fb, now, typ, price, **extra):
    ev = {"t": _i(now), "typ": typ, "p": round(price, 4)}
    ev.update(extra)
    fb["events"].append(ev)


def log_news_events(fb, now, price, news, max_age_h=6):
    """Cada noticia importante con dirección se registra una sola vez, al verla por primera vez."""
    seen = fb.setdefault("news_seen", [])
    for n in news or []:
        if not n.get("important") or n.get("direction") == "neutral":
            continue
        try:
            age = (now - _p(n["time"])).total_seconds() / 3600
        except Exception:  # noqa: BLE001
            continue
        if age > max_age_h:
            continue
        sig = n.get("sig") or [n.get("title", "")[:40]]
        if any(_jac(sig, s) >= 0.5 for s in seen):
            continue
        seen.append(sorted(sig))
        del seen[:-300]
        log_event(fb, now, "noticia", price, cat=n.get("cat_label") or n.get("cat") or "otras",
                  dir=n.get("direction"), src=n.get("source") or "")


# ───────────────────────── resultados reales ─────────────────────────

def _series(hist):
    pts = []
    for r in hist:
        p = r.get("br") or r.get("yr")
        if p and not r.get("seed"):
            try:
                pts.append((_p(r["t"]).timestamp(), p))
            except Exception:  # noqa: BLE001
                pass
    pts.sort()
    return [t for t, _ in pts], [p for _, p in pts]


def _price_at(ts, ps, target, tol=45 * 60):
    if not ts:
        return None
    k = bisect.bisect_left(ts, target)
    best = None
    for j in (k - 1, k):
        if 0 <= j < len(ts):
            d = abs(ts[j] - target)
            if d <= tol and (best is None or d < best[0]):
                best = (d, ps[j])
    return best[1] if best else None


def resolve(fb, hist, now, cfg):
    """Anota cuánto se movió el precio 6 h y 24 h después de cada predicción o evento."""
    ts, ps = _series(hist)
    if not ts:
        return
    nowt = now.timestamp()
    for h, key in ((cfg["horizon_h"], "o6"), (cfg["long_horizon_h"], "o24")):
        for rec in fb["preds"] + fb["events"]:
            if key in rec:
                continue
            target = _p(rec["t"]).timestamp() + h * 3600
            if target > nowt:
                continue
            px = _price_at(ts, ps, target)
            if px is None:
                if nowt > target + 3 * 3600:
                    rec[key] = "na"  # sin datos de ese momento: no se vuelve a intentar
                continue
            rec[key] = round((px / rec["p"] - 1) * 100, 3)


def prune(fb, now, days):
    cut = _i(now - timedelta(days=days))
    fb["preds"] = [r for r in fb["preds"] if r["t"] >= cut]
    fb["events"] = [r for r in fb["events"] if r["t"] >= cut]
    fb["reports"] = fb.get("reports", [])[-12:]


# ───────────────────────── evaluación ─────────────────────────

def _num(x):
    return isinstance(x, (int, float))


def evaluate(fb, now, days, cfg):
    since = _i(now - timedelta(days=days))
    thr, thr24 = cfg["move_thr_pct"], cfg["long_thr_pct"]
    preds = [r for r in fb["preds"] if r["t"] >= since and _num(r.get("o6"))]
    res = {"days": days, "n": len(preds), "labels": {}, "comps": {}, "news_effect": None, "news_cat": {},
           "signals": {}, "dir_n": 0, "dir_hits": 0, "flat_share": None, "long": {"n": 0, "hits": 0}}

    for lab in ("ALZA", "BAJA", "ESTABLE"):
        sub = [r for r in preds if r["lab"] == lab]
        res["labels"][lab] = {"n": len(sub), "hits": sum(1 for r in sub if hit(lab, r["o6"], thr))}
    res["dir_n"] = res["labels"]["ALZA"]["n"] + res["labels"]["BAJA"]["n"]
    res["dir_hits"] = res["labels"]["ALZA"]["hits"] + res["labels"]["BAJA"]["hits"]
    if preds:
        res["flat_share"] = sum(1 for r in preds if abs(r["o6"]) < thr) / len(preds)
    for r in fb["preds"]:
        if r["t"] >= since and _num(r.get("o24")):
            res["long"]["n"] += 1
            res["long"]["hits"] += hit(r["lab"], r["o24"], thr24)

    # cada indicador: ¿acertó la dirección cuando el precio realmente se movió?
    for name in COMP_NAMES:
        n = h = 0
        for r in preds:
            v = (r.get("comps") or {}).get(name)
            if v is None or abs(v) < 0.3 or abs(r["o6"]) < thr / 2:
                continue
            n += 1
            h += (v > 0) == (r["o6"] > 0)
        res["comps"][name] = {"n": n, "hits": h}

    # ¿las noticias mejoraron o empeoraron el escenario?
    both = [r for r in preds if _num(r.get("nn"))]
    if both:
        with_n = sum(hit(r["lab"], r["o6"], thr) for r in both)
        without = sum(hit(label_of(r["nn"]), r["o6"], thr) for r in both)
        flips = [r for r in both if label_of(r["nn"]) != r["lab"]]
        res["news_effect"] = {
            "n": len(both), "with": with_n, "without": without, "flips": len(flips),
            "news_right": sum(hit(r["lab"], r["o6"], thr) for r in flips),
            "tech_right": sum(hit(label_of(r["nn"]), r["o6"], thr) for r in flips)}

    evs = [e for e in fb["events"] if e["t"] >= since and _num(e.get("o6"))]
    for e in evs:
        if e["typ"] == "noticia":
            d = 1 if e.get("dir") == "sube" else -1
            c = res["news_cat"].setdefault(e.get("cat") or "otras", {"n": 0, "hits": 0, "sum": 0.0})
            c["n"] += 1
            c["sum"] += d * e["o6"]
            c["hits"] += (d * e["o6"]) >= thr / 2
        elif e["typ"] in SIG_DIR:
            d = SIG_DIR[e["typ"]]
            c = res["signals"].setdefault(e["typ"], {"n": 0, "hits": 0, "sum": 0.0})
            c["n"] += 1
            c["sum"] += d * e["o6"]
            c["hits"] += (d * e["o6"]) >= thr / 2
    return res


def _pct(h, n):
    return f"{100 * h / n:.0f}%" if n else "s/d"


def recommendations(week, month, cfg):
    ms = cfg["min_samples"]
    out = []
    if week["n"] < 30 and month["n"] < 60:
        out.append("Todavía hay pocos datos: toma estas conclusiones como orientativas. Con 3 o 4 semanas serán más sólidas.")
    base = month if month["n"] > week["n"] else week
    good, bad = [], []
    for name, c in base["comps"].items():
        if c["n"] >= ms:
            r = c["hits"] / c["n"]
            (good if r >= 0.60 else bad if r <= 0.45 else []).append(f"{COMP_NAMES[name]} ({_pct(c['hits'], c['n'])})")
    if bad:
        out.append("Perjudican: " + ", ".join(bad) + ". Podrías bajarles el peso en `scenario_weights` (config.json).")
    if good:
        out.append("Ayudan: " + ", ".join(good) + ". Mantén o sube su peso.")
    ne = base["news_effect"]
    if ne and ne["n"] >= ms * 2:
        w, wo = ne["with"] / ne["n"], ne["without"] / ne["n"]
        if wo - w >= 0.05:
            out.append(f"Las noticias EMPEORARON el escenario ({w * 100:.0f}% con noticias vs {wo * 100:.0f}% sin ellas). "
                       "Baja el peso de «noticias» o de las categorías que fallan.")
        elif w - wo >= 0.05:
            out.append(f"Las noticias MEJORARON el escenario ({w * 100:.0f}% vs {wo * 100:.0f}% sin ellas). Mantenlas.")
        else:
            out.append("Las noticias casi no cambian el acierto del escenario: no estorban, pero tampoco aportan mucho.")
        if ne["flips"] >= 4:
            out.append(f"Cuando las noticias contradijeron al análisis técnico ({ne['flips']} veces), "
                       + ("tuvo razón la noticia" if ne["news_right"] > ne["tech_right"] else
                          "tuvo razón el técnico" if ne["tech_right"] > ne["news_right"] else "hubo empate") + ".")
    for cat, c in base["news_cat"].items():
        if c["n"] >= 4:
            r = c["hits"] / c["n"]
            if r <= 0.40:
                out.append(f"Noticias de «{cat}»: solo {_pct(c['hits'], c['n'])} movió el dólar en la dirección esperada. "
                           f"Considera ponerles menos peso (news.cat_weight) o dejar de alertarlas.")
            elif r >= 0.65:
                out.append(f"Noticias de «{cat}»: {_pct(c['hits'], c['n'])} de acierto. Son confiables, dales prioridad.")
    for typ, c in base["signals"].items():
        if c["n"] >= ms // 2:
            r = c["hits"] / c["n"]
            if r <= 0.40:
                out.append(f"{SIG_NAMES[typ]}: acertó {_pct(c['hits'], c['n'])}. Conviene endurecer su umbral.")
            elif r >= 0.65:
                out.append(f"{SIG_NAMES[typ]}: acertó {_pct(c['hits'], c['n'])}. Es una señal útil en tu mercado.")
    if base["flat_share"] is not None and base["flat_share"] >= 0.7:
        out.append(f"El precio estuvo casi quieto ({base['flat_share'] * 100:.0f}% del tiempo se movió menos de "
                   f"{cfg['move_thr_pct']:g}% en 6 h): en esas semanas rinde más el spread que la tendencia.")
    return out


def report_text(fb, now, cfg, partial=False):
    week = evaluate(fb, now, 7, cfg)
    month = evaluate(fb, now, 28, cfg)
    loc = now.astimezone(LPZ)
    L = [f"📊 Informe de aciertos · {'parcial al ' if partial else 'semana al '}{DIAS[loc.weekday()] + loc.strftime(' %d/%m %H:%M')}",
         f"Predicciones medidas a {cfg['horizon_h']} h: {week['n']} esta semana · {month['n']} en 4 semanas"]
    if week["n"] == 0:
        L.append("\nAún no hay predicciones con resultado. El sistema empieza a medir desde que se activó esta función "
                 "y necesita al menos 6 horas.")
        return "\n".join(L)

    L += ["", f"🎯 Escenario (¿se movió como dijo?, a {cfg['horizon_h']} h)"]
    for lab in ("ALZA", "BAJA", "ESTABLE"):
        c = week["labels"][lab]
        if c["n"]:
            L.append(f"• Dijo {lab}: {c['n']} veces → acertó {c['hits']} ({_pct(c['hits'], c['n'])})")
    if week["dir_n"]:
        L.append(f"• Dirección (ALZA/BAJA): {_pct(week['dir_hits'], week['dir_n'])} · 4 semanas: "
                 f"{_pct(month['dir_hits'], month['dir_n'])}")
    if week["flat_share"] is not None:
        L.append(f"• Referencia: «siempre ESTABLE» habría acertado {week['flat_share'] * 100:.0f}%")
    if week["long"]["n"]:
        L.append(f"• A {cfg['long_horizon_h']} h: acertó {_pct(week['long']['hits'], week['long']['n'])} ({week['long']['n']} casos)")

    L += ["", "🧩 Qué indicador ayudó o perjudicó", "(acierta la dirección cuando el precio realmente se movió)"]
    rows = []
    for name, c in week["comps"].items():
        m = month["comps"][name]
        if c["n"] or m["n"]:
            icon = "➖" if c["n"] < cfg["min_samples"] else "✅" if c["hits"] / c["n"] >= 0.6 else "❌" if c["hits"] / c["n"] <= 0.45 else "🔸"
            rows.append(f"{icon} {COMP_NAMES[name]}: {_pct(c['hits'], c['n'])} (n={c['n']}) · 4 sem: {_pct(m['hits'], m['n'])} (n={m['n']})")
    L += rows or ["• sin datos suficientes"]

    ne = week["news_effect"]
    L += ["", "📰 Noticias"]
    if ne and ne["n"]:
        L.append(f"• Escenario con noticias: {_pct(ne['with'], ne['n'])} · sin noticias: {_pct(ne['without'], ne['n'])}")
        if ne["flips"]:
            L.append(f"• Las noticias cambiaron el escenario {ne['flips']} veces: acertó la noticia {ne['news_right']}, "
                     f"el técnico {ne['tech_right']}")
    if week["news_cat"]:
        for cat, c in sorted(week["news_cat"].items(), key=lambda kv: -kv[1]["n"])[:5]:
            L.append(f"• {cat}: {c['hits']}/{c['n']} movieron el dólar como se esperaba (prom. {c['sum'] / c['n']:+.2f}%)")
    elif not ne:
        L.append("• sin noticias importantes medidas esta semana")

    if week["signals"]:
        L += ["", "🚦 Señales (precio a 6 h, a favor de la señal)"]
        for typ, c in week["signals"].items():
            L.append(f"• {SIG_NAMES[typ]}: {c['hits']}/{c['n']} ({_pct(c['hits'], c['n'])}) · movimiento medio a favor {c['sum'] / c['n']:+.2f}%")

    recs = recommendations(week, month, cfg)
    if recs:
        L += ["", "💡 Qué sugiere el sistema"] + [f"• {r}" for r in recs]
    L += ["", "No se cambia nada solo: tú decides si ajustas pesos en config.json. No es asesoría financiera."]
    return "\n".join(L)[:3900]
