"""Extract every trade of the 2026-07-13 -> 07-29 freeze backtest, with the
fields the exit engine needs to be replayed: entry/initial_stop/tp1/tp2/qty/
atr_1h, plus which engine path closed it and which checkpoint armed it.

The audit CSV (data/audit/backtest_trade_rows_*.csv) drops tp1/tp2/atr_1h and
the checkpoint index, so it cannot drive PositionSimulator. This one can.

Output: data/audit/freeze_backtest_trades_full.csv
"""
from __future__ import annotations

import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import pandas as pd
from pymongo import MongoClient

ASSETS = ["XAUUSD", "XAGUSD", "USTEC", "BTCUSD", "EURUSD"]
OUT = REPO / "data" / "audit" / "freeze_backtest_trades_full.csv"

DEAD = {None, "No Entry", "Flat", "Filtered", "Open"}


def ts(v):
    if v is None:
        return None
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def main() -> int:
    db = MongoClient("mongodb://localhost:27017", serverSelectionTimeoutMS=5000)["nifty_backtest"]
    rows = []
    for asset in ASSETS:
        doc = db["backtests"].find_one({"test_name": f"freeze_audit_2026-07-13_2026-07-29_{asset}"})
        if not doc:
            print(f"{asset}: no backtest doc")
            continue
        bid = str(doc["_id"])
        its = list(db["backtest_iterations"].find({"backtest_id": bid}).sort("iteration", 1))

        # Checkpoints where a GPT call actually happened (signal_data present)
        # = the slot was free. Used to date a trade's ARMING checkpoint.
        armed_at = sorted(
            ts(i.get("iteration_timestamp") or i.get("timestamp"))
            for i in its
            if i.get("signal_data") and not i.get("monitoring_active_trade")
        )

        for it in its:
            to = it.get("trade_outcome") or it.get("outcome") or {}
            if to.get("result") in DEAD:
                continue
            pred = it.get("prediction") or {}
            entry_t = ts(to.get("entry_time") or to.get("signal_time") or pred.get("entry_timestamp"))
            exit_t = ts(to.get("trade_closed_at") or to.get("exit_time"))
            entry = to.get("entry")
            isl = to.get("initial_stop") or to.get("stop")
            tp1 = to.get("take_profit_1") or pred.get("tp1")
            tp2 = to.get("take_profit_2") or pred.get("tp2") or to.get("target")
            qty = pred.get("quantity")
            if None in (entry_t, exit_t, entry, isl, tp1, tp2, qty):
                print(f"  {asset} it={it.get('iteration')} SKIP incomplete "
                      f"(entry_t={entry_t} tp1={tp1} tp2={tp2} qty={qty})")
                continue

            ck = ts(it.get("iteration_timestamp") or it.get("timestamp"))
            # Arming checkpoint = last free-slot GPT call at or before entry.
            arm = None
            for a in armed_at:
                if a <= entry_t:
                    arm = a
                else:
                    break

            rows.append({
                "asset": asset,
                "iteration": it.get("iteration"),
                "entry_iteration": it.get("entry_iteration"),
                "close_checkpoint": ck.isoformat(),
                "arm_checkpoint": arm.isoformat() if arm is not None else "",
                "path": "monitor" if it.get("monitoring_active_trade") else "in_iteration",
                "direction": to.get("direction") or pred.get("direction"),
                "entry_time": entry_t.isoformat(),
                "exit_time": exit_t.isoformat(),
                "entry": entry,
                "initial_sl": isl,
                "final_sl": to.get("stop"),
                "tp1": tp1,
                "tp2": tp2,
                "qty": qty,
                "atr_1h": pred.get("atr_1h") or "",
                "result": to.get("result"),
                "exit_price": to.get("exit_price"),
                "profit": to.get("profit"),
                "ttl_minutes": (it.get("signal_data") or {}).get("ttl_minutes") or "",
                "confidence": pred.get("confidence") or "",
            })

    rows.sort(key=lambda r: (r["asset"], r["entry_time"]))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"\nwrote {len(rows)} trades -> {OUT}")
    for a in ASSETS:
        n = sum(1 for r in rows if r["asset"] == a)
        p = sum(float(r["profit"]) for r in rows if r["asset"] == a)
        mon = sum(1 for r in rows if r["asset"] == a and r["path"] == "monitor")
        print(f"  {a:7} n={n:3}  monitor-path={mon:3}  pnl=${p:+.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
