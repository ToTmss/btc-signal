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


def prep(c):
    cl = [x[4] for x in c]
    return {
        "close": cl,
        "ema20": ema(cl, 20), "ema50": ema(cl, 50), "ema200": ema(cl, 200),
        "atr": atr(c), "vol_avg": sma([x[5] for x in c], 20),
    }


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
    "breakout": (s_breakout, {"n": 24, "volx": 1.5, "stop_atr": 1.2, "rr": 2.0, "max_bars": 48}),
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
        out["tf"][tf] = {
            "regime": regime(ind, i),
            "atr_pct": round(ind["atr"][i] / c[i][4] * 100, 3),
            "vol_x": round(c[i][5] / ind["vol_avg"][i - 1], 2) if ind["vol_avg"][i - 1] else None,
            "chg_pct_24": round((c[i][4] / c[max(0, i - 24)][4] - 1) * 100, 2),
            "sup": sup, "res": res,
        }
        for name in enabled:
            fn, p = STRATEGIES[name]
            sig = fn(c, ind, i, p)
            if sig:
                pl = plan(sig[0], c[i][4], sig[1], p["rr"], lev)
                if pl:
                    out["signals"].append({"tf": tf, "strategy": name, "bar_ts": c[i][0], **pl})
    if events:
        out["events"] = events
    return out
