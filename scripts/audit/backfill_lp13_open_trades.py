"""One-off repair for the three `comprehensive_trades` docs damaged or stranded by
the 2026-07-27 LP-13 defects (see docs/trading_insights/daily/AUDIT_2026-07-27.md §6).

Read-only against MT5 (deal history only — NO orders are ever placed). Dry-run by
default: prints exactly what it would write. Pass --apply to write to Mongo.

    python scripts/audit/backfill_lp13_open_trades.py            # inspect
    python scripts/audit/backfill_lp13_open_trades.py --apply    # write

Targets:
  1. XAGUSD 6a50f1857f8fa2adf7423d22 — opened 2026-07-10 13:20, ticket 2193965818.
     Left `open` because the pre-fix recovery cleared the monitor row without
     exit accounting (the original LP-13 symptom).
  2. EURUSD 6a62d2dd6855fdaff063d31c — opened 2026-07-24 02:50, ticket 2209415879.
     This IS LP-13 occurrence #2; the 07-27 fix should have closed it but the
     synthetic-exit path crashed on a legacy ticket first (BUG-3).
  3. USTEC  6a512af1b0eb589e38fa519d — opened 2026-07-10 17:25, ticket 2194353981.
     CORRUPTED by BUG-1: a duplicate 07-27 exit closed it by SYMBOL fallback with
     that day's exit_price 28534.98 / pnl 3.12 / trade_closed_at 2026-07-27.
     Repaired from its own real closing deal.

Every write comes from a real broker deal. If a deal cannot be found (MT5 deal
history is finite), the doc is left exactly as-is and the row is reported as
UNRESOLVED — nothing is invented.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5
from bson import ObjectId
from pymongo import MongoClient

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

# (_id, symbol, position_ticket, opened_at_utc, note)
TARGETS = [
    ("6a50f1857f8fa2adf7423d22", "XAGUSD", 2193965818,
     datetime(2026, 7, 10, 13, 20, tzinfo=timezone.utc), "stranded open (LP-13 original)"),
    ("6a62d2dd6855fdaff063d31c", "EURUSD", 2209415879,
     datetime(2026, 7, 24, 2, 50, tzinfo=timezone.utc), "stranded open (LP-13 #2)"),
    ("6a512af1b0eb589e38fa519d", "USTEC", 2194353981,
     datetime(2026, 7, 10, 17, 25, tzinfo=timezone.utc), "CORRUPTED by BUG-1"),
]

# What BUG-1 wrote onto the USTEC doc — used only to confirm we are repairing the
# damage we think we are, never to derive a replacement value.
BUG1_STAMP = {"exit_price": 28534.98, "pnl": 3.12, "exit_reason": "TIMEOUT"}

_DEAL_REASON = {0: "CLIENT", 1: "MOBILE", 2: "WEB", 3: "EXPERT", 4: "SL", 5: "TP", 6: "SO"}


def load_live_creds():
    p = os.path.join(REPO, "config", "mt5_config.local.json")
    if not os.path.exists(p):
        sys.exit(f"LIVE creds not found at {p}. Refusing to use demo config.")
    cfg = json.load(open(p))["mt5"]
    if "real" not in cfg["server"].lower():
        sys.exit(f"server '{cfg['server']}' is not a real account — refusing")
    return cfg["server"], int(cfg["login"]), cfg["password"]


def connect():
    server, login, pw = load_live_creds()
    if not mt5.initialize(login=login, password=pw, server=server):
        if not mt5.initialize():
            sys.exit(f"mt5.initialize failed: {mt5.last_error()}")
        if not mt5.login(login=login, password=pw, server=server):
            sys.exit(f"mt5.login failed: {mt5.last_error()}")
    ai = mt5.account_info()
    if ai is None or ai.login != login or "real" not in str(ai.server).lower():
        sys.exit(f"ACCOUNT VERIFICATION FAILED: {getattr(ai, 'login', None)}@{getattr(ai, 'server', None)}")
    print(f"ACTIVE ACCOUNT: {ai.login} {ai.server} balance={ai.balance} {ai.currency}\n")


def find_close(position_ticket: int, opened_at: datetime):
    """The OUT deal for this exact position, from its own open window forward."""
    frm = opened_at - timedelta(days=2)
    to = datetime.now(timezone.utc) + timedelta(days=1)
    deals = mt5.history_deals_get(frm, to, position=position_ticket)
    if not deals:
        deals = mt5.history_deals_get(frm, to) or []
    out = []
    for d in deals:
        dd = d._asdict() if hasattr(d, "_asdict") else dict(d)
        if int(dd.get("position_id", 0) or 0) != position_ticket:
            continue
        if int(dd.get("entry", -1)) != 1:          # DEAL_ENTRY_OUT
            continue
        out.append(dd)
    if not out:
        return None
    out.sort(key=lambda x: x.get("time", 0))
    # Sum partials; price/time/reason come from the FINAL closing deal.
    total_profit = sum(float(x.get("profit", 0.0)) for x in out)
    total_comm = sum(float(x.get("commission", 0.0)) for x in out)
    last = out[-1]
    t = last.get("time")
    closed_at = datetime.fromtimestamp(int(t), tz=timezone.utc) if isinstance(t, (int, float)) else t
    return {
        "exit_price": float(last.get("price", 0.0)),
        "pnl": total_profit,
        "commission": total_comm,
        "closed_at": closed_at,
        "reason": _DEAL_REASON.get(int(last.get("reason", 0)), f"REASON_{last.get('reason')}"),
        "magic": int(last.get("magic", 0) or 0),
        "n_deals": len(out),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write to Mongo (default: dry run)")
    ap.add_argument("--mongo", default="mongodb://localhost:27017/")
    a = ap.parse_args()

    connect()
    db = MongoClient(a.mongo, serverSelectionTimeoutMS=5000)["nifty_backtest"]
    coll = db["comprehensive_trades"]

    resolved = unresolved = skipped = 0
    for doc_id, symbol, ticket, opened_at, note in TARGETS:
        doc = coll.find_one({"_id": ObjectId(doc_id)})
        print(f"=== {symbol} {doc_id}  ({note})")
        if doc is None:
            print("    doc not found — skipping\n")
            skipped += 1
            continue
        print(f"    current: status={doc.get('trade_status')} entry={doc.get('entry_price')} "
              f"exit={doc.get('exit_price')} pnl={doc.get('pnl')} "
              f"closed_at={doc.get('trade_closed_at')} reason={doc.get('exit_reason')}")

        if doc_id == "6a512af1b0eb589e38fa519d":
            stamped = all(doc.get(k) == v for k, v in BUG1_STAMP.items())
            print(f"    BUG-1 stamp present: {stamped}")

        deal = find_close(ticket, opened_at)
        if deal is None:
            print(f"    !! NO closing deal found for position {ticket} in MT5 history "
                  f"(history may not reach {opened_at:%Y-%m-%d}) — LEFT UNCHANGED\n")
            unresolved += 1
            continue

        print(f"    broker : exit={deal['exit_price']} pnl={deal['pnl']:+.2f} "
              f"comm={deal['commission']:+.2f} closed_at={deal['closed_at']} "
              f"reason={deal['reason']} magic={deal['magic']} deals={deal['n_deals']}")

        if deal["magic"] != 234000:
            print(f"    !! magic {deal['magic']} is not the app's 234000 — LEFT UNCHANGED\n")
            unresolved += 1
            continue

        update = {
            "trade_status": "closed",
            "exit_price": deal["exit_price"],
            "exit_reason": f"BACKFILL_{deal['reason']}",
            "pnl": deal["pnl"],
            "trade_closed_at": deal["closed_at"],
            "backfilled_at": datetime.now(timezone.utc),
            "backfill_note": f"LP-13 repair 2026-07-28: {note}; resolved from broker "
                             f"position {ticket}",
        }
        if a.apply:
            coll.update_one({"_id": ObjectId(doc_id)}, {"$set": update})
            print("    APPLIED\n")
        else:
            print(f"    would write: {update}\n")
        resolved += 1

    print(f"resolved={resolved} unresolved={unresolved} skipped={skipped} "
          f"{'(APPLIED)' if a.apply else '(DRY RUN — pass --apply to write)'}")
    mt5.shutdown()


if __name__ == "__main__":
    main()
