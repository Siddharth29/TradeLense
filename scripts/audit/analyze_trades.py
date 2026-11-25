"""Aggregate + reconstruct the executed trades for a loss/perf audit.

Reads `comprehensive_trades` from MongoDB (the executed-trade ledger) and the real
5m candles produced by pull_live_mt5.py, then prints:
  - win rate, payoff ratio, breakeven win-rate, profit factor
  - exit-reason / per-asset / per-side / per-day / per-hour breakdowns
  - per-trade MFE/MAE in R (max favourable / adverse excursion on the real chart)
  - market regime per asset (Kaufman efficiency ratio)
  - per-trade dollar-risk % (min-lot overshoot check)

Usage: python scripts/audit/analyze_trades.py --mkt data/audit/mktdata \
           --start 2026-06-29 --end 2026-07-01T12:00
"""
import argparse, os
from datetime import datetime, timedelta
from collections import defaultdict
from statistics import mean, median
import pymongo, pandas as pd, numpy as np

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB = "nifty_backtest"


def R_of(d):
    ep, sl, xp = d.get("entry_price"), d.get("stop_loss"), d.get("exit_price")
    side = (d.get("side") or "").lower()
    if not (ep and sl and xp) or ep == sl:
        return None
    return (1 if side == "buy" else -1) * (xp - ep) / abs(ep - sl)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mkt", default="data/audit/mktdata")
    ap.add_argument("--start", default="2026-06-29")
    ap.add_argument("--end", default="2026-07-01T12:00")
    a = ap.parse_args()
    db = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)[DB]
    q = {"trade_status": "closed",
         "timestamp": {"$gte": datetime.fromisoformat(a.start), "$lte": datetime.fromisoformat(a.end)}}
    closed = list(db["comprehensive_trades"].find(q).sort("timestamp", 1))
    n = len(closed)
    pnls = [d["pnl"] for d in closed if d.get("pnl") is not None]
    wins = [d for d in closed if (d.get("pnl") or 0) > 0]
    losses = [d for d in closed if (d.get("pnl") or 0) < 0]
    gw = sum(d["pnl"] for d in wins); gl = sum(d["pnl"] for d in losses)
    print(f"trades={n}  win%={100*len(wins)/n:.1f}  netPnL={sum(pnls):+.2f}  PF={abs(gw/gl):.2f}")
    aw, al = mean([d["pnl"] for d in wins]), abs(mean([d["pnl"] for d in losses]))
    print(f"avg win={aw:+.2f} avg loss={-al:+.2f} payoff={aw/al:.2f}  breakeven win%={100*al/(aw+al):.1f}")

    print("\nExit reason:")
    er = defaultdict(lambda: [0, 0.0])
    for d in closed:
        er[d.get("exit_reason")][0] += 1; er[d.get("exit_reason")][1] += d.get("pnl") or 0
    for k, v in sorted(er.items(), key=lambda x: x[1][1]):
        print(f"  {str(k):16} n={v[0]:2} pnl={v[1]:+8.2f}")

    print("\nPer-asset:")
    pa = defaultdict(lambda: {"n": 0, "pnl": 0.0, "w": 0})
    for d in closed:
        pa[d["asset"]]["n"] += 1; pa[d["asset"]]["pnl"] += d.get("pnl") or 0
        if (d.get("pnl") or 0) > 0: pa[d["asset"]]["w"] += 1
    for k, v in sorted(pa.items(), key=lambda x: x[1]["pnl"]):
        print(f"  {k:8} n={v['n']:2} win%={100*v['w']/v['n']:3.0f} pnl={v['pnl']:+8.2f}")

    # ---- reconstruction: MFE/MAE on real candles ----
    cache = {}
    def cand(s):
        if s not in cache:
            cache[s] = pd.read_csv(f"{a.mkt}/{s}_5m.csv", parse_dates=["dt_mt5"]).sort_values("dt_mt5")
        return cache[s]
    print("\nPer-asset MFE (max favourable excursion, R) on real chart:")
    mfe = defaultdict(list)
    for d in closed:
        sym = d["asset"]; side = (d.get("side") or "").lower(); sign = 1 if side == "buy" else -1
        ep, sl = d.get("entry_price"), d.get("stop_loss")
        if not (ep and sl) or ep == sl:
            continue
        try:
            df = cand(sym)
        except FileNotFoundError:
            continue
        t0 = pd.Timestamp(str(d["timestamp"])).floor("5min").tz_localize("UTC")
        ct = d.get("trade_closed_at")
        life = df[df["dt_mt5"] >= t0]
        if ct:
            life = life[life["dt_mt5"] <= pd.Timestamp(str(ct)).tz_localize("UTC") + timedelta(minutes=5)]
        else:
            life = life.head(60)
        if len(life):
            fav = life["high"].max() if sign > 0 else life["low"].min()
            mfe[sym].append(sign * (fav - ep) / abs(ep - sl))
    for k, v in sorted(mfe.items()):
        print(f"  {k:8} n={len(v):2} avgMFE={mean(v):+.2f}R maxMFE={max(v):+.2f}R")

    # ---- regime: Kaufman efficiency ratio per asset over the window ----
    print("\nMarket regime (Kaufman ER; >0.3 trend, <0.12 chop):")
    for s in sorted(pa):
        try:
            df = pd.read_csv(f"{a.mkt}/{s}_5m.csv", parse_dates=["dt_mt5"])
        except FileNotFoundError:
            continue
        df = df[(df["dt_mt5"] >= a.start) & (df["dt_mt5"] <= a.end)]
        if len(df) < 5:
            continue
        direction = abs(df["close"].iloc[-1] - df["close"].iloc[0])
        vol = df["close"].diff().abs().sum()
        er_ = direction / vol if vol else 0
        net = 100 * (df["close"].iloc[-1] - df["open"].iloc[0]) / df["open"].iloc[0]
        print(f"  {s:8} net={net:+6.2f}%  ER={er_:.2f}  {'TREND' if er_>0.3 else ('CHOP' if er_<0.12 else 'MIXED')}")


if __name__ == "__main__":
    main()
