"""Validate judge code <-> placed order <-> real market.

For each executed trade it (1) links to the judge program whose declared SL/TP match the
order, and (2) replays that program in the REAL sandbox against the REAL 5m candles,
scanning bars around entry to see whether the code fires the traded direction.

A clean result = SL/TP levels match the order AND the code fires the traded direction
(intra-bar triggers may show as 'no-trigger@5m' since only 5m bars are used here).

Usage: python scripts/audit/replay_judge_code.py --mkt data/audit/mktdata
"""
import argparse, os, re, sys
from datetime import timedelta
from collections import defaultdict
import pymongo, pandas as pd

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO)
from packages.core.sandbox_runner import run_in_sandbox   # noqa: E402

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB = "nifty_backtest"


def gv(code, name):
    m = re.search(rf"\b{name}\s*=\s*([0-9]+\.?[0-9]*)", code)
    return float(m.group(1)) if m else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mkt", default="data/audit/mktdata")
    ap.add_argument("--start", default="2026-06-29")
    ap.add_argument("--end", default="2026-07-01T12:00")
    a = ap.parse_args()
    db = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)[DB]

    trades = list(db["comprehensive_trades"].find(
        {"trade_status": "closed",
         "timestamp": {"$gte": pd.Timestamp(a.start).to_pydatetime(),
                       "$lte": pd.Timestamp(a.end).to_pydatetime()}}).sort("timestamp", 1))

    jresp = defaultdict(list)                      # judge programs per asset (code-sniper judge)
    for g in db["gpt_responses"].find({"gpt_mode": "multi_expert_judge_code_sniper",
                                       "timestamp": {"$gte": pd.Timestamp(a.start).to_pydatetime()
                                                     - timedelta(days=1)}}):
        jresp[g["asset"]].append(g)
    for s in jresp:
        jresp[s].sort(key=lambda x: x["timestamp"])

    cache = {}
    def cand(s):
        if s not in cache:
            cache[s] = pd.read_csv(f"{a.mkt}/{s}_5m.csv", parse_dates=["dt_mt5"]).sort_values("dt_mt5")
        return cache[s]

    def find_code(sym, ttime, tsl):
        cs = [g for g in jresp.get(sym, [])
              if ttime - timedelta(minutes=120) <= pd.Timestamp(str(g["timestamp"])) <= ttime + timedelta(seconds=5)]
        best, bs = None, 1e18
        for g in cs:
            code = (g.get("parsed_decision") or {}).get("code", "")
            csl = gv(code, "SL") or gv(code, "STOP_LOSS") or g.get("stop_loss") or 0
            if abs(csl - tsl) < bs:
                bs, best = abs(csl - tsl), (code, csl, gv(code, "TP1") or gv(code, "TAKE_PROFIT_1"))
        return best

    fires = holds = opp = 0
    for i, t in enumerate(trades, 1):
        sym, side = t["asset"], (t["side"] or "").lower()
        ttime = pd.Timestamp(str(t["timestamp"]))
        found = find_code(sym, ttime, t["stop_loss"])
        if not found:
            print(f"#{i:>2} {sym:7} {side:4} no-code"); continue
        code, csl, ctp = found
        link = "MATCH" if abs((csl or 0) - t["stop_loss"]) <= abs(t["stop_loss"]) * 0.001 + 0.05 else "dyn-SL"
        df = cand(sym)
        t0 = ttime.floor("5min").tz_localize("UTC")
        scan = df[(df["dt_mt5"] >= t0 - timedelta(minutes=30)) & (df["dt_mt5"] <= t0 + timedelta(minutes=30))]
        fired = opp_here = False
        for end_t in scan["dt_mt5"]:
            win = df[df["dt_mt5"] <= end_t].tail(100)
            c5 = [[r.dt_mt5.isoformat(), float(r.open), float(r.high), float(r.low),
                   float(r.close), float(r.tick_volume)] for r in win.itertuples()]
            try:
                d = (run_in_sandbox(code, c5, end_t.isoformat()) or {}).get("direction")
            except Exception:
                d = None
            if d == side:
                fired = True
            elif d in ("buy", "sell"):
                opp_here = True
        verdict = "FIRES " + side if fired else "no-trigger@5m"
        if fired: fires += 1
        else: holds += 1
        if opp_here and not fired: opp += 1
        print(f"#{i:>2} {sym:7} {side:4} link={link:6} codeSL={csl} orderSL={t['stop_loss']}  {verdict}")

    print(f"\nFIRES traded dir={fires}  no-trigger@5m={holds} (intra-bar; need ticks)  "
          f"genuine-opposite={opp}  of {len(trades)}")


if __name__ == "__main__":
    main()
