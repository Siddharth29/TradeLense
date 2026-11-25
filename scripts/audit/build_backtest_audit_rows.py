"""Per-trade audit rows for the 2026-07-13->07-29 backtest run (5 assets, $2000 wallet).

Adapts the live /audit-day methodology (docs/trading_insights/PATTERN_LIBRARY.md +
OBSERVATION_PARAMETERS.md) to backtest data: same MFE/post-exit-R/LP-01/LP-03/LP-04/LP-10
measurements, computed from the real candle files already pulled this session
(data/processed/*_5m_2years.csv) and the backtest_iterations Mongo collection instead of
live MT5 deals + day logs.

Output: data/audit/backtest_trade_rows_2026-07-13_2026-07-29.csv (one row per trade).
"""
from __future__ import annotations

import csv
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import pandas as pd
from pymongo import MongoClient

from packages.core.backtest_account import size_position, commission_for
from packages.core.config import CS_DEFAULT_RISK_PCT

ASSETS = ["XAUUSD", "XAGUSD", "USTEC", "BTCUSD", "EURUSD"]
POST_EXIT_HOURS = 3

client = MongoClient("mongodb://localhost:27017", serverSelectionTimeoutMS=5000)
db = client["nifty_backtest"]


def load_candles(asset: str) -> pd.DataFrame:
    path = REPO / "data" / "processed" / f"{asset.lower()}_5m_2years.csv"
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df.sort_index()


def mfe_r(candles: pd.DataFrame, entry_time, exit_time, entry: float, sl: float, direction: str) -> float | None:
    dist = abs(entry - sl)
    if dist <= 0:
        return None
    win = candles.loc[entry_time:exit_time]
    if win.empty:
        return None
    if direction == "long":
        return (win["high"].max() - entry) / dist
    return (entry - win["low"].min()) / dist


def post_exit_r(candles: pd.DataFrame, exit_time, entry: float, sl: float, direction: str, hours: int) -> float | None:
    dist = abs(entry - sl)
    if dist <= 0:
        return None
    end = exit_time + timedelta(hours=hours)
    win = candles.loc[exit_time:end]
    if win.empty:
        return None
    if direction == "long":
        return (win["high"].max() - entry) / dist
    return (entry - win["low"].min()) / dist


def parse_ts(s) -> datetime | None:
    if s is None:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    ts = pd.Timestamp(s)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.to_pydatetime()


def main() -> None:
    rows = []
    for asset in ASSETS:
        candles = load_candles(asset)
        doc = db["backtests"].find_one({"test_name": f"freeze_audit_2026-07-13_2026-07-29_{asset}"})
        if not doc:
            continue
        bid = str(doc["_id"])
        trades = list(db["backtest_iterations"].find(
            {"backtest_id": bid, "trade_outcome.result": {"$nin": [None, "No Entry", "Flat"]}}
        ))
        for t in trades:
            to = t["trade_outcome"]
            pred = t.get("prediction") or {}
            entry = to.get("entry")
            sl = to.get("initial_stop") or to.get("stop")
            exit_price = to.get("exit_price")
            profit = to.get("profit")
            direction = to.get("direction") or pred.get("direction")
            entry_time = parse_ts(to.get("entry_time") or to.get("signal_time") or pred.get("entry_timestamp"))
            exit_time = parse_ts(to.get("trade_closed_at") or to.get("exit_time"))
            qty = pred.get("quantity")
            if None in (entry, sl, exit_price, profit, direction, entry_time, exit_time, qty):
                continue

            risk_dist = abs(entry - sl)
            sgn = 1.0 if direction == "long" else -1.0
            realized_r = sgn * (exit_price - entry) / risk_dist if risk_dist else None

            mfe = mfe_r(candles, entry_time, exit_time, entry, sl, direction)
            post_r = post_exit_r(candles, exit_time, entry, sl, direction, POST_EXIT_HOURS)

            # LP-01: risk multiple at a flat $2000 reference balance (this run's actual
            # sizing basis) - overshoot = implied_risk / intended_risk at that reference.
            sizing = size_position(asset, 2000.0, entry, sl, CS_DEFAULT_RISK_PCT)
            risk_multiple = (qty / sizing.lot) if (sizing.lot and sizing.lot > 0) else None

            # LP-04 / LP-10 time-window flags (entry UTC time-of-day)
            hh, mm = entry_time.hour, entry_time.minute
            mins_of_day = hh * 60 + mm
            near_release = any(abs(mins_of_day - slot) <= 10 for slot in (12 * 60 + 30, 14 * 60 + 30))
            near_maintenance = 0 <= ((20 * 60 + 55) - mins_of_day) <= 60

            # wrong-direction vs LP-03 (stop-swept-right-thesis) classification
            is_loss = profit <= 0
            wrong_direction = is_loss and (mfe is not None and mfe < 0.5) and (post_r is not None and post_r < 1.0)
            lp03 = is_loss and not wrong_direction and (mfe is not None or post_r is not None)

            rows.append({
                "asset": asset, "entry_time": entry_time.isoformat(), "exit_time": exit_time.isoformat(),
                "direction": direction, "entry": entry, "sl": sl, "exit_price": exit_price,
                "result": to.get("result"), "profit": round(profit, 2),
                "planned_rr": to.get("risk_reward"),
                "realized_r": round(realized_r, 3) if realized_r is not None else None,
                "mfe_r": round(mfe, 3) if mfe is not None else None,
                "post_exit_3h_r": round(post_r, 3) if post_r is not None else None,
                "qty": qty, "risk_multiple_vs_2k_ideal": round(risk_multiple, 3) if risk_multiple else None,
                "near_release_window": near_release, "near_maintenance_window": near_maintenance,
                "is_loss": is_loss, "wrong_direction": wrong_direction, "lp03_stop_swept": lp03,
                "commission": round(commission_for(asset, qty), 4),
            })

    out = REPO / "data" / "audit" / "backtest_trade_rows_2026-07-13_2026-07-29.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} rows -> {out}")


if __name__ == "__main__":
    main()
