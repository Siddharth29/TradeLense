"""LP-08 shadow replay — Phase 0.4/1 of the label-vs-tape fix plan (2026-07-25).

For every freeze-era app trade, compute the at-entry session-tape reading with
the SHIPPED math (packages.core.market_structure.compute_session_tape) and the
at-entry htf_trend label the system actually saw (parsed from the day logs),
then join with broker-true outcomes (MT5 deals) and conservative MFE/TAR bounds
from fully contained M5 candles and actual executions. Boundary-bar movement
outside the position's life must not be counted as its favorable excursion.

Output: the falsifiable table — CONFLICT vs ALIGNED vs NO-SIGNAL pools with
n / WR / avg net pnl / avg realized price-R / avg MFE_R / TAR rate.

Gate to proceed to Phase 2a (chat plan, 2026-07-25): conflicted pool n >= 25,
WR < ~35% and avg R <= -0.3 while aligned >= ~0. If the table doesn't show it,
the pattern stays observe-only.

Usage (MT5 terminal must be running; refuses non-Real servers):
    python scripts/research/label_tape_replay.py            # main table
    python scripts/research/label_tape_replay.py --sweep    # threshold grid
    python scripts/research/label_tape_replay.py --csv out.csv

Data sources (read-only):
  - results/logs/backtest_2026-07-*.log  trade_open events (entry/SL/TP) and
    the last-seen "htf_trend" per asset before each entry (prompt/gpt lines)
  - MT5 deal history                     position match -> net pnl, exit time
  - MT5 M5 rates                         session tape at entry, MFE, TAR
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from packages.core.market_structure import compute_session_tape  # noqa: E402

FREEZE_START = datetime(2026, 7, 13, tzinfo=timezone.utc)
ASSETS = ("XAUUSD", "XAGUSD", "EURUSD", "GBPUSD", "USTEC", "BTCUSD", "USOIL")
# The htf_trend value usually sits INSIDE a JSON-encoded msg string, so the
# quotes are escaped (\"htf_trend\":\"up\") — match both raw and escaped.
HTF_RE = re.compile(r'\\?"htf_trend\\?":\s*\\?"(up|down|mixed|range|na)\\?"')
ASSET_RE = re.compile(r"asset=([A-Z]+)")
TAPE_BARS = 150          # 5m bars fed to compute_session_tape (≈12.5h)
POST_WINDOW_H = 3        # MFE window cap when the trade has no exit (orphans)


def _iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


# ── 1. day logs → trade_opens + at-entry labels ───────────────────────────

def load_trades_from_logs() -> list[dict]:
    paths = sorted(glob.glob(os.path.join(REPO, "results", "logs", "backtest_2026-07-*.log")))
    paths = [p for p in paths if os.path.basename(p) >= "backtest_2026-07-13.log"]
    last_label: dict[str, tuple[datetime, str]] = {}
    trades: list[dict] = []
    for path in paths:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if "htf_trend" in line:
                    m = HTF_RE.search(line)
                    if m:
                        try:
                            j = json.loads(line)
                        except ValueError:
                            continue
                        ts = _iso(j.get("ts"))
                        ctx = j.get("ctx") or {}
                        pay = j.get("payload") or {}
                        a = (ctx.get("asset") or ctx.get("symbol")
                             or pay.get("asset") or pay.get("symbol"))
                        if not a:
                            am = ASSET_RE.search(j.get("msg", ""))
                            a = am.group(1) if am else None
                        if a:
                            last_label[a] = (ts, m.group(1))
                if '"event":"trade_open"' in line:
                    try:
                        j = json.loads(line)
                    except ValueError:
                        continue
                    p = j.get("payload") or {}
                    sym = p.get("symbol") or p.get("asset")
                    try:
                        entry = float(p["entry_price"] or p["price"])
                        sl = float(p["stop_loss"])
                        tp = float(p["take_profit"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    et = _iso(p.get("timestamp") or j.get("ts"))
                    lab_ts, lab = last_label.get(sym, (None, None))
                    trades.append({
                        "symbol": sym, "entry_time": et, "entry": entry,
                        "sl": sl, "tp": tp,
                        "side": "buy" if sl < entry else "sell",
                        "qty": float(p.get("quantity") or 0),
                        "label": lab,
                        "label_age_min": (round((et - lab_ts).total_seconds() / 60, 1)
                                          if lab_ts else None),
                    })
    return trades


# ── 2. MT5: deals (outcomes) + M5 rates (tape/MFE) ───────────────────────

def mt5_connect():
    import MetaTrader5 as mt5
    if not mt5.initialize():
        sys.exit(f"MT5 init failed: {mt5.last_error()}")
    acc = mt5.account_info()
    print(f"ACTIVE ACCOUNT: {acc.login} {acc.server} balance={acc.balance} {acc.currency}")
    if "Real" not in acc.server:
        mt5.shutdown()
        sys.exit("Refusing non-Real MT5 server (runbook rule).")
    return mt5


def load_deals(mt5) -> tuple[list, dict]:
    frm, to = FREEZE_START - timedelta(days=1), datetime.now(timezone.utc) + timedelta(days=1)
    deals = mt5.history_deals_get(frm, to) or []
    by_pos: dict[int, list] = {}
    for d in deals:
        by_pos.setdefault(d.position_id, []).append(d)
    ins = [d for d in deals if d.entry == 0 and d.magic == 234000]  # IN, app-owned
    return ins, by_pos


def load_rates(mt5) -> dict[str, list[dict]]:
    out = {}
    frm = FREEZE_START - timedelta(days=2)
    to = datetime.now(timezone.utc)
    for sym in ASSETS:
        mt5.symbol_select(sym, True)
        raw = mt5.copy_rates_range(sym, mt5.TIMEFRAME_M5, frm, to)
        if raw is None or len(raw) == 0:
            continue
        out[sym] = [{"time": int(r["time"]), "open": float(r["open"]),
                     "high": float(r["high"]), "low": float(r["low"]),
                     "close": float(r["close"])} for r in raw]
    return out


def match_outcome(tr: dict, ins: list, by_pos: dict) -> None:
    """Attach broker-true net pnl + exit info via the IN deal's position_id.

    A trade_open event with NO broker IN deal at its timestamp is not a real
    entry — the runner re-emits trade_open for the remaining half at each
    partial booking (monitor-tick timestamps ending :00, qty halved). The
    broker is authoritative (audit invariant #1): unmatched events are dropped
    from the pools by the caller.
    """
    best, best_dt = None, 181.0
    et = tr["entry_time"].timestamp()
    for d in ins:
        if d.symbol != tr["symbol"]:
            continue
        dt = abs(d.time - et)
        if dt < best_dt and abs(d.price - tr["entry"]) <= max(abs(tr["entry"]) * 0.005, 1e-9):
            best, best_dt = d, dt
    if best is None:
        tr["net"] = tr["exit_time"] = tr["exit_price"] = tr["pos_id"] = None
        return
    tr["pos_id"] = best.position_id
    legs = by_pos.get(best.position_id, [])
    outs = [d for d in legs if d.entry == 1]
    net = sum(d.profit + d.commission + d.swap for d in legs)
    tr["net"] = round(net, 2)
    tr["exit_time"] = (datetime.fromtimestamp(max(d.time for d in outs), tz=timezone.utc)
                       if outs else None)
    tr["exit_price"] = outs[-1].price if outs else None
    tr["observed_exit_prices"] = [d.price for d in outs]


def reconstruct(tr: dict, rates: dict) -> None:
    """Session tape at entry (shipped math) + MFE_R / realized R / TAR."""
    bars = rates.get(tr["symbol"]) or []
    et = tr["entry_time"].timestamp()
    # MT5 rate timestamps mark candle OPEN, not close. A 12:35:05 entry
    # cannot observe the final close/high/low of the 12:35--12:40 candle.
    pre = [b for b in bars if b["time"] + 300 <= et][-TAPE_BARS:]
    tr["tape"] = compute_session_tape(pre, tr["label"], tr["symbol"]) if pre else None
    dist = abs(tr["entry"] - tr["sl"])
    tp_r = abs(tr["tp"] - tr["entry"]) / dist if dist else None
    end = tr.get("exit_time") or (tr["entry_time"] + timedelta(hours=POST_WINDOW_H))
    # M5 OHLC cannot resolve the part of a candle before entry or after exit.
    # Use only fully contained bars and actual fill prices; report this as a
    # lower bound instead of attributing post-exit highs/lows to the position.
    win = [b for b in bars if et <= b["time"] and b["time"] + 300 <= end.timestamp()]
    observed = [tr["entry"]] + list(tr.get("observed_exit_prices") or [])
    if tr.get("exit_price") is not None:
        observed.append(tr["exit_price"])
    mfe_r = real_r = None
    if dist:
        if tr["side"] == "buy":
            mfe_r = (max(observed + [b["high"] for b in win]) - tr["entry"]) / dist
        else:
            mfe_r = (tr["entry"] - min(observed + [b["low"] for b in win])) / dist
    if dist and tr.get("exit_price"):
        sgn = 1.0 if tr["side"] == "buy" else -1.0
        real_r = sgn * (tr["exit_price"] - tr["entry"]) / dist
    tr["mfe_r"] = round(mfe_r, 2) if mfe_r is not None else None
    tr["mfe_basis"] = "lower_bound_closed_bars_and_executions"
    tr["real_r"] = round(real_r, 2) if real_r is not None else None
    tr["tar"] = round(mfe_r / tp_r, 2) if mfe_r is not None and tp_r else None


# ── 3. pools + table ──────────────────────────────────────────────────────

def classify(tr: dict, min_er: float, min_er2h: float) -> str:
    """Side-aware pools — LP-08 is about WHICH SIDE fires inside a conflict.

    LABEL-SIDE-CONFLICT  entry with the stale label, against the live tape —
                         the LP-08 death zone (07-23: 4 htf_up buys into an EU
                         downtrend) and the target of the Phase-2a hint.
    TAPE-SIDE-CONFLICT   entry against the label, with the tape — the judge
                         already overriding the stale label. If this pool WINS,
                         the hint must not forbid counter-label entries.
    WITH-BOTH / COUNTER-BOTH  label and tape agree; entry with / against both.
    NO-SIGNAL            tape absent, label non-directional, session flat, or
                         below the ER/persistence thresholds.
    """
    t = tr.get("tape")
    lab = tr.get("label")
    side_dir = "up" if tr["side"] == "buy" else "down"
    if not t or lab not in ("up", "down") or t["direction"] not in ("up", "down"):
        return "NO-SIGNAL"
    strong = (t["er_session"] is not None and t["er_session"] >= min_er
              and t["er_2h"] is not None and t["er_2h"] >= min_er2h)
    if not strong:
        return "NO-SIGNAL"
    if lab == t["direction"]:
        return "WITH-BOTH" if side_dir == lab else "COUNTER-BOTH"
    return "LABEL-SIDE-CONFLICT" if side_dir == lab else "TAPE-SIDE-CONFLICT"


def pool_stats(rows: list[dict]) -> dict:
    n = len(rows)
    scored = [r for r in rows if r.get("net") is not None]
    wins = sum(1 for r in scored if r["net"] > 0)
    rr = [r["real_r"] for r in rows if r.get("real_r") is not None]
    mf = [r["mfe_r"] for r in rows if r.get("mfe_r") is not None]
    tars = [r["tar"] for r in rows if r.get("tar") is not None]
    return {
        "n": n, "W": wins, "L": len(scored) - wins,
        "WR": round(100 * wins / len(scored), 1) if scored else None,
        "net$": round(sum(r["net"] for r in scored), 2) if scored else None,
        "avgR": round(sum(rr) / len(rr), 2) if rr else None,
        "avgMFE": round(sum(mf) / len(mf), 2) if mf else None,
        "TAR>=1": f"{sum(1 for t in tars if t >= 1.0)}/{len(tars)}" if tars else None,
    }


POOLS = ("LABEL-SIDE-CONFLICT", "TAPE-SIDE-CONFLICT", "WITH-BOTH", "COUNTER-BOTH", "NO-SIGNAL")


def print_table(trades: list[dict], min_er: float, min_er2h: float, header: str,
                detail: bool = True) -> None:
    pools: dict[str, list] = {p: [] for p in POOLS}
    for tr in trades:
        pools[classify(tr, min_er, min_er2h)].append(tr)
    print(f"\n=== {header} (er_session>={min_er}, er_2h>={min_er2h}) ===")
    print(f"{'pool':20} {'n':>4} {'W-L':>7} {'WR%':>6} {'net$':>9} {'avgR':>6} {'avgMFE':>7} {'TAR>=1':>8}")
    for name in POOLS:
        s = pool_stats(pools[name])
        print(f"{name:20} {s['n']:>4} {s['W']:>3}-{s['L']:<3} {str(s['WR']):>6} "
              f"{str(s['net$']):>9} {str(s['avgR']):>6} {str(s['avgMFE']):>7} {str(s['TAR>=1']):>8}")
    if not detail:
        return
    for name in ("LABEL-SIDE-CONFLICT", "TAPE-SIDE-CONFLICT"):
        rows = pools[name]
        if not rows:
            continue
        print(f"  {name} trades:")
        for r in sorted(rows, key=lambda x: x["entry_time"]):
            t = r["tape"]
            print(f"    {r['entry_time']:%m-%d %H:%M} {r['symbol']:7} {r['side']:4} "
                  f"label={r['label']:4} sess={t['session']:7} dir={t['direction']:4} "
                  f"erS={t['er_session']} er2h={t['er_2h']} move={t['move_pct']}% "
                  f"net={r['net']} R={r['real_r']} MFE={r['mfe_r']} TAR={r['tar']}")
    conf = pools["LABEL-SIDE-CONFLICT"]
    if conf:
        # pullback-vs-reversal proxy: split by |session move| median
        moves = sorted(abs(r["tape"]["move_pct"]) for r in conf)
        med = moves[len(moves) // 2]
        deep = [r for r in conf if abs(r["tape"]["move_pct"]) >= med]
        shal = [r for r in conf if abs(r["tape"]["move_pct"]) < med]
        print(f"  depth split (|move| median {med}%): "
              f"deep {pool_stats(deep)} | shallow {pool_stats(shal)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--csv")
    args = ap.parse_args()

    from packages.core.config import SESSION_TAPE_MIN_ER, SESSION_TAPE_MIN_ER_2H
    trades = load_trades_from_logs()
    print(f"trade_open events parsed: {len(trades)} "
          f"(labels missing: {sum(1 for t in trades if not t['label'])})")

    mt5 = mt5_connect()
    ins, by_pos = load_deals(mt5)
    rates = load_rates(mt5)
    mt5.shutdown()

    for tr in trades:
        match_outcome(tr, ins, by_pos)
    unmatched = [t for t in trades if t.get("pos_id") is None]
    if unmatched:
        print(f"dropped {len(unmatched)} trade_open events with no broker IN deal "
              f"(partial-booking re-registrations / rejects): "
              + ", ".join(f"{t['symbol']} {t['entry_time']:%m-%d %H:%M}" for t in unmatched))
    # broker-true trade list: one row per position (earliest event wins)
    seen: dict = {}
    for tr in sorted((t for t in trades if t.get("pos_id") is not None),
                     key=lambda x: x["entry_time"]):
        seen.setdefault(tr["pos_id"], tr)
    trades = list(seen.values())
    print(f"broker-matched positions: {len(trades)}")
    for tr in trades:
        reconstruct(tr, rates)

    ages = [t["label_age_min"] for t in trades if t["label_age_min"] is not None]
    if ages:
        print(f"label age at entry: median {sorted(ages)[len(ages)//2]} min, max {max(ages)} min")

    print_table(trades, SESSION_TAPE_MIN_ER, SESSION_TAPE_MIN_ER_2H, "MAIN TABLE — shipped thresholds")
    print("MFE/TAR are lower bounds from fully contained M5 bars and observed executions.")

    if args.sweep:
        for er in (0.15, 0.20, 0.25, 0.30, 0.35):
            for er2 in (0.10, 0.15, 0.20):
                print_table(trades, er, er2, "SWEEP", detail=False)
        # the calibration candidate in full detail
        print_table(trades, 0.20, 0.15, "SWEEP DETAIL — candidate", detail=True)

    if args.csv:
        cols = ["entry_time", "symbol", "side", "entry", "sl", "tp", "qty", "label",
                "label_age_min", "net", "real_r", "mfe_r", "tar", "mfe_basis"]
        tape_cols = ["session", "direction", "move_pct", "er_session", "er_2h",
                     "er_ratio_2h_1h", "bars", "conflict"]
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(cols + [f"tape_{c}" for c in tape_cols])
            for tr in sorted(trades, key=lambda x: x["entry_time"]):
                t = tr.get("tape") or {}
                w.writerow([tr.get(c) for c in cols] + [t.get(c) for c in tape_cols])
        print(f"CSV -> {args.csv}")


if __name__ == "__main__":
    main()
