"""Simulate the 35 logged trades at 1% / 2% / 5% risk-per-trade.

Re-runs the REAL sizer (calc_lot_size_by_risk) at each risk level so the
min-lot floor on gold is respected (P&L does NOT scale linearly). P&L at a new
lot = (realized P&L per lot from the log) x new_lot.
"""
import json, os, sys
sys.path.insert(0, os.getcwd())
from packages.core.risk import CONTRACT_SPECS as C, calc_lot_size_by_risk

LOG = "logs (15).json"

# ---- pass 1: load & order -------------------------------------------------
rows = []
with open(LOG, "r", encoding="utf-8", errors="replace") as f:
    for line in f:
        try: o = json.loads(line)
        except Exception: continue
        rows.append(o)
rows.sort(key=lambda o: o.get("time", ""))

# ---- pass 2: build per-trade (entry-paired) -------------------------------
queued = {}      # (sym, entry) -> sl
sizings = []     # {asset, funds, time}
executes = []    # {asset, side, entry, qty, time}
# pnl legs per (asset, open-window): reuse simple asset-window stitch
open_idx = {}
trades = []
for o in rows:
    m = o.get("msg", ""); ctx = o.get("ctx", {}) or {}
    if m.startswith("Position size calculated"):
        sizings.append({"asset": ctx.get("asset"), "funds": ctx.get("available_funds"), "time": o.get("time")})
    elif m.startswith("Trade queued for monitoring"):
        queued[(ctx.get("symbol"), ctx.get("entry"))] = ctx.get("sl")
    elif m.startswith("Code sniper trade executed"):
        t = {"asset": o.get("asset"), "side": o.get("side"), "entry": o.get("entry"),
             "qty": o.get("quantity"), "time": o.get("time"),
             "final_pnl": None, "partials": []}
        open_idx[o.get("asset")] = t
        trades.append(t)
    else:
        a = o.get("asset") or ctx.get("asset") or ctx.get("symbol")
        base = a.replace("m", "") if a and a.endswith("m") else a
        cur = open_idx.get(base)
        if cur is None: continue
        if "TP1 partial exit recorded" in m or m.startswith("TP2 partial booked"):
            if ctx.get("pnl") is not None: cur["partials"].append(float(ctx["pnl"]))
        elif m.startswith("Updated comprehensive trade exit"):
            if ctx.get("pnl") is not None: cur["final_pnl"] = float(ctx["pnl"])
            open_idx.pop(base, None)
        elif m.startswith("Position for") and "no longer exists" in m:
            if cur["final_pnl"] is None and ctx.get("pnl") is not None:
                cur["final_pnl"] = float(ctx["pnl"])

def funds_before(asset, t):
    best = None
    for s in sizings:
        if s["asset"] == asset and s["time"] <= t: best = s["funds"]
    return best

# ---- pass 3: assemble + simulate ------------------------------------------
RISK_LEVELS = [0.01, 0.02, 0.05]
sim = []
for t in trades:
    a = t["asset"]; sl = queued.get((a, t["entry"])); f0 = funds_before(a, t["time"])
    cs = C[a].contract_size if a in C else None
    legs = ([t["final_pnl"]] if t["final_pnl"] is not None else []) + t["partials"]
    pnl = round(sum(legs), 4) if legs else None
    if None in (sl, f0, cs, t["qty"]) or pnl is None or t["qty"] == 0:
        continue
    sl_pts = abs(t["entry"] - sl)
    pnl_per_lot = pnl / t["qty"]
    row = {"asset": a, "side": t["side"], "entry": t["entry"], "sl_pts": sl_pts,
           "act_qty": t["qty"], "funds": f0, "pnl_act": pnl, "cs": cs}
    for r in RISK_LEVELS:
        lot = calc_lot_size_by_risk(balance=f0, risk_pct=r, sl_points=sl_pts,
                                    symbol=a, specs=C, entry_price=t["entry"])
        row[f"lot_{r}"] = lot
        row[f"pnl_{r}"] = pnl_per_lot * lot
        row[f"risk_{r}"] = lot * sl_pts * cs / f0 * 100
    sim.append(row)

# ---- output ----------------------------------------------------------------
print(f"Simulated {len(sim)} trades. P&L per trade at each risk-per-trade level (sized off the ACTUAL balance at that trade):\n")
hdr = f"{'#':>2} {'asset':7} {'side':4} {'qty@1%':>6} {'qty@5%':>6} | {'pnl@1%':>8} {'pnl@2%':>8} {'pnl@5%':>8} | {'risk@1%':>7} {'risk@5%':>7}"
print(hdr); print("-" * len(hdr))
tot = {r: 0.0 for r in RISK_LEVELS}
for i, r in enumerate(sim, 1):
    for rl in RISK_LEVELS: tot[rl] += r[f"pnl_{rl}"]
    flag = " <gold-floored" if r["asset"] == "XAUUSD" and abs(r["lot_0.01"] - r["lot_0.02"]) < 1e-9 else ""
    print(f"{i:>2} {r['asset']:7} {r['side']:4} {r['lot_0.01']:>6.2f} {r['lot_0.05']:>6.2f} | "
          f"{r['pnl_0.01']:>+8.2f} {r['pnl_0.02']:>+8.2f} {r['pnl_0.05']:>+8.2f} | "
          f"{r['risk_0.01']:>6.2f}% {r['risk_0.05']:>6.2f}%{flag}")
print("-" * len(hdr))
print(f"{'':>22}{'TOTAL':>6}  | {tot[0.01]:>+8.2f} {tot[0.02]:>+8.2f} {tot[0.05]:>+8.2f}")

print("\n=== NON-COMPOUNDED totals (each trade sized off its real logged balance) ===")
start = 574.05
for rl in RISK_LEVELS:
    print(f"  risk {int(rl*100)}%:  net P&L {tot[rl]:+8.2f}  |  on ${start:.0f} start = {tot[rl]/start*100:+6.1f}%")

# per-asset at each level
from collections import defaultdict
print("\n=== Per-asset net P&L ===")
pa = defaultdict(lambda: {0.01: 0.0, 0.02: 0.0, 0.05: 0.0})
for r in sim:
    for rl in RISK_LEVELS: pa[r["asset"]][rl] += r[f"pnl_{rl}"]
print(f"  {'asset':8} {'@1%':>9} {'@2%':>9} {'@5%':>9}   (note)")
for k in ("XAUUSD", "BTCUSD", "USOIL", "GBPUSD"):
    v = pa[k]
    note = "min-lot floored 1%->2% identical" if k == "XAUUSD" else "scales ~linearly"
    print(f"  {k:8} {v[0.01]:>+9.2f} {v[0.02]:>+9.2f} {v[0.05]:>+9.2f}   {note}")

print("\n=== COMPOUNDED final balance (start $574, each trade re-sized off running balance) ===")
for rl in RISK_LEVELS:
    bal = start
    for r in sim:
        lot = calc_lot_size_by_risk(balance=bal, risk_pct=rl, sl_points=r["sl_pts"],
                                    symbol=r["asset"], specs=C, entry_price=r["entry"])
        bal += (r["pnl_act"] / r["act_qty"]) * lot
    print(f"  risk {int(rl*100)}%:  ${start:.0f} -> ${bal:7.2f}   ({(bal/start-1)*100:+6.1f}% over 2 days, compounded)")
