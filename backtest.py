"""Backtest engine strategies on candle files with fees, leverage and liquidation-distance filter.

Usage: python3 backtest.py data/gemini_1h.json [lev] [strategy ...]
Entry = next bar open after signal; stop checked before take-profit inside a bar (conservative);
time stop after max_bars; one position at a time. PnL reported as % of margin.
"""
import json, sys, datetime
from engine import STRATEGIES, FEE_SIDE, prep, plan

def run(c, name, lev, p_over=None):
    fn, p = STRATEGIES[name]
    p = {**p, **(p_over or {})}
    ind = prep(c)
    trades, i = [], 0
    while i < len(c) - 2:
        sig = fn(c, ind, i, p)
        if not sig:
            i += 1; continue
        side, stop = sig
        entry = c[i + 1][1]
        pl = plan(side, entry, stop, p["rr"], lev)
        if not pl or (side == "long" and stop >= entry) or (side == "short" and stop <= entry):
            i += 1; continue
        tp = pl["tp"]
        exit_px, j = None, i + 1
        for j in range(i + 1, min(len(c), i + 1 + p["max_bars"])):
            _, o, h, l, cl, v = c[j]
            if side == "long":
                if l <= stop: exit_px = stop; break
                if h >= tp: exit_px = tp; break
            else:
                if h >= stop: exit_px = stop; break
                if l <= tp: exit_px = tp; break
        if exit_px is None:
            exit_px = c[j][4]
        mv = (exit_px / entry - 1) * (1 if side == "long" else -1)
        trades.append({"ts": c[i + 1][0], "side": side, "ret": lev * (mv - 2 * FEE_SIDE)})
        i = j + 1
    return trades

def stats(tr):
    if not tr:
        return "0 trades"
    eq, peak, dd = 1.0, 1.0, 0.0
    for t in tr:
        eq *= 1 + max(t["ret"], -1) * 0.5      # risk half the balance per trade (≈ how we sized: margin ≈ balance-1.2)
        peak = max(peak, eq); dd = max(dd, 1 - eq / peak)
    w = sum(t["ret"] > 0 for t in tr)
    avg = sum(t["ret"] for t in tr) / len(tr) * 100
    return f"{len(tr):3d} trades  win {w/len(tr)*100:4.0f}%  avg/trade {avg:+6.1f}% margin  equity x{eq:5.2f}  maxDD {dd*100:4.0f}%"

if __name__ == "__main__":
    c = json.load(open(sys.argv[1]))
    lev = int(sys.argv[2]) if len(sys.argv) > 2 else 40
    names = sys.argv[3:] or list(STRATEGIES)
    f = lambda t: datetime.datetime.utcfromtimestamp(t / 1000).strftime("%m-%d")
    print(f"{sys.argv[1]}  {len(c)} bars {f(c[0][0])}..{f(c[-1][0])}  lev {lev}x")
    for n in names:
        print(f"  {n:15s}", stats(run(c, n, lev)))
