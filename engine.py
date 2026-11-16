"""BTC signal engine: indicators, levels, strategies, compact summary.

Candles: list of [ts_ms, open, high, low, close, volume], oldest first, closed bars only.
Pure standard library so it runs anywhere (GitHub Actions, local, sandbox).
"""
from __future__ import annotations

FEE_SIDE = 0.0009          # Telegram Wallet perps taker fee per side (0.09% of notional)
LIQ_FACTOR = 0.52          # observed: liquidation distance ≈ 0.52 / leverage (40x → ~1.3%)


# ---------- indicators ----------
def ema(vals, n):
    k, out, e = 2 / (n + 1), [], None
    for v in vals:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def atr(c, n=14):
    trs, out, a = [], [], None
    for i, (_, o, h, l, cl, v) in enumerate(c):
        pc = c[i - 1][4] if i else cl
        tr = max(h - l, abs(h - pc), abs(l - pc))
        a = tr if a is None else (a * (n - 1) + tr) / n
        out.append(a)
    return out


def sma(vals, n):
    out, s = [], 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        out.append(s / min(i + 1, n))
    return out


def rsi(vals, n=14):
    out, ag, al = [], None, None
    for i, v in enumerate(vals):
        if i == 0:
            out.append(50.0); continue
        d = v - vals[i - 1]
        g, l = max(d, 0), max(-d, 0)
        ag = g if ag is None else (ag * (n - 1) + g) / n
        al = l if al is None else (al * (n - 1) + l) / n
        out.append(100.0 if al == 0 else 100 - 100 / (1 + ag / al))
    return out


def macd_hist(vals, f=12, s=26, sig=9):
    line = [a - b for a, b in zip(ema(vals, f), ema(vals, s))]
    sl = ema(line, sig)
    return [a - b for a, b in zip(line, sl)]


def bb_width(vals, n=20, k=2.0):
    """Bollinger band width in % of the middle band (squeeze = low values)."""
    out = []
    for i in range(len(vals)):
        w = vals[max(0, i - n + 1):i + 1]
        m = sum(w) / len(w)
        sd = (sum((x - m) ** 2 for x in w) / len(w)) ** 0.5
        out.append(2 * k * sd / m * 100)
    return out


def vwap_day(c):
    """VWAP since 00:00 UTC of the last bar's day."""
    day = c[-1][0] // 86_400_000
    pv = vv = 0.0
    for ts, o, h, l, cl, v in c:
        if ts // 86_400_000 == day:
            pv += (h + l + cl) / 3 * v; vv += v
    return pv / vv if vv else c[-1][4]


def prep(c):
    cl = [x[4] for x in c]
    return {
        "close": cl,
        "ema20": ema(cl, 20), "ema50": ema(cl, 50), "ema200": ema(cl, 200),
        "atr": atr(c), "vol_avg": sma([x[5] for x in c], 20),
        "rsi": rsi(cl), "macd_h": macd_hist(cl), "bbw": bb_width(cl),
    }


def structure(c, look=3, last=120):
    """Market structure from the last two swing highs and lows: HH/HL, LH/LL or mixed."""
    seg = c[-last:]
    hs, ls = [], []
    for i in range(look, len(seg) - look):
        win = seg[i - look:i + look + 1]
        if seg[i][2] == max(x[2] for x in win): hs.append(seg[i][2])
        if seg[i][3] == min(x[3] for x in win): ls.append(seg[i][3])
    if len(hs) < 2 or len(ls) < 2:
        return "n/a"
    up_h, up_l = hs[-1] > hs[-2], ls[-1] > ls[-2]
    return "HH/HL" if up_h and up_l else "LH/LL" if not up_h and not up_l else "mixed"


# ---------- strategies: each returns (side, stop) for bar i (signal on close of i) ----------
def s_trend_pullback(c, ind, i, p):
    """Trade with EMA50/200 trend after a pullback that tags EMA20 and closes back beyond it."""
    if i < 200:
        return None
    _, o, h, l, cl, v = c[i]
    e20, e50, e200, a = ind["ema20"][i], ind["ema50"][i], ind["ema200"][i], ind["atr"][i]
    if e50 > e200 and cl > e50 and l <= e20 < cl and cl > o:
        return "long", min(x[3] for x in c[i - 2:i + 1]) - p["buf"] * a
    if e50 < e200 and cl < e50 and h >= e20 > cl and cl < o:
        return "short", max(x[2] for x in c[i - 2:i + 1]) + p["buf"] * a
    return None


def s_breakout(c, ind, i, p):
    """Close beyond the N-bar range on above-average volume, in the EMA200 direction."""
    n = p["n"]
    if i < max(n, 200):
        return None
    hh = max(x[2] for x in c[i - n:i]); ll = min(x[3] for x in c[i - n:i])
    _, o, h, l, cl, v = c[i]
    a = ind["atr"][i]
    if v < p["volx"] * ind["vol_avg"][i - 1]:
        return None
    if cl > hh and cl > ind["ema200"][i]:
        return "long", cl - p["stop_atr"] * a
    if cl < ll and cl < ind["ema200"][i]:
        return "short", cl + p["stop_atr"] * a
    return None


def s_false_break(c, ind, i, p):
    """Sweep of the N-bar low/high that closes back inside the range (spring / upthrust)."""
    n = p["n"]
    if i < n + 1:
        return None
    ll = min(x[3] for x in c[i - n:i]); hh = max(x[2] for x in c[i - n:i])
    _, o, h, l, cl, v = c[i]
    a = ind["atr"][i]
    if v < p["volx"] * ind["vol_avg"][i - 1]:
        return None
    if l < ll and cl > ll and cl > o:
        return "long", l - p["buf"] * a
    if h > hh and cl < hh and cl < o:
        return "short", h + p["buf"] * a
    return None


STRATEGIES = {
    "trend_pullback": (s_trend_pullback, {"buf": 0.2, "rr": 2.0, "max_bars": 48}),
    "breakout": (s_breakout, {"n": 24, "volx": 1.5, "stop_atr": 1.2, "rr": 3.0, "max_bars": 48}),
    "false_break": (s_false_break, {"n": 24, "volx": 1.2, "buf": 0.1, "rr": 2.0, "max_bars": 36}),
}


def plan(side, entry, stop, rr, lev):
    """Turn a raw signal into an order plan; None if the stop sits too close to liquidation."""
    risk = abs(entry - stop) / entry
    if risk < 0.0015 or risk > 0.85 * LIQ_FACTOR / lev:
        return None
    tp = entry * (1 + rr * risk) if side == "long" else entry * (1 - rr * risk)
    net_win = lev * (rr * risk - 2 * FEE_SIDE)
    net_loss = lev * (risk + 2 * FEE_SIDE)
    return {"side": side, "entry": round(entry, 1), "stop": round(stop, 1), "tp": round(tp, 1),
            "risk_pct": round(risk * 100, 3), "rr_net": round(net_win / net_loss, 2)}


# ---------- levels & regime for the summary ----------
def swing_levels(c, look=3, last=150, k=4):
    lows, highs = [], []
    seg = c[-last:]
    for i in range(look, len(seg) - look):
        if seg[i][3] == min(x[3] for x in seg[i - look:i + look + 1]):
            lows.append(seg[i][3])
        if seg[i][2] == max(x[2] for x in seg[i - look:i + look + 1]):
            highs.append(seg[i][2])
    px = c[-1][4]
    sup = sorted({round(x, -1) for x in lows if x < px}, reverse=True)[:k]
    res = sorted({round(x, -1) for x in highs if x > px})[:k]
    return sup, res


def regime(ind, i):
    e20, e50, e200, cl = (ind[k][i] for k in ("ema20", "ema50", "ema200", "close"))
    if e20 > e50 > e200 and cl > e20:
        return "up"
    if e20 < e50 < e200 and cl < e20:
        return "down"
    return "range"


def summarize(tf_candles: dict, lev=40, enabled=("trend_pullback", "breakout", "false_break"), events=None):
    """tf_candles: {"5m": [...], "15m": [...], "1h": [...]} closed bars. Returns compact dict."""
    out = {"px": tf_candles["5m"][-1][4] if "5m" in tf_candles else None, "tf": {}, "signals": []}
    for tf, c in tf_candles.items():
        if len(c) < 210:
            continue
        ind = prep(c)
        i = len(c) - 1
        sup, res = swing_levels(c)
        px, a = c[i][4], ind["atr"][i]
        bbw_last = ind["bbw"][-100:]
        out["tf"][tf] = {
            "regime": regime(ind, i),
            "structure": structure(c),
            "rsi": round(ind["rsi"][i], 1),
            "macd_h": "up" if ind["macd_h"][i] > ind["macd_h"][i - 1] else "down",
            "macd_sign": "+" if ind["macd_h"][i] > 0 else "-",
            "bbw_pctile": round(sum(x < ind["bbw"][i] for x in bbw_last) / len(bbw_last) * 100),
            "vs_ema200_pct": round((px / ind["ema200"][i] - 1) * 100, 2),
            "atr_pct": round(a / px * 100, 3),
            "vol_x": round(c[i][5] / ind["vol_avg"][i - 1], 2) if ind["vol_avg"][i - 1] else None,
            "chg_pct_24": round((px / c[max(0, i - 24)][4] - 1) * 100, 2),
            "sup": sup, "res": res,
            "to_sup_atr": round((px - sup[0]) / a, 1) if sup else None,
            "to_res_atr": round((res[0] - px) / a, 1) if res else None,
        }
        if tf == "5m":
            out["vwap_day"] = round(vwap_day(c), 1)
        for name in enabled:
            fn, p = STRATEGIES[name]
            sig = fn(c, ind, i, p)
            if sig:
                pl = plan(sig[0], c[i][4], sig[1], p["rr"], lev)
                if pl:
                    out["signals"].append({"tf": tf, "strategy": name, "bar_ts": c[i][0], **pl})
    out["bias"] = bias(out)
    if events:
        out["events"] = events
    return out


def bias(s, deriv=None):
    """Composite directional score −100…+100 with the reasons behind it."""
    score, why = 0, []
    w = {"1h": 2.0, "15m": 1.0, "5m": 0.5}
    for tf, k in w.items():
        t = s["tf"].get(tf)
        if not t:
            continue
        r = {"up": 1, "down": -1}.get(t["regime"], 0) + {"HH/HL": 1, "LH/LL": -1}.get(t["structure"], 0)
        r += 0.5 if t["macd_sign"] == "+" else -0.5
        score += k * r * 10
        if r:
            why.append(f"{tf}:{t['regime']}/{t['structure']}")
    h = s["tf"].get("1h")
    if h:
        if h["rsi"] > 70: score -= 10; why.append("1h RSI перекуплен")
        if h["rsi"] < 30: score += 10; why.append("1h RSI перепродан")
    if deriv and deriv.get("funding_8h_pct") is not None:
        f = deriv["funding_8h_pct"]
        if f > 0.03: score -= 10; why.append("фандинг высокий (лонги перегреты)")
        if f < -0.01: score += 10; why.append("фандинг отрицательный (шорты перегреты)")
    score = max(-100, min(100, round(score)))
    return {"score": score, "lean": "long" if score >= 30 else "short" if score <= -30 else "neutral", "why": why[:5]}
