"""Fetch recent 5m/1h candles for XAU/GBP/BTC from the already-logged-in MT5
terminal and save to data/processed/. Read-only. Then quantify post-stop
excursion (opportunity cost) for the worst decision trades.
"""
import MetaTrader5 as mt5
import csv, os
from datetime import datetime, timedelta

assert mt5.initialize(), mt5.last_error()
SYMS = {"XAUUSD": "XAUUSDm", "GBPUSD": "GBPUSDm", "BTCUSD": "BTCUSDm"}
TFS = {"5m": mt5.TIMEFRAME_M5, "1h": mt5.TIMEFRAME_H1}
os.makedirs("data/processed", exist_ok=True)
out = {}
for clean, sym in SYMS.items():
    out[clean] = {}
    for tflab, tf in TFS.items():
        n = 2500 if tflab == "5m" else 400
        rates = mt5.copy_rates_from_pos(sym, tf, 0, n)
        if rates is None:
            print("FAIL", sym, tflab, mt5.last_error()); continue
        path = f"data/processed/{clean.lower()}_{tflab}_jun.csv"
        with open(path, "w", newline="") as f:
            w = csv.writer(f); w.writerow(["time","open","high","low","close","tick_volume"])
            for r in rates:
                w.writerow([datetime.utcfromtimestamp(r['time']).isoformat(), r['open'], r['high'], r['low'], r['close'], r['tick_volume']])
        out[clean][tflab] = [(datetime.utcfromtimestamp(r['time']), r['open'], r['high'], r['low'], r['close']) for r in rates]
        print(f"saved {path}  {len(rates)} bars  {out[clean][tflab][0][0]} -> {out[clean][tflab][-1][0]}")
mt5.shutdown()

# ---- opportunity-cost: for a stopped trade, how far did price go in the intended direction after the stop? ----
def excursion_after(candles5, t_close, direction, stop_px, horizon_h=8):
    """From t_close forward horizon_h hours, return best favorable excursion (in price)
    beyond the stop, i.e. how much the intended-direction move continued."""
    end = t_close + timedelta(hours=horizon_h)
    seg = [c for c in candles5 if t_close <= c[0] <= end]
    if not seg: return None
    if direction in ("sell","short"):
        best = min(c[3] for c in seg)  # lowest low
        return stop_px - best          # how far below the stop price fell
    else:
        best = max(c[2] for c in seg)
        return best - stop_px

print("\n=== OPPORTUNITY COST: post-stop continuation on key losing trades ===")
import json
data = json.load(open("total trades.json", encoding="utf-8"))["trades"]
def pdt(s):
    if not s: return None
    s=s.split('+')[0]
    for f in ('%Y-%m-%dT%H:%M:%S.%f','%Y-%m-%dT%H:%M:%S'):
        try:return datetime.strptime(s,f)
        except:pass
checked=0
for t in data:
    a=t["asset"]
    if a not in out or "5m" not in out[a]: continue
    if t.get("exit_reason") not in ("MT5_REASON_4","SL"): continue
    tc=pdt(t.get("trade_closed_at"))
    if not tc or tc < datetime(2026,6,8): continue   # focus on the recent regime-transition window
    sl=t.get("stop_loss"); side=t.get("side"); pnl=t.get("pnl") or 0
    if not sl: continue
    exc=excursion_after(out[a]["5m"], tc, side, sl, horizon_h=8)
    if exc is None: continue
    entry=t.get("entry_price")
    risk=abs(entry-sl) if entry and sl else None
    excR = exc/risk if risk else None
    print(f"  {tc.strftime('%m-%d %H:%M')} {a:7} {side:5} stop@{sl:<9.4g} pnl={pnl:6.2f}  post-stop continuation={exc:9.4g} ({excR:.1f}R)" if excR is not None else "")
    checked+=1
print(f"[{checked} stopped trades checked]")
