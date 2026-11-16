"""Real-position monitor + trade guards. Pure logic, no network.

position.json (main branch, written by Claude when a real trade is opened/changed/closed):
  {"side":"long","entry":64200,"stop":63800,"tp":65000,"opened_utc":"2026-11-16T15:00:00+00:00"}
  or {} when flat.
trades.json (main branch, appended by Claude on close):
  [{"closed_utc":"...","pnl":-0.42}, ...]
"""
import datetime

FEES_BUFFER = 0.0025          # breakeven stop = entry ± 0.25% (covers 0.18% round-trip fees)
DAY_OFFSET = datetime.timedelta(hours=5)   # trading day in Yekaterinburg time
MAX_LOSSES_PER_DAY = 2
COOLDOWN_MIN = 30


def guards(trades, now):
    """Return a reason string if new entries are blocked, else None."""
    today = (now + DAY_OFFSET).date()
    day = [t for t in trades if (datetime.datetime.fromisoformat(t["closed_utc"]) + DAY_OFFSET).date() == today]
    losses = [t for t in day if t["pnl"] < 0]
    if len(losses) >= MAX_LOSSES_PER_DAY:
        return f"дневной лимит: {len(losses)} убыточные сделки сегодня"
    if trades and trades[-1]["pnl"] < 0:
        mins = (now - datetime.datetime.fromisoformat(trades[-1]["closed_utc"])).total_seconds() / 60
        if mins < COOLDOWN_MIN:
            return f"пауза после убытка ещё {int(COOLDOWN_MIN - mins)} мин"
    return None


def check_position(pos, s, now):
    """Return (status, action) for an open real position given the summary s."""
    px = s["px"]
    side, entry, stop, tp = pos["side"], pos["entry"], pos["stop"], pos["tp"]
    d = 1 if side == "long" else -1
    R = abs(entry - stop) if abs(entry - stop) > 0 else entry * 0.004
    if pos.get("orig_stop"):
        R = abs(entry - pos["orig_stop"])
    prog = (px - entry) * d / R
    a15 = s["tf"].get("15m", s["tf"]["5m"])["atr_pct"] / 100 * px
    h1 = s["tf"]["1h"]
    status = {"prog_R": round(prog, 2), "pnl_pct": round((px / entry - 1) * d * 100, 3)}

    # stop or target probably already executed on the exchange
    if (px - stop) * d <= 0 or (px - tp) * d >= 0:
        return status, {"do": "verify_closed", "why": "цена у стопа/тейка — проверить позицию"}

    be = entry * (1 + d * FEES_BUFFER)
    stop_at_be = (stop - be) * d >= 0
    # macro event soon: protect profit
    if s.get("no_trade_window") and prog > 0.3 and not stop_at_be:
        return status, {"do": "move_stop", "stop": round(be, 1), "why": "скоро данные США — стоп в безубыток"}
    # strong opposite bias while not in profit → cut early
    if s["bias"]["score"] * d <= -45 and prog < 0.2:
        return status, {"do": "close", "why": f"картина развернулась против позиции (bias {s['bias']['score']})"}
    # time stop: >12h without progress
    opened = datetime.datetime.fromisoformat(pos["opened_utc"])
    if (now - opened).total_seconds() > 12 * 3600 and prog < 0.3:
        return status, {"do": "close", "why": "12 ч без движения к цели"}
    # breakeven at +1R
    if prog >= 1.0 and not stop_at_be:
        return status, {"do": "move_stop", "stop": round(be, 1), "why": "+1R — стоп в безубыток"}
    # trail at ≥1.5R: 1×ATR(15m) behind price, only if it improves the stop by >0.1%
    if prog >= 1.5:
        trail = px - d * a15
        if (trail - stop) * d > entry * 0.001:
            return status, {"do": "move_stop", "stop": round(trail, 1), "why": "трейлинг 1×ATR15м"}
    # near target with momentum in our favour → extend target to next level
    if abs(tp - px) < 0.3 * a15 and h1["macd_h"] == ("up" if d == 1 else "down") and s["bias"]["score"] * d >= 30:
        lv = h1["res"] if d == 1 else h1["sup"]
        nxt = [x for x in lv if (x - tp) * d > a15]
        if nxt:
            return status, {"do": "move_tp", "tp": round(nxt[0] - d * 0.1 * a15, 1), "why": "импульс у цели — продлить тейк"}
    return status, None


def filter_signals(s, blocked):
    """Keep only signals that agree with the composite bias and pass the guards."""
    if blocked or s.get("no_trade_window"):
        return []
    out = []
    for g in s["signals"]:
        d = 1 if g["side"] == "long" else -1
        if s["bias"]["score"] * d < 0:
            continue                      # against the multi-timeframe picture
        if g["rr_net"] < 1.5:
            continue                      # not worth it after fees
        rsi = s["tf"]["1h"]["rsi"]
        if (d == 1 and rsi > 72) or (d == -1 and rsi < 28):
            continue                      # chasing an exhausted move
        out.append(g)
    return out


def brief(s, now):
    """One line Claude reads every check."""
    t = (now + DAY_OFFSET).strftime("%H:%M")
    h = s["tf"]["1h"]
    parts = [f"{t} BTC {s['px']:.0f}", f"bias {s['bias']['score']} {s['bias']['lean']}",
             f"1h {h['regime']} RSI{h['rsi']:.0f}"]
    a = s.get("action")
    if a:
        parts.append("ACTION " + " ".join(f"{k}={v}" for k, v in a.items()))
    elif s.get("position"):
        parts.append(f"pos {s['position']['side']} {s['pos_status']['prog_R']}R — без действий")
    elif s["signals"]:
        g = s["signals"][0]
        parts.append(f"SIGNAL {g['side']} entry {g['entry']} stop {g['stop']} tp {g['tp']} rr{g['rr_net']}")
    else:
        parts.append("нет сигнала" + (f" ({s['blocked']})" if s.get("blocked") else ""))
    if s.get("events"):
        parts.append("события: " + ", ".join(f"{e['name']} через {e['in_min']}м" for e in s["events"][:2]))
    return " | ".join(parts)
