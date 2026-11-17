"""Forward paper tournament: many strategy books trade side by side on live candles (no money).

Each book = strategy × timeframe × filter. A book opens at the close of a signal bar, then is
closed by stop / take-profit / time limit on later closed bars. Fees 0.09%/side, leverage LEV.
State in tournament.json (data branch). Results summarised in summary["tournament"].
"""
import json, datetime
from engine import STRATEGIES, FEE_SIDE, prep, plan

TFS = ("5m", "15m")
FILTERS = ("raw", "bias")          # bias = only trades that agree with the composite score


# Pilot 2 (from 2026-11-17 10:15 Yekaterinburg): only the books that were not losing overnight.
ACTIVE = ["15m:momentum_burst", "15m:trend_pullback", "15m:breakout", "15m:false_break",
          "15m:rsi_revert", "5m:rsi_revert"]


def books():
    return [f"{x}:{flt}" for x in ACTIVE for flt in FILTERS]


def _close(pos, c, lev):
    """Walk bars after pos['bar_ts']; return closed trade dict or None (and update progress)."""
    for ts, o, h, l, cl, v in c:
        if ts <= pos["last_ts"]:
            continue
        pos["last_ts"] = ts
        pos["bars"] += 1
        long_ = pos["side"] == "long"
        px = None
        if (l <= pos["stop"]) if long_ else (h >= pos["stop"]):
            px, why = pos["stop"], "stop"
        elif (h >= pos["tp"]) if long_ else (l <= pos["tp"]):
            px, why = pos["tp"], "tp"
        elif pos["bars"] >= pos["max_bars"]:
            px, why = cl, "time"
        if px is not None:
            mv = (px / pos["entry"] - 1) * (1 if long_ else -1)
            return {**pos, "exit": px, "exit_ts": ts, "why": why,
                    "ret": round(lev * (mv - 2 * FEE_SIDE) * 100, 1)}
    return None


def update(tfs, bias_score, lev, path="tournament.json"):
    try:
        st = json.load(open(path))
    except Exception:
        st = {"started_utc": datetime.datetime.utcnow().isoformat(timespec="minutes"), "books": {}}
    preps = {tf: prep(tfs[tf]) for tf in TFS if tf in tfs}
    for b in books():
        tf, name, flt = b.split(":")
        if tf not in preps:
            continue
        c, ind = tfs[tf], preps[tf]
        bk = st["books"].setdefault(b, {"open": None, "closed": [], "last_sig_ts": 0})
        if bk["open"]:
            done = _close(bk["open"], c, lev)
            if done:
                bk["closed"].append({k: done[k] for k in ("side", "entry", "exit", "ret", "why", "entry_ts", "exit_ts")})
                bk["open"] = None
        if bk["open"]:
            continue
        i = len(c) - 1
        if c[i][0] <= bk["last_sig_ts"]:
            continue
        bk["last_sig_ts"] = c[i][0]
        fn, p = STRATEGIES[name]
        sig = fn(c, ind, i, p)
        if not sig:
            continue
        side, stop = sig
        if flt == "bias" and bias_score * (1 if side == "long" else -1) < 0:
            continue
        pl = plan(side, c[i][4], stop, p["rr"], lev)
        if not pl:
            continue
        bk["open"] = {"side": side, "entry": pl["entry"], "stop": pl["stop"], "tp": pl["tp"],
                      "entry_ts": c[i][0], "last_ts": c[i][0], "bars": 0, "max_bars": p["max_bars"]}
    json.dump(st, open(path, "w"), separators=(",", ":"))
    return table(st)


def table(st):
    rows = []
    for b, bk in st["books"].items():
        cl = bk["closed"]
        n = len(cl)
        rows.append({"book": b, "n": n, "win": round(sum(t["ret"] > 0 for t in cl) / n * 100) if n else None,
                     "sum": round(sum(t["ret"] for t in cl), 1), "open": bool(bk["open"])})
    rows.sort(key=lambda r: r["sum"], reverse=True)
    return {"started_utc": st["started_utc"], "top": rows[:5],
            "total_trades": sum(r["n"] for r in rows)}
