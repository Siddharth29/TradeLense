"""Deep per-trade counterfactual analysis of `total trades.json`.

For every decision trade:
  - link orphan partial legs to parent (asset+side+entry+time match)
  - regime label at entry (1h EMA20/50 trend, 1h ATR%, 15m range-ness)
  - MAE/MFE during the trade (5m data)
  - post-exit excursion in trade direction (2h/4h/8h, in R)
  - counterfactual stops: max(orig, k*ATR1h) for k in (1.0, 1.2, 1.5)
  - TP1->TP2 continuation: after TP1 touch, did price reach TP2 before BE+0.5R?
  - no-partial counterfactual P&L
Writes data/processed/deep_counterfactuals.csv + prints aggregate report.
Read-only w.r.t. trading system.
"""
import json, csv, math, re, statistics as st
from datetime import datetime, timedelta
from collections import defaultdict

ASSETS = ["BTCUSD", "XAUUSD", "GBPUSD", "USOIL"]

def load_csv(asset, tf):
    rows = []
    with open(f"data/processed/{asset.lower()}_{tf}_full.csv") as f:
        for r in csv.DictReader(f):
            rows.append((datetime.fromisoformat(r["time"]), float(r["open"]),
                         float(r["high"]), float(r["low"]), float(r["close"])))
    return rows

C5 = {a: load_csv(a, "5m") for a in ASSETS}
C15 = {a: load_csv(a, "15m") for a in ASSETS}
C1H = {a: load_csv(a, "1h") for a in ASSETS}

def pdt(s):
    if not s: return None
    s = str(s).split("+")[0].replace("Z", "")
    for f in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try: return datetime.strptime(s, f)
        except ValueError: pass
    return None

def seg(c, t0, t1):
    return [x for x in c if t0 <= x[0] <= t1]

def atr_at(c, t, period=14):
    bars = [x for x in c if x[0] <= t]
    if len(bars) < period + 1: return None
    trs = []
    for i in range(len(bars) - period, len(bars)):
        h, l, pc = bars[i][2], bars[i][3], bars[i-1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs)

def ema(vals, p):
    k = 2 / (p + 1); e = vals[0]
    for v in vals[1:]: e = v * k + e * (1 - k)
    return e

def trend_at(c, t, fast=20, slow=50, min_sep_pct=0.05):
    closes = [x[4] for x in c if x[0] <= t]
    if len(closes) < slow: return "na", 0.0
    ef, es = ema(closes[-3*slow:], fast), ema(closes[-3*slow:], slow)
    sep = (ef / es - 1) * 100
    if abs(sep) < min_sep_pct: return "range", sep
    return ("up" if sep > 0 else "down"), sep

def chop_at(c1h, t, n=24):
    """Efficiency ratio over last n 1h bars: |net|/sum|moves|. <0.25 = chop."""
    closes = [x[4] for x in c1h if x[0] <= t][-n-1:]
    if len(closes) < n + 1: return None
    net = abs(closes[-1] - closes[0])
    path = sum(abs(closes[i+1] - closes[i]) for i in range(len(closes)-1))
    return net / path if path > 0 else 0.0

data = json.load(open("total trades.json", encoding="utf-8"))["trades"]

CONTRACT = {"BTCUSD": 1.0, "XAUUSD": 100.0, "GBPUSD": 100000.0, "USOIL": 1000.0}

def norm_side(s):
    s = (s or "").lower()
    return {"long": "buy", "short": "sell"}.get(s, s)

def true_pnl(t):
    """Recorded pnl for MT5-native exits (deal-true USD); recompute price*qty*contract
    for monitor-computed exits (TP1_PARTIAL/TP2/SL/TIMEOUT/PRE_CLOSE) which ignored
    contract size. Corrupt exit prices (|exit-entry|/entry>0.2) -> keep recorded."""
    a = t.get("asset"); reason = t.get("exit_reason") or ""
    pnl = t.get("pnl") or 0.0
    if a not in CONTRACT: return pnl, False
    if reason.startswith("MT5_"): return pnl, False
    entry, exitp, qty = t.get("entry_price"), t.get("exit_price"), t.get("quantity") or 0
    if not (entry and exitp is not None and qty): return pnl, False
    if abs(exitp - entry) / entry > 0.2: return pnl, False  # corrupt exit px
    sgn = 1 if norm_side(t.get("side")) == "buy" else -1
    return sgn * (exitp - entry) * qty * CONTRACT[a], True

def is_decision(t):
    df = t.get("decision_flow")
    return isinstance(df, dict) and isinstance(df.get("judge"), dict)

def get_md(t):
    fs = (t.get("decision_flow") or {}).get("judge", {}).get("final_signal") or {}
    return fs.get("metadata", {}) if isinstance(fs, dict) else {}

def get_code(t):
    fs = (t.get("decision_flow") or {}).get("judge", {}).get("final_signal") or {}
    return fs.get("code", "") if isinstance(fs, dict) else ""

def parse_tp2(t):
    code = get_code(t)
    m = re.search(r"TP2\s*=\s*([0-9.]+)", code)
    if m:
        try: return float(m.group(1))
        except ValueError: pass
    pat = t.get("pattern") or ""
    m = re.search(r"TP2[=\s][^0-9]*([0-9][0-9,.]*)", pat)
    if m:
        try: return float(m.group(1).replace(",", ""))
        except ValueError: pass
    return None

# ---------- link orphan partials to parent decisions ----------
decisions = [t for t in data if is_decision(t)]
orphans = [t for t in data if not is_decision(t) and t.get("asset") in ASSETS]
linked = defaultdict(list)   # parent id -> [orphan,...]
orphan_used = set()
for o in orphans:
    o_t = pdt(o.get("trade_executed_at") or o.get("signal_received_at"))
    best, best_dt = None, None
    for d in decisions:
        if d.get("asset") != o.get("asset"): continue
        if norm_side(d.get("side")) != norm_side(o.get("side")): continue
        de, oe = d.get("entry_price"), o.get("entry_price")
        if de and oe and abs(de - oe) / de > 0.001: continue
        d_open = pdt(d.get("trade_executed_at"))
        d_close = pdt(d.get("trade_closed_at"))
        if not (d_open and o_t): continue
        if not (d_open - timedelta(minutes=2) <= o_t <= (d_close or d_open + timedelta(hours=5)) + timedelta(minutes=10)):
            continue
        dt = abs((o_t - d_open).total_seconds())
        if best is None or dt < best_dt:
            best, best_dt = d, dt
    if best is not None:
        linked[best["id"]].append(o)
        orphan_used.add(o["id"])

unlinked = [o for o in orphans if o["id"] not in orphan_used]
print(f"orphan partial legs: {len(orphans)} | linked to a parent decision: {len(orphan_used)} | UNLINKED: {len(unlinked)}")
for o in unlinked:
    tp, fixed = true_pnl(o)
    print(f"  UNLINKED {o.get('asset'):7} {o.get('side'):5} entry={o.get('entry_price')} exit={o.get('exit_price')} "
          f"pnl={o.get('pnl'):.4g} truePnl={tp:.2f} reason={o.get('exit_reason')} t={o.get('trade_executed_at')}")

# ---------- whole-file true P&L restatement ----------
tot_rec = sum(t.get("pnl") or 0 for t in data)
tot_true = 0.0; n_fixed = 0
for t in data:
    tp, fixed = true_pnl(t)
    tot_true += tp; n_fixed += int(fixed)
print(f"\n=== P&L RESTATEMENT (contract-size fix on monitor-computed exits) ===")
print(f"  recorded total: {tot_rec:+.2f}   TRUE total: {tot_true:+.2f}   legs recomputed: {n_fixed}")
for a in ["BTCUSD", "XAUUSD", "GBPUSD", "USOIL"]:
    rec = sum(t.get("pnl") or 0 for t in data if t.get("asset") == a)
    tru = sum(true_pnl(t)[0] for t in data if t.get("asset") == a)
    print(f"    {a:7s} recorded {rec:+8.2f}  true {tru:+8.2f}")
par = [t for t in data if t.get("exit_reason") == "TP1_PARTIAL"]
print(f"  TP1_PARTIAL legs: recorded {sum(t.get('pnl') or 0 for t in par):+.2f}  true {sum(true_pnl(t)[0] for t in par):+.2f}")

# ---------- per-trade deep dive ----------
rows = []
sanity_fail = 0
for t in decisions:
    a = t.get("asset")
    if a not in ASSETS: continue
    side = (t.get("side") or "").lower()
    long_dir = side in ("buy", "long")
    entry, exitp = t.get("entry_price"), t.get("exit_price")
    sl, tp1 = t.get("stop_loss"), t.get("take_profit")
    tp2 = parse_tp2(t)
    qty = t.get("quantity") or 0
    pnl, _ = true_pnl(t)
    # corrupt exit price -> don't trust exitp for excursion math
    if exitp is not None and entry and abs(exitp - entry) / entry > 0.2:
        exitp = None
    topen = pdt(t.get("trade_executed_at") or t.get("signal_received_at"))
    tclose = pdt(t.get("trade_closed_at"))
    if not (entry and sl and topen): continue
    risk = abs(entry - sl)
    if risk <= 0: continue

    # sanity: entry price inside the entry 5m bar (+/- 1 bar)
    bar = [c for c in C5[a] if abs((c[0] - topen).total_seconds()) <= 600]
    ok_px = any(c[3] - risk*0.5 <= entry <= c[2] + risk*0.5 for c in bar) if bar else None
    if ok_px is False: sanity_fail += 1

    # regime at entry
    tr1h, sep1h = trend_at(C1H[a], topen)
    atr1h = atr_at(C1H[a], topen)
    atr5 = atr_at(C5[a], topen)
    er = chop_at(C1H[a], topen)          # efficiency ratio (1h, 24 bars)
    atr1h_pct = 100 * atr1h / entry if atr1h else None
    sl_atr1h = risk / atr1h if atr1h else None    # stop distance in 1h-ATR units
    sl_atr5 = risk / atr5 if atr5 else None

    # in-trade MAE/MFE (5m), from open to close
    end = tclose or (topen + timedelta(hours=4))
    s5 = seg(C5[a], topen, end)
    mae = mfe = None
    if s5:
        if long_dir:
            mae = (entry - min(c[3] for c in s5)) / risk
            mfe = (max(c[2] for c in s5) - entry) / risk
        else:
            mae = (max(c[2] for c in s5) - entry) / risk
            mfe = (entry - min(c[3] for c in s5)) / risk

    # post-exit excursion in trade direction (R), horizons 2/4/8h
    post = {}
    for hh in (2, 4, 8):
        e2 = seg(C5[a], end, end + timedelta(hours=hh))
        if e2:
            if long_dir:
                post[hh] = (max(c[2] for c in e2) - (exitp or entry)) / risk
            else:
                post[hh] = ((exitp or entry) - min(c[3] for c in e2)) / risk
        else:
            post[hh] = None

    # counterfactual stops (walk forward 8h from entry): wider stop = max(orig, k*ATR1h)
    cf = {}
    for k in (1.0, 1.2, 1.5):
        if not atr1h: cf[k] = None; continue
        wd = max(risk, k * atr1h)
        wsl = entry - wd if long_dir else entry + wd
        horizon = seg(C5[a], topen, topen + timedelta(hours=8))
        hit_sl_t = hit_tp1_t = None
        for c in horizon:
            if hit_sl_t is None and ((long_dir and c[3] <= wsl) or (not long_dir and c[2] >= wsl)):
                hit_sl_t = c[0]
            if tp1 and hit_tp1_t is None and ((long_dir and c[2] >= tp1) or (not long_dir and c[3] <= tp1)):
                hit_tp1_t = c[0]
            if hit_sl_t and hit_tp1_t: break
        if hit_tp1_t and (not hit_sl_t or hit_tp1_t <= hit_sl_t):
            cf[k] = "TP1"
        elif hit_sl_t:
            cf[k] = "SL"
        else:
            cf[k] = "OPEN"

    # TP1->TP2 continuation: from first TP1 touch, which came first: TP2 or BE+0.5R retrace?
    tp1_first = None; tp2_after = None
    horizon = seg(C5[a], topen, topen + timedelta(hours=8))
    for c in horizon:
        if tp1 and ((long_dir and c[2] >= tp1) or (not long_dir and c[3] <= tp1)):
            tp1_first = c[0]; break
    if tp1_first and tp2:
        be05 = entry + 0.5 * risk if long_dir else entry - 0.5 * risk
        t_tp2 = t_be = None
        for c in [x for x in horizon if x[0] >= tp1_first]:
            if t_tp2 is None and ((long_dir and c[2] >= tp2) or (not long_dir and c[3] <= tp2)):
                t_tp2 = c[0]
            if t_be is None and ((long_dir and c[3] <= be05) or (not long_dir and c[2] >= be05)):
                t_be = c[0]
            if t_tp2 and t_be: break
        if t_tp2 and (not t_be or t_tp2 <= t_be): tp2_after = "TP2"
        elif t_be: tp2_after = "BE_RETRACE"
        else: tp2_after = "NEITHER"

    # actual total P&L for this decision incl. linked partial legs (true USD)
    kids = linked.get(t["id"], [])
    pnl_kids = sum(true_pnl(k)[0] for k in kids)
    pnl_total = pnl + pnl_kids

    # dollars per 1R for this trade (from its own realized data where possible)
    usd_per_R = None
    if exitp is not None and risk:
        move = (exitp - entry) if long_dir else (entry - exitp)
        if abs(move) > 1e-12 and pnl:
            usd_per_R = abs(pnl / (move / risk))

    rows.append(dict(
        id=t["id"], asset=a, side=side, entry=entry, exit=exitp, sl=sl, tp1=tp1, tp2=tp2,
        qty=qty, pnl=pnl, pnl_with_partials=pnl_total, n_partial_legs=len(kids),
        exit_reason=t.get("exit_reason"), topen=topen, tclose=tclose,
        dur_min=((tclose - topen).total_seconds()/60 if tclose else None),
        trend_1h=tr1h, sep_1h=round(sep1h, 3), er_1h=(round(er, 3) if er is not None else None),
        atr1h_pct=(round(atr1h_pct, 3) if atr1h_pct else None),
        sl_pct=round(100*risk/entry, 3),
        sl_in_atr1h=(round(sl_atr1h, 2) if sl_atr1h else None),
        sl_in_atr5m=(round(sl_atr5, 2) if sl_atr5 else None),
        mae_R=(round(mae, 2) if mae is not None else None),
        mfe_R=(round(mfe, 2) if mfe is not None else None),
        post2h_R=(round(post[2], 2) if post[2] is not None else None),
        post4h_R=(round(post[4], 2) if post[4] is not None else None),
        post8h_R=(round(post[8], 2) if post[8] is not None else None),
        cf_stop_1_0=cf.get(1.0), cf_stop_1_2=cf.get(1.2), cf_stop_1_5=cf.get(1.5),
        tp1_touched=bool(tp1_first), after_tp1=tp2_after,
        usd_per_R=(round(usd_per_R, 2) if usd_per_R else None),
        confluence=get_md(t).get("confluence_score"),
        strategy_type=get_md(t).get("strategy_type"),
        entry_px_sane=ok_px,
    ))

print(f"\ndecision trades analyzed: {len(rows)} | entry-price sanity failures: {sanity_fail}")

with open("data/processed/deep_counterfactuals.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
    w.writeheader()
    for r in rows: w.writerow(r)
print("[wrote data/processed/deep_counterfactuals.csv]")

# ================= aggregates =================
def agg(name, rs, key="pnl_with_partials"):
    if not rs:
        print(f"  {name:42s} n=0"); return
    p = [r[key] for r in rs]
    wins = [x for x in p if x > 0]; losses = [x for x in p if x <= 0]
    pf = sum(wins)/abs(sum(losses)) if losses and sum(losses) else float("inf")
    print(f"  {name:42s} n={len(rs):<3d} net={sum(p):8.2f} exp={sum(p)/len(p):6.2f} win%={100*len(wins)/len(p):4.0f} PF={pf:5.2f}")

stopped = [r for r in rows if r["exit_reason"] in ("MT5_REASON_4", "SL") and (r["pnl"] or 0) < 0]
print("\n=== STOPPED LOSING TRADES: was the thesis right anyway? (post-exit excursion in R) ===")
for hh in (2, 4, 8):
    k = f"post{hh}h_R"
    vals = [r[k] for r in stopped if r[k] is not None]
    if vals:
        ge1 = sum(1 for v in vals if v >= 1); ge2 = sum(1 for v in vals if v >= 2)
        print(f"  horizon {hh}h: n={len(vals)}  median={st.median(vals):5.2f}R  mean={st.mean(vals):5.2f}R  "
              f">=1R: {ge1} ({100*ge1/len(vals):.0f}%)  >=2R: {ge2} ({100*ge2/len(vals):.0f}%)")

print("\n=== COUNTERFACTUAL STOPS on stopped losers (would max(orig, k*ATR1h) have saved it?) ===")
for kk, key in ((1.0, "cf_stop_1_0"), (1.2, "cf_stop_1_2"), (1.5, "cf_stop_1_5")):
    sub = [r[key] for r in stopped if r[key]]
    if sub:
        from collections import Counter
        c = Counter(sub)
        print(f"  k={kk}: TP1-first {c.get('TP1',0)}/{len(sub)}  still-stopped {c.get('SL',0)}  open@8h {c.get('OPEN',0)}")

print("\n=== STOP SIZE IN VOLATILITY UNITS (all decisions) ===")
for a in ASSETS:
    sub = [r for r in rows if r["asset"] == a and r["sl_in_atr1h"]]
    if sub:
        v = [r["sl_in_atr1h"] for r in sub]
        v5 = [r["sl_in_atr5m"] for r in sub if r["sl_in_atr5m"]]
        print(f"  {a:7s} stop/ATR1h: p25={sorted(v)[len(v)//4]:.2f} p50={st.median(v):.2f} p75={sorted(v)[3*len(v)//4]:.2f}"
              f"   stop/ATR5m: p50={st.median(v5):.2f}")

print("\n=== MAE of WINNING trades (how much heat do winners take?) ===")
winners = [r for r in rows if (r["pnl_with_partials"] or 0) > 0 and r["mae_R"] is not None]
if winners:
    v = [r["mae_R"] for r in winners]
    print(f"  n={len(winners)} MAE_R p50={st.median(v):.2f} p75={sorted(v)[3*len(v)//4]:.2f} p90={sorted(v)[int(0.9*len(v))-1]:.2f}")
losers = [r for r in rows if (r["pnl_with_partials"] or 0) <= 0 and r["mfe_R"] is not None]
if losers:
    v = [r["mfe_R"] for r in losers]
    ge05 = sum(1 for x in v if x >= 0.5); ge1 = sum(1 for x in v if x >= 1.0)
    print(f"  losers n={len(losers)} MFE_R p50={st.median(v):.2f}  reached>=+0.5R first: {ge05} ({100*ge05/len(v):.0f}%)  >=+1R: {ge1}")

print("\n=== AFTER TP1 TOUCH: TP2 vs BE-retrace (validates/invalidates 50% booking) ===")
sub = [r for r in rows if r["after_tp1"]]
from collections import Counter
c = Counter(r["after_tp1"] for r in sub)
print(f"  TP1 touched n={len(sub)}: reached TP2 first: {c.get('TP2',0)}  retraced to BE+0.5R first: {c.get('BE_RETRACE',0)}  neither(8h): {c.get('NEITHER',0)}")

print("\n=== REGIME SPLIT (efficiency ratio at entry; ER<0.25 = chop) ===")
chop = [r for r in rows if r["er_1h"] is not None and r["er_1h"] < 0.25]
trend = [r for r in rows if r["er_1h"] is not None and r["er_1h"] >= 0.25]
agg("CHOP entries (ER<0.25)", chop)
agg("TREND entries (ER>=0.25)", trend)
for a in ASSETS:
    agg(f"  {a} chop", [r for r in chop if r["asset"] == a])
    agg(f"  {a} trend", [r for r in trend if r["asset"] == a])

print("\n=== HTF ALIGNMENT (1h EMA trend vs side) — with-partials P&L ===")
aligned = [r for r in rows if (r["trend_1h"] == "up" and r["side"] == "buy") or (r["trend_1h"] == "down" and r["side"] == "sell")]
counter = [r for r in rows if (r["trend_1h"] == "down" and r["side"] == "buy") or (r["trend_1h"] == "up" and r["side"] == "sell")]
ranged = [r for r in rows if r["trend_1h"] == "range"]
agg("ALIGNED with 1h trend", aligned)
agg("COUNTER 1h trend", counter)
agg("1h RANGE at entry", ranged)

print("\n=== ALIGNMENT x REGIME cross-split ===")
agg("aligned + trend(ER>=0.25)", [r for r in aligned if r["er_1h"] is not None and r["er_1h"] >= 0.25])
agg("aligned + chop(ER<0.25)", [r for r in aligned if r["er_1h"] is not None and r["er_1h"] < 0.25])
agg("counter + trend(ER>=0.25)", [r for r in counter if r["er_1h"] is not None and r["er_1h"] >= 0.25])
agg("counter + chop(ER<0.25)", [r for r in counter if r["er_1h"] is not None and r["er_1h"] < 0.25])
agg("range-1h entries", ranged)

print("\n=== BUY vs SELL (true P&L incl partials) ===")
agg("buys", [r for r in rows if r["side"] == "buy"])
agg("sells", [r for r in rows if r["side"] == "sell"])

print("\n=== RE-ENTRY CHURN: same asset+side entry within 90min of a stop-out ===")
by_asset = defaultdict(list)
for r in sorted(rows, key=lambda x: x["topen"]):
    by_asset[r["asset"]].append(r)
churn = []
for a, lst in by_asset.items():
    for i, r in enumerate(lst):
        for q in lst[:i]:
            if q["tclose"] and q["exit_reason"] in ("MT5_REASON_4", "SL") and (q["pnl"] or 0) < 0 \
               and q["side"] == r["side"] and timedelta(0) <= r["topen"] - q["tclose"] <= timedelta(minutes=90):
                churn.append(r); break
agg("re-entries <=90min after same-dir stop", churn)
ch_by_a = Counter((r["asset"]) for r in churn)
print(f"   by asset: {dict(ch_by_a)}")

print("\n=== PARTIAL-LEG ECONOMICS (linked) ===")
n_par = sum(len(v) for v in linked.values())
par_pnl = sum(k.get("pnl") or 0 for v in linked.values() for k in v)
print(f"  decisions with >=1 partial leg: {sum(1 for v in linked.values() if v)} | partial legs: {n_par} | partial P&L: {par_pnl:+.2f}")
# decisions where TP1 partial happened, what did the parent leg (residual) do?
par_parents = [r for r in rows if r["n_partial_legs"] > 0]
agg("parents WITH partial (residual final leg)", par_parents, key="pnl")
agg("parents WITH partial (total incl partial)", par_parents, key="pnl_with_partials")
