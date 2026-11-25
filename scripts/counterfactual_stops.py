"""Counterfactual: would a proper ATR-scaled stop have survived the noise bounce
AND let the (correct) thesis reach target? Uses freshly downloaded *_5m_jun.csv
and *_1h_jun.csv. Read-only analysis.
"""
import csv, json
from datetime import datetime, timedelta

def load5(asset):
    rows=[]
    with open(f"data/processed/{asset.lower()}_5m_jun.csv") as f:
        for r in csv.DictReader(f):
            rows.append((datetime.fromisoformat(r["time"]), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])))
    return rows
def load1h(asset):
    rows=[]
    with open(f"data/processed/{asset.lower()}_1h_jun.csv") as f:
        for r in csv.DictReader(f):
            rows.append((datetime.fromisoformat(r["time"]), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])))
    return rows
def atr_1h_at(c1h, t, period=14):
    bars=[c for c in c1h if c[0] <= t]
    if len(bars) < period+1: return None
    trs=[]
    for i in range(len(bars)-period, len(bars)):
        h,l,pc = bars[i][2], bars[i][3], bars[i-1][4]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    return sum(trs)/len(trs)

ASSETS=["XAUUSD","GBPUSD","BTCUSD"]
C5={a:load5(a) for a in ASSETS}
C1H={a:load1h(a) for a in ASSETS}

def pdt(s):
    if not s: return None
    s=s.split('+')[0]
    for f in ('%Y-%m-%dT%H:%M:%S.%f','%Y-%m-%dT%H:%M:%S'):
        try:return datetime.strptime(s,f)
        except:pass

data=json.load(open("total trades.json",encoding="utf-8"))["trades"]

print("=== REGIME READ (1h EMA20/50 + range), 06-08 -> 06-10 ===")
for a in ASSETS:
    seg=[c for c in C1H[a] if c[0]>=datetime(2026,6,8)]
    if not seg: continue
    closes=[c[4] for c in C1H[a]]
    def ema(vals,p):
        k=2/(p+1); e=vals[0]
        for v in vals[1:]: e=v*k+e*(1-k)
        return e
    e20=ema(closes[-60:],20); e50=ema(closes[-120:],50)
    hi=max(c[2] for c in seg); lo=min(c[3] for c in seg)
    net=seg[-1][4]-seg[0][1]
    atr=atr_1h_at(C1H[a], seg[-1][0])
    print(f"  {a:7} 08->10: net={net:+.4g} ({100*net/seg[0][1]:+.1f}%) range[{lo:.4g},{hi:.4g}] EMA20={e20:.4g} EMA50={e50:.4g} {'DOWN' if e20<e50 else 'UP'} 1hATR={atr:.4g}({100*atr/seg[-1][4]:.2f}%)")

print("\n=== COUNTERFACTUAL STOP TEST (recent stopped SELLS, post 06-08) ===")
print("  For each: orig stop dist vs 1.2x1hATR stop; did wider stop survive 8h AND would entry->TP1 have hit?")
surv=0; tot=0; would_win=0
for t in data:
    a=t["asset"]
    if a not in C5: continue
    if t.get("exit_reason") not in ("MT5_REASON_4","SL"): continue
    side=t.get("side")
    if side not in ("sell","short"): continue
    texec=pdt(t.get("trade_executed_at") or t.get("signal_received_at")); tc=pdt(t.get("trade_closed_at"))
    if not texec or texec < datetime(2026,6,8): continue
    entry=t.get("entry_price"); sl=t.get("stop_loss"); tp=t.get("take_profit")
    if not (entry and sl): continue
    atr=atr_1h_at(C1H[a], texec)
    if not atr: continue
    orig_dist=abs(entry-sl)
    wide_dist=max(orig_dist, 1.2*atr)
    wide_sl=entry+wide_dist  # short: stop above entry
    # walk forward from entry, 8h: did price hit wide_sl (high) before hitting tp (low)?
    end=texec+timedelta(hours=8)
    seg=[c for c in C5[a] if texec<=c[0]<=end]
    hit_wide=None; hit_tp=None
    for c in seg:
        if hit_wide is None and c[2]>=wide_sl: hit_wide=c[0]
        if hit_tp is None and tp and c[3]<=tp: hit_tp=c[0]
        if hit_wide or hit_tp: break
    tot+=1
    survived = (hit_wide is None) or (hit_tp is not None and (hit_wide is None or hit_tp<=hit_wide))
    if survived: surv+=1
    win = hit_tp is not None and (hit_wide is None or hit_tp<=hit_wide)
    if win: would_win+=1
    print(f"  {texec.strftime('%m-%d %H:%M')} {a:7} orig_stop={100*orig_dist/entry:.2f}% wide_stop={100*wide_dist/entry:.2f}%(1.2ATR) origPnL={t.get('pnl'):6.2f} -> wide:{'TP-HIT' if win else ('SURVIVED' if survived else 'STILL-STOPPED')}")
print(f"\n  SUMMARY: {tot} recent stopped shorts | wider(1.2xATR) stop would SURVIVE the bounce: {surv}/{tot} | reach TP1: {would_win}/{tot}")
