"""Per-trade P&L + R-multiple extractor from the NDJSON engine log.

Stitches each trade from entry -> trails/partials -> exit by (asset, chronological
window), since only one position per asset is open at a time. Outputs a per-trade
CSV plus an aggregate metrics summary.
"""
import json, re, csv, sys
from collections import defaultdict
from datetime import datetime
from statistics import median, mean

LOG = "logs (15).json"
OUT = "data/processed/per_trade_pnl.csv"

def pt(s):
    try: return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception: return None

TRAIL_RE = re.compile(r"old_sl=([0-9.]+)\s*->\s*new_sl=([0-9.]+)")
ASSET_RE = re.compile(r"\bfor ([A-Z]{3,6})\b")  # parse asset from msgs like "Trailing SL for BTCUSD:"

# ---- pass 1: collect all event rows we care about, in order -----------------
rows = []
with open(LOG, "r", encoding="utf-8", errors="replace") as f:
    for line in f:
        line = line.strip()
        if not line: continue
        try: o = json.loads(line)
        except Exception: continue
        rows.append(o)
rows.sort(key=lambda o: o.get("time", ""))

def asset_of(o):
    a = o.get("asset") or (o.get("ctx", {}) or {}).get("asset") or (o.get("ctx", {}) or {}).get("symbol") or o.get("symbol")
    if a:
        return a
    m = ASSET_RE.search(o.get("msg", ""))  # fallback: parse from message text
    return m.group(1) if m else None

def sym_norm(s):
    return (s or "").replace("m", "") if s and s.endswith("m") else s

# ---- pass 2: build trades ----------------------------------------------------
# NOTE: "Position size calculated" and "TP2 runner active" are logged ~110ms
# BEFORE "Code sniper trade executed", so buffer them per-asset and attach on entry.
open_trade = {}     # asset -> trade dict
pending = defaultdict(dict)  # asset -> buffered pre-entry sizing
trades = []

def new_trade(o):
    a = o.get("asset")
    p = pending.pop(a, {})
    t = {
        "asset": a, "order_id": o.get("order_id"), "side": o.get("side"),
        "entry_time": pt(o.get("time")), "entry_price": float(o.get("entry")),
        "quantity": o.get("quantity"), "cycle_id": o.get("cycle_id"),
        "initial_sl": p.get("initial_sl"), "risk_points": p.get("risk_points"),
        "risk_pct": p.get("risk_pct"), "funds": p.get("funds"),
        "tp1": p.get("tp1"), "tp2": p.get("tp2"),
        "trail_count": 0, "trail_rejected": 0, "final_sl": None, "trail_steps": [],
        "tp1_partial": 0, "tp2_partial": 0, "tp1_fill": None,
        "exit_time": None, "exit_price": None, "exit_reason": None,
        "candles_held": None,
        "final_pnl": None, "partial_pnls": [],
    }
    open_trade[a] = t
    trades.append(t)
    return t

for o in rows:
    msg = o.get("msg", ""); ctx = o.get("ctx", {}) or {}
    a = asset_of(o)
    base = sym_norm(a)

    # buffer pre-entry sizing (fires before the execute event)
    if msg.startswith("Position size calculated"):
        sp = ctx.get("sl_points")
        if sp is not None:
            ep = float(ctx.get("entry_price"))
            sign = 1 if (ctx.get("side", "") or "").lower() == "buy" else None
            # side not in this record; store distance + entry, resolve sign at entry
            pending[base]["risk_points"] = float(sp)
            pending[base]["_size_entry"] = ep
        pending[base]["risk_pct"] = ctx.get("risk_pct")
        pending[base]["funds"] = ctx.get("available_funds")
        continue
    if msg.startswith("TP2 runner active"):
        pending[base]["tp1"] = ctx.get("tp1"); pending[base]["tp2"] = ctx.get("managed_tp2")
        continue

    if msg.startswith("Code sniper trade executed"):
        t = new_trade(o)
        # resolve initial_sl now that we know the side
        if t["risk_points"] and t["initial_sl"] is None:
            sign = 1 if (t["side"] or "").lower() == "buy" else -1
            t["initial_sl"] = t["entry_price"] - sign * t["risk_points"]
        continue

    cur = open_trade.get(base)
    if cur is None:
        continue

    if msg.startswith("Trailing SL"):
        m = TRAIL_RE.search(msg)
        if m:
            cur["trail_count"] += 1
            cur["final_sl"] = float(m.group(2))
            step = msg.split(":", 1)[1].strip().split(" ")[0] if ":" in msg else ""
            cur["trail_steps"].append(step)
        continue
    if msg.startswith("TRAIL_SL: broker rejected") or "Refusing backward SL" in msg or msg.startswith("modify_sl failed") or "Refusing to move SL backward" in msg:
        cur["trail_rejected"] += 1
        continue

    # partial legs carry their OWN realized pnl (separate from the final leg)
    if "TP1 partial exit recorded" in msg:
        cur["tp1_partial"] = 1
        if ctx.get("pnl") is not None:
            try: cur["partial_pnls"].append(float(ctx["pnl"]))
            except Exception: pass
        continue
    if "TP1 partial close 50% executed" in msg:
        cur["tp1_partial"] = 1; continue
    if msg.startswith("Partial close 50%"):
        cur["tp1_fill"] = ctx.get("close_price"); cur["tp1_partial"] = 1; continue
    if msg.startswith("TP2 partial booked"):
        cur["tp2_partial"] = 1
        if ctx.get("pnl") is not None:
            try: cur["partial_pnls"].append(float(ctx["pnl"]))
            except Exception: pass
        continue

    # final exit (authoritative final leg, one per trade). The same close is also
    # reported by "Position for X no longer exists" - do NOT add both (double count).
    if msg.startswith("Updated comprehensive trade exit"):
        cur["exit_time"] = pt(o.get("time"))
        cur["exit_price"] = ctx.get("exit_price")
        cur["exit_reason"] = ctx.get("exit_reason")
        if ctx.get("pnl") is not None:
            try: cur["final_pnl"] = float(ctx["pnl"])
            except Exception: pass
        if ctx.get("candle_count") is not None:
            cur["candles_held"] = ctx.get("candle_count")
        open_trade.pop(base, None)
        continue
    if msg.startswith("Position for") and "no longer exists" in msg:
        if cur["final_pnl"] is None and ctx.get("pnl") is not None:
            try: cur["final_pnl"] = float(ctx["pnl"])
            except Exception: pass
        if cur["exit_price"] is None:
            cur["exit_price"] = ctx.get("exit_price")
            cur["exit_reason"] = ctx.get("exit_reason")
        if ctx.get("candle_count") is not None:
            cur["candles_held"] = ctx.get("candle_count")
        if cur["exit_time"] is None:
            cur["exit_time"] = pt(o.get("time"))

# ---- pass 3: compute metrics + write CSV ------------------------------------
def sgn(side): return 1.0 if (side or "").lower() == "buy" else -1.0

recs = []
for i, t in enumerate(trades, 1):
    s = sgn(t["side"])
    ep, sl, rp = t["entry_price"], t["initial_sl"], t["risk_points"]
    exitp = t["exit_price"]
    hold_min = None
    if t["entry_time"] and t["exit_time"]:
        hold_min = round((t["exit_time"] - t["entry_time"]).total_seconds() / 60, 1)
    price_R = None
    if exitp is not None and rp:
        price_R = round(s * (float(exitp) - ep) / rp, 3)
    tp1_R = round(s * (float(t["tp1"]) - ep) / rp, 2) if (t["tp1"] and rp) else None
    tp2_R = round(s * (float(t["tp2"]) - ep) / rp, 2) if (t["tp2"] and rp) else None
    legs = ([t["final_pnl"]] if t["final_pnl"] is not None else []) + t["partial_pnls"]
    pnl_total = round(sum(legs), 2) if legs else None
    pnl_partial = round(sum(t["partial_pnls"]), 2) if t["partial_pnls"] else 0.0
    risk_money = round(0.01 * float(t["funds"]), 2) if t["funds"] else None
    R_money = round(pnl_total / risk_money, 3) if (pnl_total is not None and risk_money) else None
    be_or_better = None
    if t["final_sl"] is not None and ep:
        be_or_better = 1 if s * (t["final_sl"] - ep) >= 0 else 0
    outcome = None
    if pnl_total is not None:
        outcome = "WIN" if pnl_total > 0 else ("LOSS" if pnl_total < 0 else "BE")
    recs.append({
        "n": i, "asset": t["asset"], "side": t["side"], "order_id": t["order_id"],
        "entry_time": t["entry_time"].strftime("%Y-%m-%d %H:%M") if t["entry_time"] else "",
        "exit_time": t["exit_time"].strftime("%Y-%m-%d %H:%M") if t["exit_time"] else "",
        "hold_min": hold_min, "candles_held": t["candles_held"],
        "entry_price": ep, "initial_sl": round(sl, 5) if sl else None,
        "risk_points": rp, "tp1": t["tp1"], "tp2": t["tp2"],
        "tp1_R": tp1_R, "tp2_R": tp2_R,
        "exit_price": exitp, "exit_reason": t["exit_reason"],
        "tp1_partial": t["tp1_partial"], "tp2_partial": t["tp2_partial"],
        "trail_count": t["trail_count"], "trail_rejected": t["trail_rejected"],
        "final_sl": t["final_sl"], "sl_at_BE_or_better": be_or_better,
        "price_R": price_R, "risk_money_usd": risk_money,
        "pnl_partial_usd": pnl_partial, "pnl_final_usd": t["final_pnl"],
        "pnl_usd": pnl_total, "R_money": R_money, "outcome": outcome,
        "funds_at_entry": t["funds"],
    })

import os
os.makedirs("data/processed", exist_ok=True)
cols = list(recs[0].keys())
with open(OUT, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=cols)
    w.writeheader()
    for r in recs: w.writerow(r)

print(f"Wrote {len(recs)} trades -> {OUT}\n")

# ---- aggregate metrics -------------------------------------------------------
def safe(arr): return [x for x in arr if x is not None]
pnls = safe([r["pnl_usd"] for r in recs])
Rm = safe([r["R_money"] for r in recs])
pR = safe([r["price_R"] for r in recs])
holds = safe([r["hold_min"] for r in recs])
wins = [r for r in recs if r["outcome"] == "WIN"]
losses = [r for r in recs if r["outcome"] == "LOSS"]
win_R = safe([r["R_money"] for r in wins]); loss_R = safe([r["R_money"] for r in losses])
gross_win = sum(p for p in pnls if p > 0); gross_loss = sum(p for p in pnls if p < 0)

print("="*60)
print("  AGGREGATE METRICS  (2-day live run, trial account)")
print("="*60)
print(f"  Trades                : {len(recs)}")
print(f"  Wins / Losses / BE    : {len(wins)} / {len(losses)} / {len(recs)-len(wins)-len(losses)}")
print(f"  Win rate              : {100*len(wins)/len(recs):.1f}%")
print(f"  Net P&L (USD)         : {sum(pnls):+.2f}")
print(f"  Gross win / loss      : {gross_win:+.2f} / {gross_loss:+.2f}")
print(f"  Profit factor         : {abs(gross_win/gross_loss):.2f}" if gross_loss else "  Profit factor         : inf")
print(f"  Avg P&L / trade       : {mean(pnls):+.2f}")
print(f"  Expectancy (R_money)  : {mean(Rm):+.3f} R")
print(f"  Avg WIN  (R_money)    : {mean(win_R):+.3f} R" if win_R else "")
print(f"  Avg LOSS (R_money)    : {mean(loss_R):+.3f} R" if loss_R else "")
print(f"  Best / Worst trade    : {max(pnls):+.2f} / {min(pnls):+.2f} USD")
print(f"  Best / Worst R_money  : {max(Rm):+.2f} / {min(Rm):+.2f} R")
print(f"  Avg price_R (final)   : {mean(pR):+.3f} R")
print(f"  Avg hold (min)        : {mean(holds):.1f}  (median {median(holds):.0f})")
print(f"  Avg candles held      : {mean(safe([r['candles_held'] for r in recs])):.1f}")
print(f"  Trades w/ TP1 partial : {sum(r['tp1_partial'] for r in recs)} ({100*sum(r['tp1_partial'] for r in recs)/len(recs):.0f}%)")
print(f"  Trades w/ SL trailed  : {sum(1 for r in recs if r['trail_count']>0)} ({100*sum(1 for r in recs if r['trail_count']>0)/len(recs):.0f}%)")
print(f"  Total trail moves     : {sum(r['trail_count'] for r in recs)}  (rejected {sum(r['trail_rejected'] for r in recs)})")
print(f"  Avg trails / trade    : {mean([r['trail_count'] for r in recs]):.1f}")
print(f"  SL moved >=BE         : {sum(1 for r in recs if r['sl_at_BE_or_better']==1)} trades")
print()
print("  Exit-reason breakdown :")
er = defaultdict(lambda: [0, 0.0])
for r in recs:
    er[r["exit_reason"]][0] += 1
    if r["pnl_usd"] is not None: er[r["exit_reason"]][1] += r["pnl_usd"]
for k, v in sorted(er.items(), key=lambda x: -x[1][0]):
    print(f"    {str(k):24} n={v[0]:2}  pnl={v[1]:+8.2f}")
print()
print("  Per-asset breakdown   :")
pa = defaultdict(lambda: {"n": 0, "pnl": 0.0, "w": 0})
for r in recs:
    pa[r["asset"]]["n"] += 1
    if r["pnl_usd"] is not None:
        pa[r["asset"]]["pnl"] += r["pnl_usd"]
        if r["pnl_usd"] > 0: pa[r["asset"]]["w"] += 1
for k, v in sorted(pa.items(), key=lambda x: -x[1]["pnl"]):
    print(f"    {k:8} n={v['n']:2}  win%={100*v['w']/v['n']:3.0f}  pnl={v['pnl']:+8.2f}")
print()
print("  Long vs Short         :")
for sd in ("buy", "sell"):
    g = [r for r in recs if (r["side"] or "").lower() == sd]
    if not g: continue
    gp = safe([r["pnl_usd"] for r in g]); gw = sum(1 for r in g if r["outcome"] == "WIN")
    gR = safe([r["R_money"] for r in g])
    expR = f"{mean(gR):+.2f}" if gR else "n/a"
    print(f"    {sd:5} n={len(g):2}  win%={100*gw/len(g):3.0f}  pnl={sum(gp):+8.2f}  expR={expR}")
