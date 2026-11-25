"""Position-sizing counterfactual simulator — "what would this window of REAL
trades have done under a different risk% or account size?"

Read-only. Places no orders. Re-sizes each already-executed position and rescales
its broker-true P&L. The core validity fact: SL / TP / trail triggers are all at
fixed PRICES and fixed R-multiples, so changing the lot size changes ONLY the
dollars — never the exit. Therefore per-position P&L scales linearly with lot,
and a clean counterfactual is just: sim_lot / actual_lot × actual_gross.

It uses the PRODUCTION sizing function (`calc_lot_size_by_risk`, which floors to
lot_step then clamps to [min_lot, max_lot]) and the real contract specs, so the
"floored / min-lot overshoot" (LP-01) count matches what the live system would do.

Inputs
------
--deals   Broker-truth deals.csv from pull_live_mt5.py (grouped by position_id;
          app positions filtered by IN-deal magic). Default: newest
          data/audit/mktdata*/deals.csv.
--logs    Day logs scanned for each position's ORIGINAL entry & stop_loss
          (first `event:trade_open` per position_ticket). Default: all
          results/logs/backtest_YYYY-MM-DD.log in the deals' date range.
--from --to   Optional YYYY-MM-DD filter on deal open date.
--scenario base:risk_pct   Repeatable. E.g. --scenario 445:1.5 --scenario 2000:1.5
          Defaults to the four-way current/3%/$2k/$2k-3% grid.
--magic   App magic number (default 234000; manual trades excluded).

Usage
-----
    python scripts/audit/sizing_scenarios.py \
        --deals data/audit/mktdata_2026-07-13/deals.csv

    # Freeze-sample review (August): point --logs/--deals at the full window.
    python scripts/audit/sizing_scenarios.py --from 2026-07-13 --to 2026-08-10

Caveats (read before quoting a number)
--------------------------------------
* Fixed sizing base per scenario (NO intra-window compounding) — isolates the
  structural effect of risk%/size. Real live sizing uses the running balance;
  expect small drift vs the actual day deltas. Validate in % terms, not dollars:
  a correct model reproduces each day's drawdown-% closely (07-09: model -12.7%
  vs actual -12.8%).
* The -10% daily DD kill-switch is annotated separately, not baked into the
  totals — it blocks NEW entries only (open trades run to their stop), which a
  position-level rescale can't model exactly. The totals are therefore the
  pre-halt figure; the annotation says where the switch would have capped a day.
* Partial exits / re-entries scale linearly with the open lot (a 2x position
  closes 2x lots at each partial), so position-level scaling stays exact.
"""
import argparse
import csv
import glob
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from packages.core.risk import CONTRACT_SPECS  # production specs
from packages.core.risk.risk_mt5 import calc_lot_size_by_risk  # production sizing


def parse_log_opens(log_paths):
    """ticket -> (symbol, entry, sl) from the FIRST trade_open per ticket."""
    opens = {}
    for path in log_paths:
        try:
            f = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with f:
            for line in f:
                if '"event":"trade_open"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                pl = rec.get("payload") or {}
                tk = str(pl.get("position_ticket") or "")
                if not tk or tk in opens:
                    continue
                entry = pl.get("entry_price") or pl.get("price")
                sl = pl.get("stop_loss")
                sym = pl.get("symbol")
                if entry and sl and sym:
                    opens[tk] = (sym, float(entry), float(sl))
    return opens


def parse_deals(deals_path, magic):
    """position_id -> dict(sym, in_lot, in_comm, gross, in_time). App only."""
    pos = defaultdict(lambda: {"in_lot": 0.0, "in_comm": 0.0, "gross": 0.0,
                               "sym": None, "in_time": None, "magic_in": None})
    with open(deals_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            pid = row["position_id"]
            p = pos[pid]
            p["sym"] = row["symbol"]
            if int(row["entry"]) == 0:  # IN deal
                p["in_lot"] = float(row["volume"])
                p["in_comm"] += float(row["commission"])
                p["in_time"] = row["time"]
                p["magic_in"] = int(row["magic"])
            else:                       # OUT deal(s) — partials sum
                p["gross"] += float(row["profit"])
                p["in_comm"] += float(row["commission"])
    return {pid: p for pid, p in pos.items() if p["magic_in"] == magic}


def build_trades(deals, opens, frm, to):
    trades, skipped = [], 0
    for pid, p in deals.items():
        if pid not in opens:
            skipped += 1
            continue
        day = p["in_time"][:10]
        if (frm and day < frm) or (to and day > to):
            continue
        sym, entry, sl = opens[pid]
        R = abs(entry - sl)
        if R <= 0 or sym not in CONTRACT_SPECS or not p["in_lot"]:
            skipped += 1
            continue
        trades.append(dict(
            pid=pid, sym=sym, R=R, day=day,
            t=datetime.fromisoformat(p["in_time"]),
            act_lot=p["in_lot"], act_gross=p["gross"],
            comm_per_lot=abs(p["in_comm"]) / p["in_lot"],
        ))
    trades.sort(key=lambda x: x["t"])
    return trades, skipped


def sim_lot(base, risk_frac, sym, R):
    """Production lot + the pre-clamp raw lot (to detect min-lot overshoot)."""
    spec = CONTRACT_SPECS[sym]
    raw = (base * risk_frac) / (R * spec.contract_size)
    lot = calc_lot_size_by_risk(base, risk_frac, R, sym, CONTRACT_SPECS)
    return lot, raw


def run(trades, base, risk_pct, dd_pct=10.0):
    risk_frac = risk_pct / 100.0
    by_day = defaultdict(lambda: {"pnl": 0.0, "comm": 0.0, "n": 0, "w": 0, "l": 0, "flr": 0})
    for tr in trades:
        lot, raw = sim_lot(base, risk_frac, tr["sym"], tr["R"])
        floored = lot > raw + 1e-12
        scale = lot / tr["act_lot"]
        gross = tr["act_gross"] * scale
        comm = -tr["comm_per_lot"] * lot
        b = by_day[tr["day"]]
        b["pnl"] += gross
        b["comm"] += comm
        b["n"] += 1
        b["flr"] += 1 if floored else 0
        b["w" if gross > 0 else "l"] += 1

    print(f"\n===== base ${base:.0f} @ {risk_pct:.1f}% =====")
    print(f"{'day':11s} {'net$':>9s} {'net%':>7s} {'gross$':>9s} {'comm$':>8s} "
          f"{'n':>3s} {'W-L':>6s} {'floored':>8s}")
    tg = tc = tn = tf = 0
    for d in sorted(by_day):
        b = by_day[d]
        net = b["pnl"] + b["comm"]
        tg += b["pnl"]; tc += b["comm"]; tn += b["n"]; tf += b["flr"]
        print(f"{d:11s} {net:+9.2f} {net / base * 100:+6.1f}% {b['pnl']:+9.2f} "
              f"{b['comm']:+8.2f} {b['n']:3d} {str(b['w']) + '-' + str(b['l']):>6s} "
              f"{str(b['flr']) + '/' + str(b['n']):>8s}")
    net = tg + tc
    print(f"{'TOTAL':11s} {net:+9.2f} {net / base * 100:+6.1f}% {tg:+9.2f} {tc:+8.2f} "
          f"{tn:3d} {'':6s} {str(tf) + '/' + str(tn):>8s}")
    print(f"  commission drag {abs(tc) / base * 100:.1f}% of base | "
          f"overshoot(min-lot-bound) {tf}/{tn} trades")
    return net


def dd_annotation(trades, scenarios, dd_pct=10.0):
    print(f"\n===== -{dd_pct:.0f}% daily DD kill-switch (blocks NEW entries; open trades run) =====")
    days = sorted({t["day"] for t in trades})
    for base, risk_pct in scenarios:
        parts = []
        for d in days:
            dts = [t for t in trades if t["day"] == d]
            pnl, halt = 0.0, None
            for i, tr in enumerate(dts):
                lot, _ = sim_lot(base, risk_pct / 100.0, tr["sym"], tr["R"])
                pnl += tr["act_gross"] * lot / tr["act_lot"] - tr["comm_per_lot"] * lot
                if pnl <= -dd_pct / 100.0 * base and halt is None:
                    halt = i + 1
            parts.append(f"{d[5:]}:{'HALT@' + str(halt) + '/' + str(len(dts)) if halt else 'ok'}")
        print(f"  ${base:>5.0f}@{risk_pct:.1f}%  " + "  ".join(parts))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--deals", default=None)
    ap.add_argument("--logs", nargs="+", default=None)
    ap.add_argument("--from", dest="frm", default=None)
    ap.add_argument("--to", dest="to", default=None)
    ap.add_argument("--scenario", action="append", default=None,
                    help="base:risk_pct, e.g. 445:1.5 (repeatable)")
    ap.add_argument("--magic", type=int, default=234000)
    a = ap.parse_args()

    deals_path = a.deals
    if not deals_path:
        cands = sorted(glob.glob(os.path.join(REPO, "data", "audit", "mktdata*", "deals.csv")))
        if not cands:
            sys.exit("No deals.csv found under data/audit/mktdata*/. Run pull_live_mt5.py first.")
        deals_path = cands[-1]
    log_paths = a.logs or sorted(glob.glob(os.path.join(REPO, "results", "logs", "backtest_2026-*.log")))

    scenarios = ([tuple(float(x) for x in s.split(":")) for s in a.scenario]
                 if a.scenario else [(445, 1.5), (445, 3.0), (2000, 1.5), (2000, 3.0)])

    opens = parse_log_opens(log_paths)
    deals = parse_deals(deals_path, a.magic)
    trades, skipped = build_trades(deals, opens, a.frm, a.to)

    print(f"deals: {deals_path}")
    print(f"app positions matched to a log open: {len(trades)} "
          f"(skipped {skipped}: no log open / bad R / non-app)")
    print("engine: production calc_lot_size_by_risk + real CONTRACT_SPECS "
          "(floor-to-step, clamp min/max lot)")
    if not trades:
        sys.exit("No trades to simulate — check --deals / --logs / date range.")

    for base, risk_pct in scenarios:
        run(trades, base, risk_pct)
    dd_annotation(trades, scenarios)
    print("\nRead net% (same trades, same risk% => the $445-vs-$2k gap is pure LP-01 "
          "overshoot+commission). A losing window stays red at every size; sizing is a "
          "magnifier, not an edge. Measure edge on the full freeze sample before deciding.")


if __name__ == "__main__":
    main()
