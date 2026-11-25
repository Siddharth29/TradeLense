"""State of every cap-bound trade AT the 4h cut, and what the extra hours did.

The question the headline P&L cannot answer: extending the cap changed 18 trades,
9 up and 9 down. Is there a rule at the 4h mark that separates them? The two
candidates are the ones the app already has an opinion about:

  * ``trade_monitor.is_risk_free``  - has the stop ratcheted to entry or better?
  * ``trade_monitor.unrealised_r``  - is the position >= TTL_EXTEND_MIN_R green?

Together they are exactly ``maybe_extend_expiry``'s gate. This measures whether
that gate would have picked the winners out of the 18.

Runs the real PositionSimulator to the 4h mark, snapshots the live trade_state,
then continues the SAME simulator to the 24h cap. One simulator, so the state at
the cut is the state the trade actually had - not a re-derivation.
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import pandas as pd  # noqa: E402

from packages.core.backtest_account import PositionSimulator, build_rules  # noqa: E402
from packages.core.backtest_types import RESULT_TIMEOUT  # noqa: E402
from packages.core.config import (  # noqa: E402
    RUNNER_MAX_HOURS, SETTLEMENT_CANDLES, TTL_EXTEND_MIN_R,
)
from packages.core.trade_monitor import is_risk_free, unrealised_r  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ttl_extension_study import (  # noqa: E402
    _closing_soon_fn, future_window, load_candles, load_trades, run_scenario, TRADES_CSV,
)

BASE_H, EXT_H = 4, 24


def walk(t, candles, cut_h: int, run_h: int) -> dict:
    """Run one position to ``run_h``, snapshotting state at the ``cut_h`` mark."""
    fut = future_window(candles, t.entry_time, run_h, unbounded=True)
    state = {
        "direction": t.direction, "entry": t.entry, "sl": t.initial_sl,
        "initial_stop": t.initial_sl, "sl_moved": False,
        "tp1_hit": False, "tp2_hit": False, "quantity": t.qty, "asset": t.asset,
    }
    rules = build_rules(t.tp1, t.tp2, entry=t.entry, stop_loss=t.initial_sl, atr_1h=t.atr_1h)
    sim = PositionSimulator(t.asset, state, rules, t.qty,
                            settlement_candles=SETTLEMENT_CANDLES, skip_first_bar=True)
    closing_soon = _closing_soon_fn()
    cut = t.entry_time + timedelta(hours=cut_h)
    hard = t.entry_time + timedelta(hours=run_h)
    snap = None

    for when, row in fut.iterrows():
        when = when.to_pydatetime()
        if snap is None and when >= cut:
            snap = {
                "cut_pnl": sim.result.pnl,
                "cut_r": unrealised_r(state, row.close),
                "cut_risk_free": is_risk_free(state),
                "cut_trail_step": state.get("trail_step", 0),
                "cut_tp1": bool(state.get("tp1_hit")),
                "cut_closed": sim.result.closed,
            }
        if sim.result.closed:
            break
        closing, _ = closing_soon(t.asset, now=when)
        if closing:
            sim.force_close(row.close, when, reason=RESULT_TIMEOUT)
            break
        if when >= hard:
            sim.force_close(row.close, when, reason=RESULT_TIMEOUT)
            break
        was = sim.result.tp2_hit
        sim.step(row.close, row.high, row.low, when)
        if sim.result.tp2_hit and not was:
            hard = max(hard, t.entry_time + timedelta(hours=RUNNER_MAX_HOURS))

    if snap is None:
        snap = {"cut_pnl": sim.result.pnl, "cut_r": None, "cut_risk_free": None,
                "cut_trail_step": None, "cut_tp1": None, "cut_closed": True}
    snap.update({
        "final_pnl": sim.result.pnl, "final_result": sim.result.result,
        "hold_min": ((pd.Timestamp(sim.result.exit_time) - pd.Timestamp(t.entry_time)).total_seconds() / 60
                     if sim.result.exit_time is not None else None),
    })
    return snap


def main() -> int:
    trades = load_trades(TRADES_CSV)
    candles = {a: load_candles(a) for a in sorted({t.asset for t in trades})}

    base = run_scenario(trades, candles, BASE_H, "serial")
    taken = [r["trade"] for r in base["rows"] if r["taken"]]
    base_pnl = {(r["trade"].asset, r["trade"].entry_time): r["sim"]["profit"]
                for r in base["rows"] if r["taken"]}

    rows = []
    for t in taken:
        s = walk(t, candles[t.asset], BASE_H, EXT_H)
        if s["cut_closed"]:
            continue          # resolved before the cap ever bound it
        b = base_pnl[(t.asset, t.entry_time)]
        rows.append({
            "asset": t.asset, "entry": t.entry_time,
            "r_at_cut": s["cut_r"], "risk_free": s["cut_risk_free"],
            "trail_step": s["cut_trail_step"], "tp1": s["cut_tp1"],
            "pnl_4h": b, "pnl_24h": s["final_pnl"], "delta": s["final_pnl"] - b,
            "result_24h": s["final_result"], "hold_min": s["hold_min"],
        })

    df = pd.DataFrame(rows).sort_values("delta")
    pd.set_option("display.width", 220)
    print(f"Positions still open at the {BASE_H}h cut (serial, one at a time): {len(df)}\n")
    print(df.to_string(index=False, float_format=lambda x: f"{x:,.2f}"))

    print(f"\nnet of extending {BASE_H}h -> {EXT_H}h on these: ${df.delta.sum():+.2f}   "
          f"improved {int((df.delta > 0.005).sum())}  worsened {int((df.delta < -0.005).sum())}  "
          f"unchanged {int((df.delta.abs() <= 0.005).sum())}")

    print(f"\nSPLIT BY THE APP'S OWN EXTENSION GATE at the cut "
          f"(maybe_extend_expiry: risk-free AND >= {TTL_EXTEND_MIN_R}R green)")
    gate = df.risk_free & (df.r_at_cut >= TTL_EXTEND_MIN_R)
    for label, sub in (("gate PASSES (would extend)", df[gate]),
                       ("gate FAILS (closes at cap)", df[~gate])):
        if sub.empty:
            print(f"  {label:28} n=0")
            continue
        print(f"  {label:28} n={len(sub):2}  net ${sub.delta.sum():+8.2f}  "
              f"up {int((sub.delta > 0.005).sum())}/down {int((sub.delta < -0.005).sum())}  "
              f"mean ${sub.delta.mean():+7.2f}")

    print("\nSPLIT BY THE STOP ALONE (has the trail ratcheted to entry or better?)")
    for label, sub in (("stop is risk-free", df[df.risk_free]),
                       ("stop still at initial", df[~df.risk_free])):
        if sub.empty:
            print(f"  {label:28} n=0")
            continue
        print(f"  {label:28} n={len(sub):2}  net ${sub.delta.sum():+8.2f}  "
              f"up {int((sub.delta > 0.005).sum())}/down {int((sub.delta < -0.005).sum())}  "
              f"mean ${sub.delta.mean():+7.2f}  "
              f"worst ${sub.delta.min():+.2f}")

    print("\nSPLIT BY UNREALISED R AT THE CUT")
    for lo, hi in ((-9, 0), (0, 0.5), (0.5, 9)):
        sub = df[(df.r_at_cut >= lo) & (df.r_at_cut < hi)]
        if sub.empty:
            continue
        print(f"  R in [{lo:+.1f},{hi:+.1f})            n={len(sub):2}  net ${sub.delta.sum():+8.2f}  "
              f"up {int((sub.delta > 0.005).sum())}/down {int((sub.delta < -0.005).sum())}  "
              f"mean ${sub.delta.mean():+7.2f}")

    print(f"\nHOLD TIME NEEDED (of the {len(df)} that ran past {BASE_H}h)")
    for h in (6, 8, 12, 16, 24):
        print(f"  resolved within {h:2}h: {int((df.hold_min <= h * 60).sum()):2}/{len(df)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
