"""What would the 2026-07-13 -> 07-29 backtest have done at an 8h / 12h / 24h cap?

The entries are FIXED. The 105 entries are the model's decisions on real candles;
nothing here re-runs the model or invents a setup. One number is varied:
``CSM_TRADE_EXPIRY_HOURS`` (4 today), the cap a position is truncated at.

Every trade is replayed through **the production exit engine itself** --
``trade_sim.simulate_trade`` -> ``backtest_account.PositionSimulator`` ->
``trade_monitor.evaluate_trade`` -- fed the candle window the runner builds. So
the TP1 half-book, the BE+0.5R move, the eight-step trail ladder, touch arming,
the TP2 partial, the ATR chandelier on the runner, min-lot partial refusal,
commission and the pre-close/weekend guard are the real ones.

Three scenarios, because the recorded run does not enforce the constraint live does
-----------------------------------------------------------------------------------
``asrun``   What the runner produced AS IT WAS on 2026-08-15, when this window
            was recorded: every hourly checkpoint called the model regardless of
            what was open, because ``_execute_iteration`` resolved a trade to
            completion inside its own call (it was handed cap+1h of look-ahead)
            and so never returned ``Open`` -- ``active_trade`` was never set and
            the one-position-at-a-time branch never ran. Measured: 0 of 1,756
            checkpoints took the monitoring branch; 18 of 105 entries opened
            while another position was still live.

            NOTE (2026-08-27): the runner no longer behaves this way. The bar
            clock (``_advance_closed_bars_to``) processes only bars that have
            closed, and a position now blocks the model call, so a fresh run
            cannot reproduce this column. It is kept because it is what the
            RECORDED sample contains, and dropping it would hide the difference
            between the sample and the engine that will produce the next one.

``serial``  The engineering-honest scenario: one position at a time, which is
            what ``execute_backtest``'s ``active_trade`` guard and the live
            monitor both enforce. A later entry whose arming checkpoint falls
            inside the previous position's life never happens. Hard cap, no
            extension -- this is what ``simulate_trade`` does today.

``serial+ext`` ``serial`` plus the hold extensions the app is *designed* to grant
            but which this backtest never reached, because they live in the
            checkpoint-monitor branch: ``trade_monitor.maybe_extend_expiry``
            (green >= TTL_EXTEND_MIN_R and risk-free earns +TTL_EXTEND_HOURS at
            the expiry, to TTL_EXTEND_MAX_HOURS) and the TP2 runner's jump to
            RUNNER_MAX_HOURS. Both use the real functions and the real config.

No invented entries: a checkpoint freed by an earlier exit is NOT filled with a
setup we never observed. ``serial`` counts are recorded entries MINUS those
crowded out, never plus.

Calibration gate: the 4h ``asrun`` replay must reproduce the recorded run before
any counterfactual prints. An unverified simulator measures itself.

    python scripts/audit/ttl_extension_study.py
    python scripts/audit/ttl_extension_study.py --hours 4,8,12,24 --csv out.csv
"""
from __future__ import annotations

import argparse
import csv
import random
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import pandas as pd  # noqa: E402

from packages.core.backtest_account import PositionSimulator, build_rules  # noqa: E402
from packages.core.backtest_types import RESULT_TIMEOUT  # noqa: E402
from packages.core.config import RUNNER_MAX_HOURS, SETTLEMENT_CANDLES  # noqa: E402
from packages.core.trade_monitor import maybe_extend_expiry  # noqa: E402
from packages.core.trade_sim import simulate_trade  # noqa: E402

TRADES_CSV = REPO / "data" / "audit" / "freeze_backtest_trades_full.csv"
CANDLE_DIR = REPO / "data" / "processed"

# What the 4h replay must hit against the recorded P&L before any counterfactual
# is believable. Set from what a faithful replay of the same engine on the same
# candles should achieve, not from what a run happens to produce.
MIN_CAL_CORR = 0.99
MAX_CAL_MAE = 1.00   # dollars per trade

SCENARIOS = ("asrun", "serial", "serial+ext")


@dataclass
class Trade:
    asset: str
    direction: str
    entry_time: datetime
    arm_checkpoint: Optional[datetime]
    entry: float
    initial_sl: float
    tp1: float
    tp2: float
    qty: float
    atr_1h: Optional[float]
    rec_result: str
    rec_profit: float
    rec_exit_time: datetime

    @property
    def gate(self) -> datetime:
        """The checkpoint at which this entry needed a free slot to exist."""
        return self.arm_checkpoint or self.entry_time


def load_trades(path: Path) -> List[Trade]:
    out = []
    for r in csv.DictReader(path.open(encoding="utf-8")):
        arm = r["arm_checkpoint"].strip()
        out.append(Trade(
            asset=r["asset"],
            direction=r["direction"],
            entry_time=pd.Timestamp(r["entry_time"]).to_pydatetime(),
            arm_checkpoint=pd.Timestamp(arm).to_pydatetime() if arm else None,
            entry=float(r["entry"]),
            initial_sl=float(r["initial_sl"]),
            tp1=float(r["tp1"]),
            tp2=float(r["tp2"]),
            qty=float(r["qty"]),
            atr_1h=float(r["atr_1h"]) if r["atr_1h"].strip() else None,
            rec_result=r["result"],
            rec_profit=float(r["profit"]),
            rec_exit_time=pd.Timestamp(r["exit_time"]).to_pydatetime(),
        ))
    return out


def load_candles(asset: str) -> pd.DataFrame:
    df = pd.read_csv(CANDLE_DIR / f"{asset.lower()}_5m_2years.csv",
                     index_col=0, parse_dates=True)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df.sort_index()


def future_window(candles: pd.DataFrame, entry_time: datetime, hours: int,
                  *, unbounded: bool = False) -> pd.DataFrame:
    """The candle window ``_execute_iteration`` hands to ``simulate_trade``.

    Runner logic: locate the trigger bar, include it as bar 0, then append up to
    ``BT_MONITOR_CANDLES_5M + 12`` more (``max_fetch``). ``simulate_trade`` then
    truncates to ``hours*12 + 1``. Both scale with the cap under test.
    ``unbounded`` hands back everything after the entry, for the extension path
    whose expiry can move past the base cap.
    """
    pos = candles.index.searchsorted(pd.Timestamp(entry_time), side="right") - 1
    if pos < 0:
        return pd.DataFrame()
    if unbounded:
        return candles.iloc[pos:]
    max_fetch = min((hours * 60) // 5 + 12, len(candles) - pos)
    return candles.iloc[pos: pos + max_fetch]


def _closing_soon_fn():
    from packages.core.services.code_sniper_monitor import _is_market_closing_soon
    return _is_market_closing_soon


def replay_hard_cap(t: Trade, candles: pd.DataFrame, hours: int) -> Optional[dict]:
    """The engine as it stands: a hard truncation at ``hours``, no extension."""
    fut = future_window(candles, t.entry_time, hours)
    if fut.empty:
        return None
    pred = {
        "direction": t.direction, "entry": t.entry, "stop": t.initial_sl,
        "target": t.tp2, "tp1": t.tp1, "tp2": t.tp2, "quantity": t.qty,
        "atr_1h": t.atr_1h, "pattern": "code_sniper",
        "entry_timestamp": pd.Timestamp(t.entry_time).isoformat(),
    }
    return simulate_trade(pred, fut, asset=t.asset, lot_size=t.qty,
                          max_trade_duration_hours=hours, atr_1h=t.atr_1h)


def replay_with_extension(t: Trade, candles: pd.DataFrame, hours: int) -> Optional[dict]:
    """The checkpoint monitor's lifecycle: base cap + the green/runner extensions.

    Mirrors ``execute_backtest``'s monitoring branch bar for bar: pre-close guard
    first, then the expiry check, then ``PositionSimulator.step``; the expiry is
    re-tested for a green-trade extension at each hourly checkpoint (which is
    where ``maybe_extend_expiry`` is called live), and jumps to RUNNER_MAX_HOURS
    the moment TP2 books. Every decision comes from production code.
    """
    fut = future_window(candles, t.entry_time, hours, unbounded=True)
    if fut.empty:
        return None
    trade_state = {
        "direction": t.direction, "entry": t.entry, "sl": t.initial_sl,
        "initial_stop": t.initial_sl, "sl_moved": False,
        "tp1_hit": False, "tp2_hit": False, "quantity": t.qty, "asset": t.asset,
    }
    rules = build_rules(t.tp1, t.tp2, entry=t.entry, stop_loss=t.initial_sl,
                        atr_1h=t.atr_1h)
    sim = PositionSimulator(t.asset, trade_state, rules, t.qty,
                            settlement_candles=SETTLEMENT_CANDLES, skip_first_bar=True)
    closing_soon = _closing_soon_fn()
    expiry = t.entry_time + timedelta(hours=hours)
    last_hour_checked: Optional[datetime] = None

    def out(res: str, price: float, when) -> dict:
        return {
            "direction": t.direction, "entry": t.entry, "stop": trade_state["sl"],
            "initial_stop": t.initial_sl, "target": t.tp2, "tp1": t.tp1,
            "entry_time": t.entry_time, "exit_time": when, "exit_price": price,
            "profit": sim.result.pnl, "result": res,
        }

    for when, row in fut.iterrows():
        when = when.to_pydatetime()
        closing, _ = closing_soon(t.asset, now=when)
        if closing:
            sim.force_close(row.close, when, reason=RESULT_TIMEOUT)
            return out(RESULT_TIMEOUT, row.close, when)

        # Green-trade extension is evaluated where live evaluates it: at the
        # hourly checkpoint, against the mark price, before the expiry test.
        hour = when.replace(minute=0, second=0, microsecond=0)
        if hour != last_hour_checked:
            last_hour_checked = hour
            expiry = maybe_extend_expiry(trade_state, row.close, expiry,
                                         start_time=t.entry_time, now=when)

        if when >= expiry:
            sim.force_close(row.close, when, reason=RESULT_TIMEOUT)
            return out(RESULT_TIMEOUT, row.close, when)

        was_tp2 = sim.result.tp2_hit
        sim.step(row.close, row.high, row.low, when)
        if sim.result.tp2_hit and not was_tp2:
            expiry = max(expiry, t.entry_time + timedelta(hours=RUNNER_MAX_HOURS))
        if sim.result.closed:
            return out(sim.result.result, sim.result.exit_price, sim.result.exit_time)

    last = fut.index[-1].to_pydatetime()
    sim.force_close(fut.iloc[-1].close, last, reason=RESULT_TIMEOUT)
    return out(RESULT_TIMEOUT, fut.iloc[-1].close, last)


def _price_r(sim: dict) -> Optional[float]:
    risk = abs(sim["entry"] - (sim.get("initial_stop") or sim["stop"]))
    if not risk or sim.get("exit_price") is None:
        return None
    sgn = 1 if sim["direction"] == "long" else -1
    return sgn * (sim["exit_price"] - sim["entry"]) / risk


def run_scenario(trades: List[Trade], candles: Dict[str, pd.DataFrame],
                 hours: int, scenario: str) -> dict:
    serial = scenario != "asrun"
    replay = replay_with_extension if scenario == "serial+ext" else replay_hard_cap
    rows: List[dict] = []
    for asset in sorted({t.asset for t in trades}):
        chain = sorted((t for t in trades if t.asset == asset), key=lambda x: x.entry_time)
        free_from: Optional[datetime] = None
        for t in chain:
            if serial and free_from is not None and t.gate <= free_from:
                rows.append({"trade": t, "taken": False, "sim": None, "blocked_until": free_from})
                continue
            sim = replay(t, candles[asset], hours)
            if sim is None:
                rows.append({"trade": t, "taken": False, "sim": None, "blocked_until": None})
                continue
            if sim.get("exit_time") is not None:
                free_from = pd.Timestamp(sim["exit_time"]).to_pydatetime()
            rows.append({"trade": t, "taken": True, "sim": sim, "blocked_until": None})
    return {"hours": hours, "scenario": scenario, "rows": rows}


def summarise(scn: dict) -> dict:
    taken = [r for r in scn["rows"] if r["taken"]]
    pnl = sum(r["sim"]["profit"] for r in taken)
    wins = sum(1 for r in taken if r["sim"]["profit"] > 0)
    rs = [x for x in (_price_r(r["sim"]) for r in taken) if x is not None]
    durs = [(pd.Timestamp(r["sim"]["exit_time"]) - pd.Timestamp(r["trade"].entry_time)).total_seconds() / 60
            for r in taken if r["sim"].get("exit_time")]
    res: Dict[str, int] = {}
    for r in taken:
        res[r["sim"]["result"]] = res.get(r["sim"]["result"], 0) + 1
    cap_bound = sum(1 for r in taken
                    if r["sim"]["result"] == RESULT_TIMEOUT
                    and (pd.Timestamp(r["sim"]["exit_time"]) - pd.Timestamp(r["trade"].entry_time)).total_seconds() / 60
                    >= scn["hours"] * 60 - 6)
    return {
        "hours": scn["hours"], "scenario": scn["scenario"], "n": len(taken),
        "blocked": sum(1 for r in scn["rows"] if not r["taken"] and r["blocked_until"]),
        "pnl": pnl, "wins": wins, "losses": len(taken) - wins,
        "wr": 100 * wins / len(taken) if taken else 0.0,
        "sum_r": sum(rs), "r_per_trade": (sum(rs) / len(rs)) if rs else 0.0,
        "median_dur": pd.Series(durs).median() if durs else 0.0,
        "max_dur": max(durs) if durs else 0.0,
        "cap_bound": cap_bound, "results": res,
    }


def calibrate(trades: List[Trade], candles: Dict[str, pd.DataFrame]) -> Tuple[bool, str]:
    pairs, mism = [], []
    for t in trades:
        sim = replay_hard_cap(t, candles[t.asset], 4)
        if sim is None:
            continue
        pairs.append((t.rec_profit, sim["profit"]))
        if abs(t.rec_profit - sim["profit"]) > 0.01 or t.rec_result != sim["result"]:
            mism.append((t, sim))
    if len(pairs) < 50:
        return False, f"only {len(pairs)} replayable trades"
    a = pd.Series([p[0] for p in pairs])
    s = pd.Series([p[1] for p in pairs])
    corr, mae = a.corr(s), (a - s).abs().mean()
    detail = (f"n={len(pairs)}  exact match={len(pairs)-len(mism)}/{len(pairs)}  "
              f"corr={corr:+.4f} (need >= {MIN_CAL_CORR})  MAE=${mae:.4f} "
              f"(need <= ${MAX_CAL_MAE:.2f})\n  recorded ${a.sum():+.2f}   "
              f"replayed ${s.sum():+.2f}   delta ${s.sum()-a.sum():+.2f}")
    for t, sim in mism[:10]:
        detail += (f"\n    {t.asset:6} {t.entry_time:%m-%d %H:%M} "
                   f"rec={t.rec_result:8}${t.rec_profit:+9.2f}  "
                   f"sim={sim['result']:8}${sim['profit']:+9.2f}")
    return (corr >= MIN_CAL_CORR and mae <= MAX_CAL_MAE), detail


def paired_test(base: dict, alt: dict, n_boot: int = 20000, seed: int = 7) -> str:
    """Per-trade paired delta on the trades BOTH scenarios took, bootstrapped.

    The paired statistic is the sharp one: same entries, same candles, only the
    cap differs, so the per-trade noise that swamps a two-sample comparison
    cancels. Trades taken in only one scenario are reported separately rather
    than folded in, which would smuggle a different sample into a paired test.
    """
    bk = {(r["trade"].asset, r["trade"].entry_time): r for r in base["rows"] if r["taken"]}
    ak = {(r["trade"].asset, r["trade"].entry_time): r for r in alt["rows"] if r["taken"]}
    common = sorted(set(bk) & set(ak))
    if not common:
        return "no common trades"
    d = [ak[k]["sim"]["profit"] - bk[k]["sim"]["profit"] for k in common]
    changed = [x for x in d if abs(x) > 0.005]
    mean = sum(d) / len(d)
    rng = random.Random(seed)
    boots = []
    for _ in range(n_boot):
        boots.append(sum(rng.choice(d) for _ in range(len(d))) / len(d))
    boots.sort()
    lo, hi = boots[int(0.025 * n_boot)], boots[int(0.975 * n_boot)]
    p = 2 * min(sum(1 for b in boots if b <= 0), sum(1 for b in boots if b >= 0)) / n_boot
    return (f"paired n={len(common)} ({len(changed)} changed)  "
            f"mean delta=${mean:+.2f}/trade  total ${sum(d):+.2f}  "
            f"95% CI [${lo:+.2f}, ${hi:+.2f}]  p={min(p, 1.0):.3f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", default="4,8,12,24")
    ap.add_argument("--csv", default="")
    ap.add_argument("--skip-calibration", action="store_true")
    args = ap.parse_args()

    trades = load_trades(TRADES_CSV)
    candles = {a: load_candles(a) for a in sorted({t.asset for t in trades})}
    print(f"trades: {len(trades)}   assets: {', '.join(candles)}")
    for a, df in candles.items():
        print(f"  {a:7} 5m candles {df.index[0]:%Y-%m-%d} -> {df.index[-1]:%Y-%m-%d %H:%M} ({len(df)} bars)")

    ok, detail = calibrate(trades, candles)
    print(f"\nCALIBRATION - 4h 'asrun' replay vs the recorded run\n  {detail}")
    if not ok and not args.skip_calibration:
        print("\n  ABORT: the replay does not reproduce the recorded run.")
        return 3

    hours = [int(h) for h in args.hours.split(",")]
    runs: Dict[Tuple[str, int], dict] = {}
    print(f"\n{'scenario':12}{'cap':>5}{'n':>5}{'blk':>5}{'W':>4}{'L':>4}{'WR%':>7}"
          f"{'P&L $':>11}{'sumR':>9}{'R/trd':>8}{'medDur':>8}{'maxDur':>8}{'capbnd':>8}   results")
    print("-" * 128)
    for scenario in SCENARIOS:
        for h in hours:
            scn = run_scenario(trades, candles, h, scenario)
            runs[(scenario, h)] = scn
            s = summarise(scn)
            rmix = " ".join(f"{k}:{v}" for k, v in sorted(s["results"].items()))
            print(f"{scenario:12}{str(h)+'h':>5}{s['n']:>5}{s['blocked']:>5}{s['wins']:>4}"
                  f"{s['losses']:>4}{s['wr']:>7.1f}{s['pnl']:>+11.2f}{s['sum_r']:>+9.2f}"
                  f"{s['r_per_trade']:>+8.3f}{s['median_dur']:>8.0f}{s['max_dur']:>8.0f}"
                  f"{s['cap_bound']:>8}   {rmix}")
        print("-" * 128)

    print("\nPAIRED TEST vs the same scenario at 4h (same entries, only the cap differs)")
    for scenario in SCENARIOS:
        for h in hours[1:]:
            print(f"  {scenario:12} 4h -> {h:2}h   {paired_test(runs[(scenario, 4)], runs[(scenario, h)])}")

    print("\nPER-ASSET (serial: one position at a time)")
    print(f"{'asset':8}" + "".join(f"{str(h) + 'h':>24}" for h in hours))
    print(f"{'':8}" + "".join(f"{'n/blk':>9}{'P&L $':>9}{'R':>6}" for _ in hours))
    for asset in sorted(candles):
        line = f"{asset:8}"
        for h in hours:
            rows = [r for r in runs[("serial", h)]["rows"] if r["trade"].asset == asset]
            tk = [r for r in rows if r["taken"]]
            bl = sum(1 for r in rows if not r["taken"] and r["blocked_until"])
            rs = [x for x in (_price_r(r["sim"]) for r in tk) if x is not None]
            line += (f"{f'{len(tk)}/{bl}':>9}"
                     f"{sum(r['sim']['profit'] for r in tk):>+9.2f}{sum(rs):>+6.1f}")
        print(line)

    if args.csv:
        out = Path(args.csv)
        with out.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["scenario", "cap_hours", "asset", "entry_time", "direction",
                        "taken", "blocked_until", "rec_result", "rec_profit",
                        "sim_result", "sim_profit", "sim_exit_time", "sim_exit_price",
                        "hold_min", "delta_vs_recorded"])
            for (scenario, h), scn in runs.items():
                for r in scn["rows"]:
                    t, s = r["trade"], r["sim"]
                    hold = ((pd.Timestamp(s["exit_time"]) - pd.Timestamp(t.entry_time)).total_seconds() / 60
                            if s and s.get("exit_time") else "")
                    w.writerow([
                        scenario, h, t.asset, t.entry_time.isoformat(), t.direction, r["taken"],
                        r["blocked_until"].isoformat() if r["blocked_until"] else "",
                        t.rec_result, f"{t.rec_profit:.4f}",
                        s["result"] if s else "", f"{s['profit']:.4f}" if s else "",
                        pd.Timestamp(s["exit_time"]).isoformat() if s and s.get("exit_time") else "",
                        f"{s['exit_price']:.6f}" if s and s.get("exit_price") is not None else "",
                        f"{hold:.0f}" if hold != "" else "",
                        f"{(s['profit'] if s else 0.0) - t.rec_profit:.4f}",
                    ])
        print(f"\nper-trade detail -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
