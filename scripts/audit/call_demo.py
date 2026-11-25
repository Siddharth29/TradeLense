"""Three read-only demos for a screen share. No orders, no broker, no model calls.

    python scripts/audit/call_demo.py funnel    --date 2026-07-23
    python scripts/audit/call_demo.py trace     --date 2026-07-16
    python scripts/audit/call_demo.py reconcile --date 2026-07-16
    python scripts/audit/call_demo.py census

funnel     - the decision funnel for one recorded trading day, read from that
             day's own log: cycles run, model calls, proposals declined,
             strategies armed, trades placed. Shows the "no" as clearly as the "yes".
trace      - one completed trade end to end: analysis call, review call, order,
             trail ladder, exit, the broker's own deals for it, audit entry.
reconcile  - one trading day against the stored broker deal export, split by the
             broker's ownership tag, plus the balance line from the equity samples.
census     - the whole 13-27 July window from the stored broker deal export.

Everything here reads local files and the local decision store. It opens no
broker connection, places no orders, and spends nothing.
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta

if __package__:
    from .trade_log_events import count_trade_stages
else:
    from trade_log_events import count_trade_stages

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BAR = "=" * 78


def _hdr(t: str) -> None:
    print("\n" + BAR + "\n" + t + "\n" + BAR)


# ------------------------------------------------------------------ funnel
FUNNEL = [
    ("Woke on a closed bar", r"woke for bar close"),
    ("Forming bar stripped before analysis", r"Stripping unclosed last"),
    ("Analysis cycles run", r'"event":"cycle"'),
    ("Model calls made", r'"event":"gpt_response"'),
    ("DECLINED - no valid proposal in the cycle", r"no valid proposals generated"),
    ("DECLINED - every proposal came back flat", r"proposals are flat"),
    ("Proposals that reached the reviewer", r"Judge processing"),
    ("Strategies armed and watched", r"Code sniper strategy added"),
    ("SKIPPED - a strategy was still live", r"still monitoring"),
    ("Stop floor raised by session noise", r"SL floor raised by session noise"),
    ("FLAGGED - min-lot forced more risk than intended", r"Min-lot RISK OVERSHOOT"),
    ("EXPIRED unfired - armed, watched, never triggered", r'"event":"strategy_expiry"'),
]


def funnel(date: str) -> None:
    path = os.path.join(REPO, "results", "logs", "backtest_" + date + ".log")
    if not os.path.exists(path):
        sys.exit("no day log for " + date)
    lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    _hdr("DECISION FUNNEL  -  " + date + "  -  read from that day's own log")
    for label, pat in FUNNEL:
        rx = re.compile(pat)
        print("  {:>6}   {}".format(sum(1 for ln in lines if rx.search(ln)), label))
    stages = count_trade_stages(lines)
    for stage, label in (("fill", "Position opens"), ("partial", "Partial exits"), ("exit", "Final exit records")):
        print("  {:>6}   {}".format(stages[stage], label))
    print("")
    print("  One count per log line, over the whole file. The published day audit")
    print("  scopes its counts to the audit window, so its numbers are smaller.")


# ------------------------------------------------------------------ trace
def trace(date: str, want_pnl=None) -> None:
    import pymongo

    db = pymongo.MongoClient("mongodb://localhost:27017", serverSelectionTimeoutMS=3000)["nifty_backtest"]
    d0 = datetime.fromisoformat(date)
    q = {"timestamp": {"$gte": d0, "$lt": d0 + timedelta(days=1)}, "trade_status": "closed"}
    trades = sorted(db["comprehensive_trades"].find(q), key=lambda t: (t.get("pnl") or 0), reverse=True)
    if not trades:
        sys.exit("no closed trades on " + date)
    if want_pnl is None:
        t = trades[0]
    else:
        t = next(x for x in trades if abs((x.get("pnl") or 0) - want_pnl) < 0.01)

    _hdr("ONE TRADE, END TO END  -  " + t["asset"] + " " + t["side"] + "  -  " + date)
    entry = t["timestamp"]

    calls = sorted(
        db["gpt_responses"].find(
            {"symbol": t["asset"], "timestamp": {"$gte": entry - timedelta(minutes=30), "$lte": entry}}
        ),
        key=lambda g: g["timestamp"],
    )
    print("")
    print("1. ANALYSIS AND REVIEW")
    for g in calls:
        role = "reviewer" if "judge" in (g.get("gpt_mode") or "") else "analyst"
        conf = "  confidence " + str(g["confidence"]) if g.get("confidence") is not None else ""
        secs = round((g.get("latency_ms") or 0) / 1000.0, 1)
        print("   {}  {:<9} {}  {} tokens  {}s{}".format(
            str(g["timestamp"])[11:19], role, g.get("model"), g.get("tokens_used"), secs, conf))
    print("")
    print("   Setup as the reviewer described it:")
    print("   " + (t.get("pattern") or "")[:300])

    risk = abs(float(t["entry_price"]) - float(t["stop_loss"]))
    print("")
    print("2. THE ORDER")
    print("   armed, then triggered at " + str(entry)[11:19])
    print("   entry {}   stop {}   target {}".format(t["entry_price"], t["stop_loss"], t.get("take_profit")))
    print("   size {} lots   risk per unit {}   broker ticket {}".format(
        t["quantity"], round(risk, 5), t.get("position_ticket")))

    print("")
    print("3. MANAGEMENT  (deterministic - there is no model call in this loop)")
    for s in (t.get("sl_trail") or []):
        print("   {}  stop -> {:<12} {}".format(str(s["at"])[11:19], s["sl"], s["label"]))

    print("")
    print("4. EXIT")
    print("   {}  at {}  reason {}  result ${}".format(
        str(t.get("trade_closed_at"))[11:19], t["exit_price"], t.get("exit_reason"), t["pnl"]))
    sign = 1 if t["side"] == "buy" else -1
    r = sign * (float(t["exit_price"]) - float(t["entry_price"])) / risk
    print("   realised {}R against the stop it was given".format(round(r, 2)))

    print("")
    print("5. THE BROKER'S OWN RECORD")
    _deals_for(t.get("position_ticket"))

    print("")
    print("6. THE AUDIT ENTRY")
    print("   written up in the daily report for " + date + ", and carried as a row in the equity ledger")


def _deals_for(ticket) -> None:
    for f in glob.glob(os.path.join(REPO, "data", "audit", "*", "deals.csv")):
        rows = [r for r in csv.DictReader(open(f)) if r["position_id"] == str(ticket)]
        if rows:
            print("   {} deals for this position in the stored broker export".format(len(rows)))
            for r in rows:
                print("   {}  {:<8} vol {:<6} price {:<12} profit {:<8} commission {:<8} tag {}".format(
                    r["time"][11:19], r["symbol"], r["volume"], r["price"],
                    r["profit"], r["commission"], r["magic"]))
            return
    print("   this position is not inside a stored broker export - re-pull to show it")


# ------------------------------------------------------------------ reconcile
def reconcile(date: str) -> None:
    _hdr("DAY RECONCILIATION  -  " + date + "  -  broker deal export vs the balance line")

    best = None
    for f in glob.glob(os.path.join(REPO, "data", "audit", "*", "deals.csv")):
        rows = [r for r in csv.DictReader(open(f)) if r["time"][:10] == date]
        if rows and (best is None or len(rows) > len(best[1])):
            best = (f, rows)
    if best is None:
        sys.exit("no stored broker export covering " + date)
    src, rows = best
    print("")
    print("Source: broker deal export, {}  -  {} deals on this date".format(
        os.path.basename(os.path.dirname(src)), len(rows)))
    print("")
    print("Split by the broker's ownership tag:")
    for tag in sorted({r["magic"] for r in rows}):
        sel = [r for r in rows if r["magic"] == tag]
        p = sum(float(r["profit"]) for r in sel)
        c = sum(float(r["commission"]) for r in sel)
        s = sum(float(r["swap"]) for r in sel)
        who = "SYSTEM" if tag == "234000" else "MANUAL / untagged"
        print("  tag {:<8} {:<18} deals {:>3}   gross {:>9.2f}   commission {:>8.2f}   swap {:>6.2f}   net {:>9.2f}".format(
            tag, who, len(sel), p, c, s, p + c + s))
    print("")
    print("  Latest deal in the export: {} UTC".format(max(r["time"] for r in rows)[11:19]))

    try:
        import pymongo

        db = pymongo.MongoClient("mongodb://localhost:27017", serverSelectionTimeoutMS=3000)["nifty_backtest"]
        d0 = datetime.fromisoformat(date)
        es = sorted(
            (str(e["timestamp"]), float(e["balance"]))
            for e in db["equity_samples"].find({"timestamp": {"$gte": d0, "$lt": d0 + timedelta(days=1)}})
        )
        if es:
            print("")
            print("Balance line for the same day (independent of the deal file):")
            print("  first sample {}  balance {:.2f}".format(es[0][0][11:19], es[0][1]))
            print("  last  sample {}  balance {:.2f}".format(es[-1][0][11:19], es[-1][1]))
            print("  day change   {:+.2f}   over {} samples".format(es[-1][1] - es[0][1], len(es)))
    except Exception as exc:
        print("")
        print("  (balance line unavailable: {})".format(type(exc).__name__))


# ------------------------------------------------------------------ census
def census() -> None:
    p = os.path.join(REPO, "data", "audit", "freeze_census_0727", "deals.csv")
    rows = list(csv.DictReader(open(p)))
    _hdr("WHOLE-WINDOW CENSUS  -  13 to 27 July 2026  -  broker deal export")
    app = [r for r in rows if r["magic"] == "234000"]
    man = [r for r in rows if r["magic"] != "234000"]
    for name, sel in (("SYSTEM", app), ("MANUAL", man)):
        pr = sum(float(r["profit"]) for r in sel)
        co = sum(float(r["commission"]) for r in sel)
        pos = {r["position_id"] for r in sel}
        print("  {:<8} deals {:>4}  positions {:>4}  gross {:>9.2f}  commission {:>8.2f}  net {:>9.2f}".format(
            name, len(sel), len(pos), pr, co, pr + co))

    agg = {}
    for r in app:
        a = agg.setdefault(r["position_id"], [0.0, r["symbol"]])
        a[0] += float(r["profit"]) + float(r["commission"]) + float(r["swap"])
    tot = [v[0] for v in agg.values()]
    wins = [x for x in tot if x > 0]
    loss = [x for x in tot if x < 0]
    flat = [x for x in tot if x == 0]
    print("")
    print("  System positions {}   winners {}   losers {}   flat {}".format(
        len(tot), len(wins), len(loss), len(flat)))
    print("  average winner {:+.2f}   average loser {:+.2f}   largest {:+.2f} / {:+.2f}".format(
        sum(wins) / len(wins), sum(loss) / len(loss), max(tot), min(tot)))
    per = Counter()
    for v in agg.values():
        per[v[1]] += v[0]
    print("")
    print("  By instrument:")
    for k, v in sorted(per.items(), key=lambda kv: -kv[1]):
        print("    {:<8} {:>9.2f}".format(k, v))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=["funnel", "trace", "reconcile", "census"])
    ap.add_argument("--date", default="2026-07-16")
    ap.add_argument("--pnl", type=float, default=None)
    a = ap.parse_args()
    if a.what == "funnel":
        funnel(a.date)
    elif a.what == "trace":
        trace(a.date, a.pnl)
    elif a.what == "reconcile":
        reconcile(a.date)
    else:
        census()


if __name__ == "__main__":
    main()
