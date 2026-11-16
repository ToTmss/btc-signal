"""Runs in GitHub Actions: fetch BTC candles, build compact summary, keep a paper-trading log.

Outputs (committed to the repo):
  summary.json  – what the trading check reads (~1 KB)
  paper.json    – forward paper-trading of the engine's signals with fees (no real money)
"""
import json, os, time, urllib.request, datetime
from engine import summarize, bias, FEE_SIDE

UA = {"User-Agent": "btc-signal-bot/1.0"}
LEV = int(os.environ.get("LEV", "40"))
ENABLED = tuple(os.environ.get("STRATEGIES", "trend_pullback").split(","))
TF_SIGNAL = os.environ.get("TF_SIGNAL", "1h")       # signals are only taken from this timeframe


def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def gemini(tf):
    # Gemini v2 candles: newest first, [ts_ms, o, h, l, c, v]; drop the still-open bar
    d = sorted(get(f"https://api.gemini.com/v2/candles/btcusd/{tf}"))
    step = {"5m": 300, "15m": 900, "1hr": 3600}[tf] * 1000
    return [x for x in d if x[0] + step <= time.time() * 1000]


def derivatives():
    """Deribit BTC-PERPETUAL: funding (8h, %) and open interest; OI change vs previous run."""
    try:
        r = get("https://www.deribit.com/api/v2/public/ticker?instrument_name=BTC-PERPETUAL")["result"]
    except Exception:
        return None
    oi = r.get("open_interest")
    try:
        prev = json.load(open("state.json"))
    except Exception:
        prev = {}
    hist = [x for x in prev.get("oi_hist", []) if time.time() - x[0] < 4 * 3600] + [[time.time(), oi]]
    json.dump({"oi_hist": hist}, open("state.json", "w"))
    old = min(hist, key=lambda x: abs(time.time() - 3600 - x[0]))
    return {
        "funding_8h_pct": round(r.get("funding_8h", 0) * 100, 4),
        "oi_usd_m": round(oi / 1e6, 1) if oi else None,
        "oi_chg_1h_pct": round((oi / old[1] - 1) * 100, 2) if oi and old[1] and old[0] < time.time() - 1800 else None,
        "basis_pct": round((r["mark_price"] / r["index_price"] - 1) * 100, 3),
    }


def fear_greed():
    try:
        x = get("https://api.alternative.me/fng/?limit=1")["data"][0]
        return {"value": int(x["value"]), "label": x["value_classification"]}
    except Exception:
        return None


def upcoming_events(now):
    """Static macro calendar (macro_calendar.json, UTC ISO times) → events in the next 6 hours."""
    try:
        cal = json.load(open("macro_calendar.json"))
    except Exception:
        return []
    out = []
    for e in cal:
        t = datetime.datetime.fromisoformat(e["utc"].replace("Z", "+00:00"))
        mins = (t - now).total_seconds() / 60
        if -30 <= mins <= 360:
            out.append({"in_min": int(mins), "name": e["name"]})
    return out


def update_paper(summary, c1h):
    """Forward-test: open hypothetical trades on fresh signals, close them on stop/tp/time."""
    try:
        paper = json.load(open("paper.json"))
    except Exception:
        paper = {"open": None, "closed": [], "seen": []}
    pos = paper["open"]
    if pos:
        for ts, o, h, l, cl, v in c1h:
            if ts <= pos["bar_ts"]:
                continue
            hit = None
            if pos["side"] == "long":
                hit = pos["stop"] if l <= pos["stop"] else pos["tp"] if h >= pos["tp"] else None
            else:
                hit = pos["stop"] if h >= pos["stop"] else pos["tp"] if l <= pos["tp"] else None
            pos["bars"] = pos.get("bars", 0) + 1
            if hit or pos["bars"] >= 48:
                px = hit or cl
                mv = (px / pos["entry"] - 1) * (1 if pos["side"] == "long" else -1)
                pos.update(exit=px, exit_ts=ts, ret_margin_pct=round(LEV * (mv - 2 * FEE_SIDE) * 100, 1))
                paper["closed"].append(pos); paper["open"] = pos = None
                break
            pos["bar_ts"] = ts
    if not paper["open"]:
        for s in summary["signals"]:
            key = f'{s["tf"]}-{s["strategy"]}-{s["bar_ts"]}'
            if s["tf"] == TF_SIGNAL and key not in paper["seen"]:
                paper["open"] = {**s, "opened": datetime.datetime.utcnow().isoformat(timespec="minutes")}
                paper["seen"] = (paper["seen"] + [key])[-200:]
                break
    cl = paper["closed"]
    paper["stats"] = {"trades": len(cl), "wins": sum(t["ret_margin_pct"] > 0 for t in cl),
                      "sum_ret_margin_pct": round(sum(t["ret_margin_pct"] for t in cl), 1)}
    json.dump(paper, open("paper.json", "w"), indent=1)
    return paper["stats"]


def main():
    now = datetime.datetime.now(datetime.timezone.utc)
    tfs = {"5m": gemini("5m"), "15m": gemini("15m"), "1h": gemini("1hr")}
    s = summarize(tfs, lev=LEV, enabled=ENABLED)
    s["signals"] = [x for x in s["signals"] if x["tf"] == TF_SIGNAL]
    s["generated_utc"] = now.isoformat(timespec="seconds")
    s["deriv"] = derivatives()
    s["bias"] = bias(s, s["deriv"])
    s["fear_greed"] = fear_greed()
    s["events"] = upcoming_events(now)
    s["no_trade_window"] = any(-15 <= e["in_min"] <= 30 for e in s["events"])
    s["paper"] = update_paper(s, tfs["1h"])
    s["params"] = {"lev": LEV, "strategies": ENABLED, "tf": TF_SIGNAL}
    json.dump(s, open("summary.json", "w"), separators=(",", ":"))
    print(json.dumps(s, ensure_ascii=False)[:1500])


if __name__ == "__main__":
    main()
