"""Follow-up analyses on top of deep_counterfactuals: flags-on restatement,
post-TP2 runner potential, GBP 06-09 churn with true P&L, hour-of-day,
confluence (true P&L), TTL usage, judge candle counts."""
import json, csv, re, statistics as st
from datetime import datetime, timedelta
from collections import defaultdict, Counter

ASSETS = ["BTCUSD", "XAUUSD", "GBPUSD", "USOIL"]
CONTRACT = {"BTCUSD": 1.0, "XAUUSD": 100.0, "GBPUSD": 100000.0, "USOIL": 1000.0}

def load_csv(asset, tf):
    rows = []
    with open(f"data/processed/{asset.lower()}_{tf}_full.csv") as f:
        for r in csv.DictReader(f):
            rows.append((datetime.fromisoformat(r["time"]), float(r["open"]),
                         float(r["high"]), float(r["low"]), float(r["close"])))
    return rows
C5 = {a: load_csv(a, "5m") for a in ASSETS}

def pdt(s):
    if not s: return None
    s = str(s).split("+")[0].replace("Z", "")
    for f in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try: return datetime.strptime(s, f)
        except ValueError: pass
    return None

def norm_side(s):
    s = (s or "").lower()
    return {"long": "buy", "short": "sell"}.get(s, s)

def true_pnl(t):
    a = t.get("asset"); reason = t.get("exit_reason") or ""
    pnl = t.get("pnl") or 0.0
    if a not in CONTRACT: return pnl
    if reason.startswith("MT5_"): return pnl
    entry, exitp, qty = t.get("entry_price"), t.get("exit_price"), t.get("quantity") or 0
    if not (entry and exitp is not None and qty): return pnl
    if abs(exitp - entry) / entry > 0.2: return pnl
    sgn = 1 if norm_side(t.get("side")) == "buy" else -1
    return sgn * (exitp - entry) * qty * CONTRACT[a]

data = json.load(open("total trades.json", encoding="utf-8"))["trades"]
def is_decision(t):
    df = t.get("decision_flow")
    return isinstance(df, dict) and isinstance(df.get("judge"), dict)
def get_md(t):
    fs = (t.get("decision_flow") or {}).get("judge", {}).get("final_signal") or {}
    return fs.get("metadata", {}) if isinstance(fs, dict) else {}
def get_fs(t):
    return (t.get("decision_flow") or {}).get("judge", {}).get("final_signal") or {}
def parse_tp2(t):
    code = get_fs(t).get("code", "")
    m = re.search(r"TP2\s*=\s*([0-9.]+)", code)
    if m:
        try: return float(m.group(1))
        except ValueError: pass
    return None

FLAG_SHIP = datetime(2026, 6, 6, 12, 0, 0)

def summ(name, ts):
    if not ts:
        print(f"  {name:44s} n=0"); return
    p = [true_pnl(t) for t in ts]
    wins = [x for x in p if x > 0]; losses = [x for x in p if x <= 0]
    pf = sum(wins)/abs(sum(losses)) if losses and sum(losses) else float("inf")
    print(f"  {name:44s} n={len(ts):<3d} net={sum(p):8.2f} exp={sum(p)/len(p):6.2f} win%={100*len(wins)/len(p):4.0f} PF={pf:5.2f}")

print("=== FLAGS-ON vs BASELINE, TRUE P&L (all legs incl partials) ===")
def texec(t): return pdt(t.get("trade_executed_at") or t.get("signal_received_at"))
pre  = [t for t in data if texec(t) and texec(t) < FLAG_SHIP]
post = [t for t in data if texec(t) and texec(t) >= FLAG_SHIP]
summ("BASELINE (<06-06 12:00) all legs", pre)
summ("FLAGS-ON (>=06-06 12:00) all legs", post)
summ("BASELINE decision legs", [t for t in pre if is_decision(t)])
summ("FLAGS-ON decision legs", [t for t in post if is_decision(t)])
for a in ASSETS:
    summ(f"  FLAGS-ON {a}", [t for t in post if t.get("asset") == a])
# per-day true pnl
print("\n  per-day true P&L (all legs):")
byday = defaultdict(float)
for t in data:
    tt = texec(t)
    if tt: byday[tt.strftime("%m-%d")] += true_pnl(t)
for d in sorted(byday): print(f"    {d}: {byday[d]:+8.2f}")

print("\n=== CONFLUENCE (true P&L, decision legs incl linked partials not joined — leg-level) ===")
for cs in (4, 5):
    summ(f"confluence={cs}", [t for t in data if is_decision(t) and get_md(t).get("confluence_score") == cs])

print("\n=== TTL USAGE (decision legs) ===")
ttls = Counter()
for t in data:
    if is_decision(t):
        ttl = get_fs(t).get("ttl_minutes")
        ttls[ttl] += 1
print(f"  ttl distribution: {dict(ttls)}")
# time from cycle start to execution (how long strategies waited before trigger)
waits = []
for t in data:
    if not is_decision(t): continue
    st_ = pdt((t.get("decision_flow") or {}).get("started_at"))
    ex = texec(t)
    if st_ and ex:
        waits.append((ex - st_).total_seconds() / 60)
if waits:
    print(f"  cycle-start -> execution wait: n={len(waits)} p25={sorted(waits)[len(waits)//4]:.0f}m "
          f"p50={st.median(waits):.0f}m p75={sorted(waits)[3*len(waits)//4]:.0f}m max={max(waits):.0f}m")
    print(f"  triggered on FIRST monitor run (<=6min): {sum(1 for w in waits if w <= 6)} "
          f"({100*sum(1 for w in waits if w <= 6)/len(waits):.0f}%)")

print("\n=== POST-TP2 RUNNER POTENTIAL (trades that reached TP2: how far beyond within 8h?) ===")
beyond = []
for t in data:
    if not is_decision(t): continue
    a = t.get("asset")
    if a not in ASSETS: continue
    entry, sl = t.get("entry_price"), t.get("stop_loss")
    tp2 = parse_tp2(t)
    topen = texec(t)
    if not (entry and sl and tp2 and topen): continue
    risk = abs(entry - sl)
    if risk <= 0: continue
    side = norm_side(t.get("side")); long_dir = side == "buy"
    horizon = [c for c in C5[a] if topen <= c[0] <= topen + timedelta(hours=8)]
    hit_tp2 = None
    for c in horizon:
        if (long_dir and c[2] >= tp2) or (not long_dir and c[3] <= tp2):
            hit_tp2 = c[0]; break
    if not hit_tp2: continue
    after = [c for c in horizon if c[0] >= hit_tp2]
    if long_dir:
        ext = (max(c[2] for c in after) - tp2) / risk
    else:
        ext = (tp2 - min(c[3] for c in after)) / risk
    beyond.append((t, ext))
if beyond:
    exts = [e for _, e in beyond]
    print(f"  n={len(beyond)} reached TP2; continuation beyond TP2 (R): "
          f"p50={st.median(exts):.2f} p75={sorted(exts)[3*len(exts)//4]:.2f} mean={st.mean(exts):.2f} max={max(exts):.2f}")
    print(f"  >=1R beyond TP2: {sum(1 for e in exts if e >= 1)} ({100*sum(1 for e in exts if e >= 1)/len(exts):.0f}%)"
          f"  >=2R: {sum(1 for e in exts if e >= 2)}")

print("\n=== GBP 2026-06-09 SEQUENCE (true P&L) ===")
gbp = [t for t in data if t.get("asset") == "GBPUSD" and texec(t) and texec(t).strftime("%m-%d") == "06-09"]
tot = 0.0
for t in sorted(gbp, key=lambda x: texec(x)):
    tp = true_pnl(t); tot += tp
    print(f"  {texec(t).strftime('%H:%M')} {norm_side(t.get('side')):4} entry={t.get('entry_price'):<8} "
          f"exit={t.get('exit_price')} reason={str(t.get('exit_reason')):24s} true={tp:+7.2f} dec={is_decision(t)}")
print(f"  GBP 06-09 net: {tot:+.2f}")

print("\n=== HOUR-OF-DAY (UTC) true P&L, decision legs ===")
byh = defaultdict(list)
for t in data:
    if is_decision(t) and texec(t):
        byh[texec(t).hour].append(true_pnl(t))
for h in sorted(byh):
    p = byh[h]
    print(f"  {h:02d}:00  n={len(p):<3d} net={sum(p):+8.2f}")

print("\n=== EXIT REASON true P&L (all legs) ===")
byr = defaultdict(list)
for t in data:
    byr[t.get("exit_reason")].append(true_pnl(t))
for r, p in sorted(byr.items(), key=lambda kv: -abs(sum(kv[1]))):
    print(f"  {str(r):28s} n={len(p):<3d} net={sum(p):+9.2f} exp={sum(p)/len(p):+7.2f}")
