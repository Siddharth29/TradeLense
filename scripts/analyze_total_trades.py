"""
Deep analysis of `total trades.json` (root).
Read-only. Produces a per-decision CSV + prints a structured report to stdout.
Run: python scripts/analyze_total_trades.py
"""
import json, csv, re, statistics as st
from collections import Counter, defaultdict
from datetime import datetime

ROOT = "."
FLAG_SHIP = datetime(2026, 6, 6, 12, 0, 0)  # HTF/soft-counter-trend flags went live ~Jun-6

def parse_dt(s):
    if not s: return None
    s = s.replace("Z", "")
    if "+" in s: s = s.split("+")[0]
    s = s.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try: return datetime.strptime(s, fmt)
        except: pass
    return None

data = json.load(open("total trades.json", "r", encoding="utf-8"))
trades = data["trades"]

def is_decision(t):
    df = t.get("decision_flow")
    return isinstance(df, dict) and isinstance(df.get("judge"), dict)

def get_md(t):
    if not is_decision(t): return {}
    fs = t["decision_flow"]["judge"].get("final_signal") or {}
    return fs.get("metadata", {}) if isinstance(fs, dict) else {}

def rationale(t):
    md = get_md(t)
    r = md.get("rationale", "") or ""
    # also judge-level rationale + pattern
    return (r + " " + (t.get("pattern") or "")).lower()

rows = []
for t in trades:
    side = (t.get("side") or "").lower()
    entry = t.get("entry_price"); exit_ = t.get("exit_price")
    sl = t.get("stop_loss"); tp = t.get("take_profit")
    pnl = t.get("pnl") or 0.0
    dec = is_decision(t)
    md = get_md(t)
    texec = parse_dt(t.get("trade_executed_at") or t.get("signal_received_at"))
    tclose = parse_dt(t.get("trade_closed_at"))
    dur_min = (tclose - texec).total_seconds()/60 if (texec and tclose) else None
    # direction sign: buy/long = +1, sell/short = -1
    long_dir = side in ("buy", "long")
    # realized R = (exit-entry)/(entry-sl) for long; mirror for short
    realR = None; planRR = None; sl_pct = None; tp_pct = None
    try:
        if entry and sl and abs(entry - sl) > 0:
            risk = abs(entry - sl)
            if long_dir:
                realR = (exit_ - entry)/risk if exit_ is not None else None
                planRR = (tp - entry)/risk if tp else None
            else:
                realR = (entry - exit_)/risk if exit_ is not None else None
                planRR = (entry - tp)/risk if tp else None
            sl_pct = 100*risk/entry
        if entry and tp:
            tp_pct = 100*abs(tp-entry)/entry
    except: pass
    # HTF alignment from rationale text
    rt = rationale(t)
    htf_down = bool(re.search(r"htf[^.]*?(down|bear)", rt)) or "4h/1d down" in rt or "4h+1d bearish" in rt or "4h/1d bearish" in rt
    htf_up = bool(re.search(r"htf[^.]*?(up|bull)", rt)) or "4h/1d up" in rt or "4h+1d bullish" in rt
    htf_dir = "down" if htf_down and not htf_up else ("up" if htf_up and not htf_down else ("conflict" if htf_up and htf_down else None))
    counter = None
    if htf_dir == "down": counter = long_dir          # long in downtrend = counter
    elif htf_dir == "up": counter = (not long_dir)     # short in uptrend = counter
    rows.append(dict(
        id=t.get("id"), asset=t.get("asset"), side=side, decision=dec,
        entry=entry, exit=exit_, sl=sl, tp=tp, pnl=pnl, qty=t.get("quantity"),
        status=t.get("trade_status"), exit_reason=t.get("exit_reason"),
        strategy_type=md.get("strategy_type"), decision_kind=md.get("decision"),
        confluence=md.get("confluence_score"), selected_proposal=md.get("selected_proposal"),
        realR=realR, planRR=planRR, sl_pct=sl_pct, tp_pct=tp_pct, dur_min=dur_min,
        texec=texec, htf_dir=htf_dir, counter=counter,
        flags_on=(texec >= FLAG_SHIP) if texec else None,
        gpt_response_id=t.get("gpt_response_id"), cycle_id=(t.get("decision_flow") or {}).get("cycle_id") if dec else None,
    ))

def fmt(x, n=2):
    return ("%."+str(n)+"f")%x if isinstance(x,(int,float)) else str(x)

def summ(name, rs):
    if not rs:
        print("  %-32s n=0"%name); return
    pnls=[r["pnl"] for r in rs]
    wins=[p for p in pnls if p>0]; losses=[p for p in pnls if p<=0]
    net=sum(pnls)
    wr=100*len(wins)/len(rs)
    aw=st.mean(wins) if wins else 0; al=st.mean(losses) if losses else 0
    pf=(sum(wins)/abs(sum(losses))) if losses and sum(losses)!=0 else float('inf')
    print("  %-32s n=%-3d net=%-9s win%%=%-5s PF=%-6s aw=%-7s al=%-7s exp=%s"%(
        name, len(rs), fmt(net), fmt(wr,1), fmt(pf), fmt(aw), fmt(al), fmt(net/len(rs))))

print("="*100)
print("OVERALL  (n=%d trades, %d decisions, %d partial/orphan)"%(len(rows),
      sum(1 for r in rows if r["decision"]), sum(1 for r in rows if not r["decision"])))
print("="*100)
summ("ALL TRADES", rows)
summ("DECISION legs only", [r for r in rows if r["decision"]])
summ("PARTIAL/ORPHAN legs only", [r for r in rows if not r["decision"]])
print()
print("--- BY ASSET (all legs) ---")
for a in sorted(set(r["asset"] for r in rows)):
    summ(a, [r for r in rows if r["asset"]==a])
print()
print("--- BY ASSET (decision legs only) ---")
for a in sorted(set(r["asset"] for r in rows)):
    summ(a+" [dec]", [r for r in rows if r["asset"]==a and r["decision"]])
print()
print("--- DECISION legs BY SIDE ---")
for s in ["buy","sell","long","short"]:
    summ(s, [r for r in rows if r["decision"] and r["side"]==s])
print()
print("--- EXIT REASON (decision legs) ---")
er=Counter(r["exit_reason"] for r in rows if r["decision"])
for k,c in er.most_common():
    sub=[r for r in rows if r["decision"] and r["exit_reason"]==k]
    summ(str(k), sub)
print()
print("--- EXIT REASON (partial/orphan legs) ---")
er=Counter(r["exit_reason"] for r in rows if not r["decision"])
for k,c in er.most_common():
    sub=[r for r in rows if not r["decision"] and r["exit_reason"]==k]
    summ(str(k), sub)
print()
print("--- BASELINE (pre Jun-6 12:00) vs FLAGS-ON (post) : decision legs ---")
summ("BASELINE decisions", [r for r in rows if r["decision"] and r["flags_on"]==False])
summ("FLAGS-ON decisions", [r for r in rows if r["decision"] and r["flags_on"]==True])
print("   (all legs incl partials)")
summ("BASELINE all", [r for r in rows if r["flags_on"]==False])
summ("FLAGS-ON all", [r for r in rows if r["flags_on"]==True])
print()
print("--- HTF ALIGNMENT (decision legs, parsed from rationale) ---")
for c in [True, False, None]:
    lab={True:"COUNTER-trend", False:"ALIGNED", None:"unknown/none"}[c]
    summ(lab, [r for r in rows if r["decision"] and r["counter"]==c])
htf_known=[r for r in rows if r["decision"] and r["htf_dir"]]
print("   decisions with parseable HTF dir: %d / %d"%(len(htf_known), sum(1 for r in rows if r['decision'])))
print()
print("--- CONFLUENCE SCORE vs outcome (decision legs) ---")
for cs in sorted(set(r["confluence"] for r in rows if r["decision"] and r["confluence"] is not None)):
    summ("confluence=%s"%cs, [r for r in rows if r["decision"] and r["confluence"]==cs])
print()
print("--- STRATEGY TYPE vs outcome (decision legs) ---")
for stp in sorted(set(str(r["strategy_type"]) for r in rows if r["decision"])):
    summ("strat=%s"%stp, [r for r in rows if r["decision"] and str(r["strategy_type"])==stp])
print()
print("--- SL distance %% distribution (decision legs, by asset) ---")
for a in sorted(set(r["asset"] for r in rows if r["decision"])):
    sls=[r["sl_pct"] for r in rows if r["decision"] and r["asset"]==a and r["sl_pct"]]
    if sls:
        print("  %-10s n=%-3d slpct: min=%.3f p50=%.3f p90=%.3f max=%.3f"%(
            a, len(sls), min(sls), st.median(sls), sorted(sls)[int(0.9*len(sls))-1], max(sls)))
print()
print("--- PLANNED RR distribution (decision legs) ---")
rr=[r["planRR"] for r in rows if r["decision"] and r["planRR"]]
if rr:
    print("  n=%d  min=%.2f p25=%.2f p50=%.2f p75=%.2f max=%.2f  <1.5: %d"%(
        len(rr), min(rr), sorted(rr)[len(rr)//4], st.median(rr), sorted(rr)[3*len(rr)//4], max(rr),
        sum(1 for x in rr if x<1.5)))
print()
print("--- DURATION (minutes) decision legs by exit reason ---")
for k in ["MT5_REASON_4","MT5_REASON_5","TP1_PARTIAL","TP2","SL","MT5_TP"]:
    ds=[r["dur_min"] for r in rows if r["decision"] and r["exit_reason"]==k and r["dur_min"] is not None]
    if ds:
        print("  %-14s n=%-3d  p50=%.0fm p90=%.0fm max=%.0fm"%(k,len(ds),st.median(ds),sorted(ds)[int(0.9*len(ds))-1],max(ds)))
print()
print("--- REALIZED R distribution (decision legs) ---")
Rs=[r["realR"] for r in rows if r["decision"] and r["realR"] is not None]
if Rs:
    print("  n=%d mean=%.3f p10=%.2f p50=%.2f p90=%.2f"%(len(Rs),st.mean(Rs),
        sorted(Rs)[int(0.1*len(Rs))], st.median(Rs), sorted(Rs)[int(0.9*len(Rs))-1]))
    print("  R<=-0.9 (full stop): %d (%.0f%%)   R>=+0.9: %d (%.0f%%)   -0.9<R<+0.9 (managed/partial exit): %d (%.0f%%)"%(
        sum(1 for x in Rs if x<=-0.9), 100*sum(1 for x in Rs if x<=-0.9)/len(Rs),
        sum(1 for x in Rs if x>=0.9), 100*sum(1 for x in Rs if x>=0.9)/len(Rs),
        sum(1 for x in Rs if -0.9<x<0.9), 100*sum(1 for x in Rs if -0.9<x<0.9)/len(Rs)))

# write CSV
with open("data/processed/total_trades_decisions.csv","w",newline="",encoding="utf-8") as f:
    w=csv.DictWriter(f, fieldnames=[k for k in rows[0].keys() if k!="texec"])
    w.writeheader()
    for r in rows:
        rr={k:v for k,v in r.items() if k!="texec"}
        w.writerow(rr)
print("\n[wrote data/processed/total_trades_decisions.csv]")
