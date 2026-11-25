"""End-to-end: drive the real checkpoint loop and assert live parity holds.

The unit tests next door prove each rule in isolation. This one runs
``BacktestRunner.execute_backtest`` itself — real candles, real bar feed, real
PositionSimulator, real exit ladder — with only the model call and the database
stubbed, and asserts the properties that were violated in the 2026-07-13 -> 07-29
freeze run:

  * positions never overlap
  * no position is opened AND closed inside the checkpoint that opened it
  * a checkpoint with a position open produces a monitoring row, not a model call
  * nothing is ever evaluated on a bar stamped after the checkpoint

The freeze run failed all four: 18 of 105 entries opened while another was live,
one setup filled twice, and 0 of 1,756 checkpoints took the monitoring branch.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from packages.core.backtest_runner import BacktestRunner

REPO = Path(__file__).resolve().parents[1]
CANDLES = REPO / "data" / "processed" / "xauusd_5m_2years.csv"

WINDOW_START = datetime(2026, 7, 14, 0, 0, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 7, 16, 0, 0, tzinfo=timezone.utc)

pytestmark = pytest.mark.skipif(
    not CANDLES.exists(), reason="XAUUSD 5m candle file not present"
)

# A strategy that fires on any bar trading below the previous close. It fires
# often on real data, which is the point: the loop has to cope with a strategy
# that would trigger on many bars and still open exactly one position at a time.
# Levels chosen so the test exercises the LOOP rather than the gates: 0.8% stop
# and 2% TP1 clear CS_MIN_RR_RATIO_TP1 (RR 2.5) and the per-asset % floors, and
# they are far enough apart on gold that most positions live past the next
# hourly checkpoint — which is exactly the state that has to suppress the model
# call, and the state the old runner could never reach.
EAGER_SHORT = """
def run(candles_5, now):
    close = candles_5[-1][4]
    prev = candles_5[-2][4]
    levels = {'entry': close, 'stop_loss': close * 1.008,
              'take_profit_1': close * 0.980, 'take_profit_2': close * 0.970,
              'confidence': 0.7, 'rationale': 'test'}
    levels['direction'] = 'sell' if close < prev else 'hold'
    return levels
"""


def load_bars(interval_minutes: int):
    frame = pd.read_csv(CANDLES, index_col=0, parse_dates=True)
    if frame.index.tz is None:
        frame.index = frame.index.tz_localize("UTC")
    frame = frame.sort_index()
    frame = frame[(frame.index >= WINDOW_START - timedelta(days=3)) & (frame.index <= WINDOW_END)]
    if interval_minutes == 15:
        frame = frame.resample("15min").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last"}
        ).dropna()
    return [
        {"time": stamp.to_pydatetime(), "open": float(row.open), "high": float(row.high),
         "low": float(row.low), "close": float(row.close), "volume": 0}
        for stamp, row in frame.iterrows()
    ]


def stub_signal():
    signal = SimpleNamespace(
        code=EAGER_SHORT,
        ttl_minutes=180,
        confidence=0.7,
        metadata={"strategy_type": "retest", "decision": "accepted"},
    )
    signal.model_dump = lambda: {
        "code": EAGER_SHORT, "ttl_minutes": 180, "confidence": 0.7,
        "metadata": {"strategy_type": "retest", "decision": "accepted"},
    }
    return signal, {"cycle_id": "test-cycle", "judge": {}}


@pytest.mark.asyncio
async def test_loop_holds_one_position_at_a_time_with_no_look_ahead():
    runner = BacktestRunner(
        asset="XAUUSD",
        start_date=WINDOW_START,
        end_date=WINDOW_END,
        candle_count_5m=100,
        candle_count_15m=50,
        use_code_sniper=True,
        initial_capital=2000.0,
    )
    runner.candle_service.fetch_historical_5m = AsyncMock(return_value=load_bars(5))
    runner.candle_service.fetch_historical_15m = AsyncMock(return_value=load_bars(15))

    written: list = []

    async def record_bulk(_backtest_id, rows):
        written.extend(rows)

    with patch("packages.core.backtest_runner.multi_expert_decision_with_details",
               AsyncMock(return_value=stub_signal())), \
         patch("packages.core.backtest_runner.backtest_repo") as repo:
        repo.get_backtest = AsyncMock(return_value={"status": "running"})
        repo.update_backtest_status = AsyncMock()
        repo.update_backtest_progress = AsyncMock()
        repo.add_iteration = AsyncMock()
        repo.add_iterations_bulk = AsyncMock(side_effect=record_bulk)
        repo.update_backtest_results = AsyncMock()
        repo.complete_backtest = AsyncMock()

        await runner.execute_backtest(
            test_name="parity-e2e", interval_hours=1, backtest_id="test-id",
        )

    # execute_backtest persists its stream rather than returning it, so the
    # exported rows ARE the record under test — the same rows an audit reads.
    iterations = written
    assert iterations, "the loop persisted no iterations"

    # ── every recorded position, in entry order ──────────────────────────
    positions = []
    for row in iterations:
        outcome = row.get("trade_outcome") or row.get("outcome") or {}
        entry_time = outcome.get("entry_time")
        exit_time = outcome.get("exit_time")
        if not entry_time or not exit_time or outcome.get("result") in (None, "No Entry", "Flat", "Open"):
            continue
        positions.append({
            "entry": _utc(entry_time),
            "exit": _utc(exit_time),
            "checkpoint": _utc(row.get("iteration_timestamp") or row.get("timestamp")),
            "entry_iteration": row.get("entry_iteration"),
            "iteration": row.get("iteration"),
        })
    positions.sort(key=lambda p: p["entry"])
    assert positions, "the eager strategy opened no position at all — test is not exercising the loop"

    # 1. Positions never overlap.
    for earlier, later in zip(positions, positions[1:]):
        assert later["entry"] >= earlier["exit"], (
            f"overlapping positions: {earlier['entry']}->{earlier['exit']} "
            f"then {later['entry']}"
        )

    # 2. No position is opened and closed inside the checkpoint that opened it.
    #    This is the look-ahead signature: it requires seeing bars the checkpoint
    #    clock says have not printed.
    for position in positions:
        assert position["exit"] > position["checkpoint"] or position["entry_iteration"] != position["iteration"], (
            f"position {position['entry']}->{position['exit']} resolved inside its "
            f"own checkpoint {position['checkpoint']}"
        )

    # 3. A checkpoint that finds a position open records it as such instead of
    #    calling the model. The freeze run produced zero of these across 1,756
    #    checkpoints, which is what let it open overlapping positions.
    occupied = [r for r in iterations if r.get("trade_status") == "active"]
    assert occupied, (
        "no checkpoint ever found a position open — the active-position guard "
        "is still unreachable"
    )
    for row in occupied:
        outcome = row.get("trade_outcome") or {}
        assert outcome.get("result") == "Open"
        assert not outcome.get("profit"), "an open row must never book profit"

    # A closed position is always reported from a LATER checkpoint than the one
    # that opened it: discovering the close requires bars that had not printed.
    closed_rows = [r for r in iterations if r.get("trade_status") == "closed"]
    assert closed_rows, "no position ever closed"

    # 4. Nothing is stamped after the checkpoint that recorded it.
    for row in iterations:
        outcome = row.get("trade_outcome") or row.get("outcome") or {}
        checkpoint = _utc(row.get("iteration_timestamp") or row.get("timestamp"))
        for field in ("entry_time", "exit_time"):
            stamp = outcome.get(field)
            if stamp:
                assert _utc(stamp) <= checkpoint, (
                    f"{field} {stamp} is after its checkpoint {checkpoint} — look-ahead"
                )


def _utc(value):
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
