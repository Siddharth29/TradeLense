"""Exit-config sweep: replay recorded entries under different exit rules.

The question this answers
------------------------
The 97-trade freeze sample runs at -0.033R per trade. WP-04 says direction is
not the limiter, so the money is lost between a correct thesis and a banked
exit. This replays the *same entries* through the *same exit engine* with
different exit configuration, and reports what each config would have banked.

It uses ``trade_monitor.evaluate_trade`` — the real production function, not a
reimplementation. That matters: a hand-written simulator would be testing my
understanding of the exit rules rather than the rules themselves.

CALIBRATION GATE (read this before believing any number)
--------------------------------------------------------
The sweep runs a ``baseline`` config first — the settings that were actually
live during the window — and compares its simulated R against the broker-true
``real_r`` already recorded for each trade. **If baseline does not track the
recorded outcomes, every counterfactual below it is meaningless** and the run
aborts. Verified simulators earn trust; assumed ones cost money.

What is and is not swept
------------------------
Swept (fully supported by the recorded data):
  - **TTL**  {60,90,120} -> {90,180,240} -> unlimited. A time cutoff, so all
    that is needed is the entry timestamp and the bars after it.
  - **trail_arm_on_touch**  close-basis vs intrabar-touch arming. Passed
    per-call through ``rules``, so no global config is mutated.
  - **LP-14 filter**  drop entries whose ER_2h/ER_1h < 0.4.

NOT swept, and why:
  - **TP2 partial fraction.** TP2 price levels are not stored anywhere
    structured. ``event:trade_open`` carries a single ``take_profit`` (TP1);
    the TP2 level appears only inside the judge's free-text rationale. Sweeping
    it would mean regexing prose, and a mis-parsed level silently changes the
    result. It needs a real field first.

    python scripts/audit/exit_config_sweep.py
    python scripts/audit/exit_config_sweep.py --candles data/processed
"""

from __future__ import annotations

import argparse
import csv
import statistics as st
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd  # noqa: E402

from packages.core.candle_repository import (  # noqa: E402
    CandleRepository, CandleSpec, CsvCandleSource,
)
from packages.core.candles import Candle  # noqa: E402
from packages.core.config import DEFAULT_TRAIL_STEPS  # noqa: E402
from packages.core.trade_monitor import (  # noqa: E402
    EXIT_SL, EXIT_TP, MOVE_SL_TO_BE, PARTIAL_TP2, TRAIL_SL, evaluate_trade,
)

REPLAY_CSV = "data/research/label_tape_replay_2026-07-25.csv"

# Correlation between simulated and recorded R below which the sweep aborts.
# Set from what a faithful replay should achieve, not from what a given run
# happens to produce — moving it to make a run pass defeats the gate.
MIN_CALIBRATION_CORR = 0.90


@dataclass(frozen=True)
class ExitConfig:
    name: str
    ttl_minutes: Optional[int]      # None = no time cutoff
    trail_arm_on_touch: bool
    lp14_filter: bool = False


CONFIGS: tuple[ExitConfig, ...] = (
    ExitConfig("baseline (live during window)", 120, False, False),
    ExitConfig("TTL 240", 240, False, False),
    ExitConfig("TTL unlimited", None, False, False),
    ExitConfig("touch-arm", 120, True, False),
    ExitConfig("TTL 240 + touch-arm", 240, True, False),
    ExitConfig("TTL 240 + touch-arm + LP-14", 240, True, True),
    ExitConfig("baseline + LP-14", 120, False, True),
)


# TP1 books half the position and moves the stop to BE+0.5R
# (services/trade_monitor.py:1128). Modelled explicitly: ignoring it would
# overstate every winner that tags TP1 and keeps running, which is most of them.
TP1_PARTIAL_FRACTION = 0.5

# At the broker minimum lot the position cannot be split, so the partial is
# throttled and the whole position rides to the final exit. This is SP-03's
# min-lot finding and OBSERVATION_PARAMETERS B4 (`runner_possible`) — without
# it the sweep cannot tell "capped by design" from "gave it back".
MIN_LOT = 0.01


@dataclass
class Entry:
    """One recorded live entry, as the broker saw it."""

    time: datetime
    symbol: str
    side: str
    entry: float
    sl: float
    tp: float
    qty: float
    real_r: float
    er_ratio: Optional[float]

    @property
    def direction(self) -> str:
        return "long" if self.side.lower() == "buy" else "short"

    @property
    def risk(self) -> float:
        return abs(self.entry - self.sl)

    @property
    def can_split(self) -> bool:
        """False at min lot — no partial, no runner, whole position rides."""
        return self.qty > MIN_LOT


def load_entries(path: str) -> List[Entry]:
    out: List[Entry] = []
    for row in csv.DictReader(open(path)):
        if not row["real_r"].strip() or not row["sl"].strip():
            continue
        ratio = row.get("tape_er_ratio_2h_1h", "").strip()
        ts = pd.Timestamp(row["entry_time"])
        out.append(
            Entry(
                time=(ts if ts.tzinfo else ts.tz_localize("UTC")).to_pydatetime(),
                symbol=row["symbol"],
                side=row["side"],
                entry=float(row["entry"]),
                sl=float(row["sl"]),
                tp=float(row["tp"]),
                qty=float(row["qty"] or 0),
                real_r=float(row["real_r"]),
                er_ratio=float(ratio) if ratio else None,
            )
        )
    return out


def simulate(entry: Entry, bars: Sequence[Candle], cfg: ExitConfig) -> Optional[float]:
    """Replay one entry through the production exit engine. Returns realised R.

    Returns None when there are no bars after the entry — a missing result is
    reported as a gap rather than silently counted as a scratch, which would
    bias every config toward zero.
    """
    risk = entry.risk
    if risk <= 0:
        return None

    after = [b for b in bars if pd.Timestamp(b.time) >= pd.Timestamp(entry.time)]
    if not after:
        return None

    deadline = entry.time + timedelta(minutes=cfg.ttl_minutes) if cfg.ttl_minutes else None

    state: Dict = {
        "direction": entry.direction,
        "sl": entry.sl,
        "entry": entry.entry,
        "tp1_hit": False,
        "tp2_hit": False,
        "trail_consec": 0,
        "trail_step": 0,
    }
    rules: Dict = {
        "tp1": entry.tp,
        "tp": None,               # TP2 level unavailable — see module docstring
        "runner_enabled": False,
        "trail_arm_on_touch": cfg.trail_arm_on_touch,
        "trail_config": DEFAULT_TRAIL_STEPS,
        "entry": entry.entry,
        "risk": risk,
    }

    def r_of(price: float) -> float:
        move = price - entry.entry if entry.direction == "long" else entry.entry - price
        return move / risk

    # Realised R accumulates as the position is closed in pieces. ``open_frac``
    # is what is still riding; ``banked`` is what has already been taken off.
    banked = 0.0
    open_frac = 1.0

    def close_all(price: float) -> float:
        return banked + open_frac * r_of(price)

    for i, bar in enumerate(after):
        if deadline is not None and pd.Timestamp(bar.time).to_pydatetime() >= deadline:
            return close_all(bar.open)     # TTL closes at market on the next bar

        action = evaluate_trade(
            state, bar.close, bar.high, bar.low, rules, is_entry_candle=(i == 0)
        )

        if action == EXIT_SL:
            return close_all(state["sl"])
        if action in (EXIT_TP, PARTIAL_TP2):
            return close_all(entry.tp)
        if action == MOVE_SL_TO_BE:
            state["tp1_hit"] = True
            if entry.can_split:
                # Book the TP1 half; the rest rides on a BE+0.5R stop.
                banked += TP1_PARTIAL_FRACTION * r_of(entry.tp)
                open_frac -= TP1_PARTIAL_FRACTION
            # At min lot the partial is throttled — nothing is booked and the
            # whole position continues, which is exactly the SP-03 limiter.
            be = entry.entry + 0.5 * risk if entry.direction == "long" else entry.entry - 0.5 * risk
            state["sl"] = be
        elif action == TRAIL_SL:
            step = state.get("pending_sl")
            if step is not None:
                state["sl"] = step

    return close_all(after[-1].close)      # ran out of bars — mark to last close


def calibrate(rows: List[tuple[Entry, Optional[float]]]) -> tuple[bool, str]:
    """Compare baseline simulation against broker-true recorded R."""
    pairs = [(e.real_r, sim) for e, sim in rows if sim is not None]
    if len(pairs) < 20:
        return False, f"only {len(pairs)} simulable trades — too few to calibrate"
    actual = [a for a, _ in pairs]
    sim = [s for _, s in pairs]
    try:
        corr = st.correlation(actual, sim)
    except st.StatisticsError as exc:
        return False, f"correlation undefined: {exc}"
    mae = st.mean(abs(a - s) for a, s in pairs)
    ok = corr >= MIN_CALIBRATION_CORR
    return ok, (
        f"n={len(pairs)}  corr={corr:+.3f} (need >= {MIN_CALIBRATION_CORR})  "
        f"mean|sim-actual|={mae:.3f}R  "
        f"sumR actual={sum(actual):+.2f} sim={sum(sim):+.2f}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--entries", default=REPLAY_CSV)
    ap.add_argument("--candles", default="data/processed")
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--skip-calibration", action="store_true",
                    help="run counterfactuals even if baseline does not reproduce reality (debug only)")
    args = ap.parse_args()

    entries = load_entries(args.entries)
    print(f"entries: {len(entries)} from {args.entries}")

    # ── Load + validate candles once per symbol ──────────────────────────
    repo = CandleRepository(CsvCandleSource(args.candles), root=args.candles)
    bars_by_symbol: Dict[str, List[Candle]] = {}
    missing: List[str] = []
    for symbol in sorted({e.symbol for e in entries}):
        sym_entries = [e for e in entries if e.symbol == symbol]
        spec = CandleSpec(
            symbol, args.interval,
            min(e.time for e in sym_entries),
            max(e.time for e in sym_entries) + timedelta(hours=12),
        )
        bars, report = repo.load(spec)
        if not bars:
            missing.append(symbol)
            continue
        if not report.ok:
            print(f"  WARN {report.summary()}")
        bars_by_symbol[symbol] = bars

    if missing:
        print(f"\nNO CANDLES for: {', '.join(missing)}")
        print(f"Run:  python scripts/dl_backtest_window.py --out {args.candles}")
        print("(on the Windows box with MT5 logged in)")
        return 2

    # ── Calibration gate ─────────────────────────────────────────────────
    base = CONFIGS[0]
    base_rows = [(e, simulate(e, bars_by_symbol[e.symbol], base)) for e in entries]
    ok, detail = calibrate(base_rows)
    print(f"\nCALIBRATION ({base.name})\n  {detail}")
    if not ok and not args.skip_calibration:
        print("\n  ABORT: baseline does not reproduce recorded outcomes.")
        print("  Every counterfactual below would be measuring the simulator, not the strategy.")
        print("  Fix the replay (candle alignment, trail ladder, TP levels) before trusting a sweep.")
        return 3
    if not ok:
        print("  !! calibration FAILED but --skip-calibration was passed — numbers below are NOT trustworthy")

    # ── Sweep ────────────────────────────────────────────────────────────
    cost = 0.378 + 0.46  # commission + measured API cost per trade
    print(f"\n{'config':34}{'n':>5}{'W%':>7}{'sumR':>9}{'R/trade':>10}"
          f"{'$7.5/R':>11}{'$25/R':>11}")
    print("-" * 87)

    for cfg in CONFIGS:
        rs: List[float] = []
        for e in entries:
            if cfg.lp14_filter and (e.er_ratio is None or e.er_ratio < 0.4):
                continue
            r = simulate(e, bars_by_symbol[e.symbol], cfg)
            if r is not None:
                rs.append(r)
        if not rs:
            print(f"{cfg.name:34}{'—':>5}")
            continue
        n, s = len(rs), sum(rs)
        w = 100 * sum(1 for r in rs if r > 0) / n
        print(f"{cfg.name:34}{n:5}{w:7.1f}{s:+9.2f}{s/n:+10.4f}"
              f"{s*7.5 - n*cost:+11.2f}{s*25 - n*cost:+11.2f}")

    print("\nBreak-even R/trade:  $7.5/R = +0.1117   |   $25/R = +0.0335")
    print("Costs applied: $0.378 commission + $0.46 API (measured) = $0.838/trade")
    print("\nA config only counts if it clears break-even by more than the sample's")
    print("error bar. At n~97 that bar is about +/-0.21R — see docs/runbooks/backtest-sweep.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
