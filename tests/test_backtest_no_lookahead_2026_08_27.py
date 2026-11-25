"""The three invariants the 2026-08-27 rewrite exists to hold.

Context: the 2026-07-13 -> 07-29 freeze backtest opened 18 of its 105 positions
while another position on the same asset was still live, filled one setup twice,
and took the one-position-at-a-time branch on 0 of 1,756 checkpoints. The cause
was look-ahead: ``_execute_iteration`` was handed hours of future bars and
resolved each position inside the iteration that opened it.

These tests fail if any of that can happen again:

  1. no look-ahead   — nothing reachable returns a bar stamped after the clock
  2. one position    — a bar that closes a position cannot also open one, and no
                       second position can be opened while one is live
  3. live parity     — the exit ladder runs live's steps in live's order
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from packages.core.backtest_armed_strategy import ArmedStrategy
from packages.core.backtest_bar_feed import ClosedBarFeed
from packages.core.backtest_position_monitor import (
    OpenPosition,
    PositionMonitor,
    unrealised_snapshot,
)
from packages.core.backtest_runner import BacktestRunner
from packages.core.config import RUNNER_MAX_HOURS, TTL_EXTEND_MIN_R

import pandas as pd

START = datetime(2026, 7, 13, 0, 0, tzinfo=timezone.utc)


def make_bars(count: int, *, start: datetime = START, price: float = 100.0,
              step: float = 0.0, high_pad: float = 0.5, low_pad: float = 0.5):
    """A clean 5-minute series with no gaps."""
    bars = []
    for i in range(count):
        close = price + step * i
        bars.append({
            "time": start + timedelta(minutes=5 * i),
            "open": close,
            "high": close + high_pad,
            "low": close - low_pad,
            "close": close,
        })
    return bars


def make_feed(bars):
    index = {pd.Timestamp(b["time"]): i for i, b in enumerate(bars)}
    return ClosedBarFeed(bars, sorted(index.keys()), index)


def never_closing(_asset, now=None):
    return False, ""


# ─────────────────────────────────────────────────────────────────────────
# 1. No look-ahead
# ─────────────────────────────────────────────────────────────────────────

class TestNoLookAhead:
    def test_feed_never_returns_a_bar_after_the_clock(self):
        bars = make_bars(50)
        feed = make_feed(bars)
        now = bars[10]["time"]

        assert feed.last_bar_closed_at_or_before(now)["time"] == now - timedelta(minutes=5)
        assert feed.index_of_last_bar_closed_at_or_before(now) == 9

        window = feed.bars_closed_between(bars[5]["time"], now)
        assert [b["time"] for b in window] == [b["time"] for b in bars[5:10]]
        assert all(b["time"] + timedelta(minutes=5) <= now for b in window)

    def test_bar_between_two_closes_resolves_to_the_earlier_one(self):
        """A clock inside a bar has not seen that bar close yet."""
        bars = make_bars(10)
        feed = make_feed(bars)
        mid_bar = bars[3]["time"] + timedelta(minutes=2)
        assert feed.last_bar_closed_at_or_before(mid_bar)["time"] == bars[2]["time"]
        assert [b["time"] for b in feed.bars_closed_between(bars[3]["time"], mid_bar)] == []

    def test_window_is_empty_when_the_clock_has_not_advanced(self):
        bars = make_bars(10)
        feed = make_feed(bars)
        assert feed.bars_closed_between(bars[4]["time"], bars[4]["time"]) == []

    def test_history_excludes_the_bar_being_evaluated(self):
        bars = make_bars(50)
        feed = make_feed(bars)
        history = feed.history_before_index(20, 5)
        assert [b["time"] for b in history] == [b["time"] for b in bars[15:20]]
        assert bars[20]["time"] not in [b["time"] for b in history]

    def test_history_refuses_rather_than_padding_when_short(self):
        feed = make_feed(make_bars(50))
        assert feed.history_before_index(3, 40) is None

    def test_advance_stops_at_the_checkpoint(self):
        """The bar clock must not process a single bar past `now`."""
        bars = make_bars(60)
        runner = BacktestRunner(
            asset="XAUUSD", start_date=START, end_date=START + timedelta(days=1),
        )
        runner._bar_feed = make_feed(bars)
        runner._position_monitor = PositionMonitor(never_closing)

        seen = []
        armed = ArmedStrategy(
            code="", signal={}, cycle_context={},
            armed_at=bars[0]["time"],
            ttl_expiry=bars[0]["time"] + timedelta(hours=99),
            armed_at_iteration=1,
        )
        runner._run_armed_strategy_on_bar = (
            lambda a, bar, idx: seen.append(bar["time"]) or None
        )

        now = bars[12]["time"]
        advance = runner._advance_closed_bars_to(
            now,
            open_position=None,
            armed_strategy=armed,
            last_processed_bar_time=bars[0]["time"],
            entry_iteration=2,
            refuse_signature=None,
        )
        assert seen == [b["time"] for b in bars[:12]]
        assert max(seen) + timedelta(minutes=5) <= now
        assert advance.last_processed_bar_time == now


# ─────────────────────────────────────────────────────────────────────────
# 2. One position at a time
# ─────────────────────────────────────────────────────────────────────────

class TestOnePositionAtATime:
    def _runner_with(self, bars):
        runner = BacktestRunner(
            asset="XAUUSD", start_date=START, end_date=START + timedelta(days=1),
        )
        runner._bar_feed = make_feed(bars)
        runner._position_monitor = PositionMonitor(never_closing)
        return runner

    def _position(self, bars, *, entry_index=0, expiry_hours=4):
        entry_time = bars[entry_index]["time"]
        return OpenPosition(
            asset="XAUUSD",
            trade_state={
                "direction": "long", "entry": 100.0, "sl": 99.0,
                "initial_stop": 99.0, "sl_moved": False,
                "tp1_hit": False, "tp2_hit": False, "quantity": 0.1,
                "asset": "XAUUSD",
            },
            rules={"tp1": 101.0, "tp": 102.0, "move_sl_to_be": True, "runner_enabled": False},
            quantity=0.1,
            prediction={"direction": "long"},
            entry_time=entry_time,
            entry_iteration=1,
            expiry=entry_time + timedelta(hours=expiry_hours),
            last_evaluated_bar_time=entry_time,
        )

    def test_armed_strategy_is_never_run_while_a_position_is_open(self):
        bars = make_bars(30)
        runner = self._runner_with(bars)
        runner._run_armed_strategy_on_bar = lambda *a, **k: pytest.fail(
            "strategy evaluated while a position was open"
        )
        armed = ArmedStrategy(
            code="", signal={}, cycle_context={},
            armed_at=bars[0]["time"],
            ttl_expiry=bars[0]["time"] + timedelta(hours=99),
            armed_at_iteration=1,
        )
        advance = runner._advance_closed_bars_to(
            bars[20]["time"],
            open_position=self._position(bars),
            armed_strategy=armed,
            last_processed_bar_time=bars[0]["time"],
            entry_iteration=2,
            refuse_signature=None,
        )
        assert advance.open_position is not None
        assert advance.opened == []

    def test_a_bar_that_closes_a_position_does_not_also_open_one(self):
        """Live's monitor and its cycle are different clocks."""
        bars = make_bars(30)
        runner = self._runner_with(bars)
        closing_bar = bars[5]["time"]

        def close_on_bar_five(position, bar):
            if bar["time"] == closing_bar:
                return runner._position_monitor.__class__._closed(
                    runner._position_monitor, position,
                    position.simulator_for_replay(), "SL", 99.0, bar["time"],
                )
            return None

        runner._position_monitor.step = close_on_bar_five
        evaluated = []
        runner._run_armed_strategy_on_bar = (
            lambda a, bar, idx: evaluated.append(bar["time"]) or None
        )
        armed = ArmedStrategy(
            code="", signal={}, cycle_context={},
            armed_at=bars[0]["time"],
            ttl_expiry=bars[0]["time"] + timedelta(hours=99),
            armed_at_iteration=1,
        )
        advance = runner._advance_closed_bars_to(
            bars[10]["time"],
            open_position=self._position(bars),
            armed_strategy=armed,
            last_processed_bar_time=bars[0]["time"],
            entry_iteration=2,
            refuse_signature=None,
        )
        assert len(advance.closes) == 1
        # The closing bar itself is never offered to the strategy.
        assert closing_bar not in evaluated
        # Bars after it are, because the slot is free again.
        assert evaluated == [b["time"] for b in bars[6:10]]

    def test_a_fired_strategy_is_consumed_even_when_a_gate_rejects_it(self):
        """Otherwise one strategy could fill repeatedly on later bars."""
        bars = make_bars(30)
        runner = self._runner_with(bars)
        calls = []

        def trigger_every_bar(a, bar, idx):
            calls.append(bar["time"])
            return {"direction": "long"}

        runner._run_armed_strategy_on_bar = trigger_every_bar
        runner._open_position_from_trigger = (
            lambda *a, **k: (None, "rejected by a gate")
        )
        armed = ArmedStrategy(
            code="", signal={}, cycle_context={},
            armed_at=bars[0]["time"],
            ttl_expiry=bars[0]["time"] + timedelta(hours=99),
            armed_at_iteration=1,
        )
        advance = runner._advance_closed_bars_to(
            bars[10]["time"],
            open_position=None,
            armed_strategy=armed,
            last_processed_bar_time=bars[0]["time"],
            entry_iteration=2,
            refuse_signature=None,
        )
        assert len(calls) == 1
        assert advance.armed_strategy is None
        assert advance.rejections == ["rejected by a gate"]

    def test_expired_strategy_is_dropped_at_the_bar_its_ttl_passes(self):
        bars = make_bars(30)
        runner = self._runner_with(bars)
        runner._run_armed_strategy_on_bar = lambda *a, **k: pytest.fail(
            "expired strategy was evaluated"
        )
        armed = ArmedStrategy(
            code="", signal={}, cycle_context={},
            armed_at=bars[0]["time"],
            ttl_expiry=bars[3]["time"],
            armed_at_iteration=1,
        )
        advance = runner._advance_closed_bars_to(
            bars[10]["time"],
            open_position=None,
            armed_strategy=armed,
            last_processed_bar_time=bars[2]["time"],
            entry_iteration=2,
            refuse_signature=None,
        )
        assert advance.armed_strategy is None

    def test_first_checkpoint_only_sets_a_starting_point(self):
        bars = make_bars(30)
        runner = self._runner_with(bars)
        runner._run_armed_strategy_on_bar = lambda *a, **k: pytest.fail(
            "evaluated a bar before anything was armed"
        )
        advance = runner._advance_closed_bars_to(
            bars[10]["time"],
            open_position=None,
            armed_strategy=None,
            last_processed_bar_time=None,
            entry_iteration=1,
            refuse_signature=None,
        )
        assert advance.last_processed_bar_time == bars[10]["time"]
        assert advance.closes == [] and advance.opened == []


# ─────────────────────────────────────────────────────────────────────────
# 3. The exit ladder runs live's steps in live's order
# ─────────────────────────────────────────────────────────────────────────

class TestPositionMonitorParity:
    def _position(self, *, entry=100.0, sl=99.0, tp1=101.0, tp2=102.0,
                  expiry_hours=4, entry_time=START, runner_enabled=False):
        return OpenPosition(
            asset="XAUUSD",
            trade_state={
                "direction": "long", "entry": entry, "sl": sl,
                "initial_stop": sl, "sl_moved": False,
                "tp1_hit": False, "tp2_hit": False, "quantity": 1.0,
                "asset": "XAUUSD",
            },
            rules={"tp1": tp1, "tp": tp2, "move_sl_to_be": True,
                   "runner_enabled": runner_enabled, "runner_trail_dist": 0.5},
            quantity=1.0,
            prediction={"direction": "long"},
            entry_time=entry_time,
            entry_iteration=1,
            expiry=entry_time + timedelta(hours=expiry_hours),
            last_evaluated_bar_time=entry_time,
        )

    def test_expiry_closes_at_market_on_the_bar_it_is_reached(self):
        monitor = PositionMonitor(never_closing)
        position = self._position(expiry_hours=1)
        bars = make_bars(30, start=START + timedelta(minutes=5))
        closed = monitor.advance(position, bars)
        assert closed is not None
        assert closed.outcome["result"] == "Timeout"
        assert closed.bar_time == position.entry_time + timedelta(hours=1)

    def test_pre_close_guard_flattens_before_the_gap(self):
        def closing_at(asset, now=None):
            return (now.hour == 20 and now.minute >= 50), "daily_maintenance"

        monitor = PositionMonitor(closing_at)
        entry = datetime(2026, 7, 13, 20, 0, tzinfo=timezone.utc)
        position = self._position(entry_time=entry, expiry_hours=4)
        closed = monitor.advance(
            position, make_bars(30, start=entry + timedelta(minutes=5))
        )
        assert closed is not None
        assert closed.outcome["exit_reason"] == "PRE_CLOSE_DAILY_MAINTENANCE"
        assert closed.bar_time.hour == 20 and closed.bar_time.minute == 50

    def test_green_risk_free_position_earns_a_ttl_extension(self):
        """Live evaluates maybe_extend_expiry on every bar; so must the backtest."""
        monitor = PositionMonitor(never_closing)
        position = self._position(expiry_hours=1, tp1=1e9, tp2=1e9)
        # Risk-free and comfortably beyond TTL_EXTEND_MIN_R at the expiry bar.
        position.trade_state["sl"] = 100.5
        bars = make_bars(
            40, start=START + timedelta(minutes=5),
            price=100.0 + (TTL_EXTEND_MIN_R + 1.0), step=0.0,
            high_pad=0.01, low_pad=0.01,
        )
        monitor.advance(position, bars)
        assert position.expiry > position.entry_time + timedelta(hours=1)

    def test_a_loser_at_expiry_is_not_extended(self):
        monitor = PositionMonitor(never_closing)
        position = self._position(expiry_hours=1, tp1=1e9, tp2=1e9)
        bars = make_bars(
            40, start=START + timedelta(minutes=5), price=99.5,
            high_pad=0.01, low_pad=0.01,
        )
        closed = monitor.advance(position, bars)
        assert closed is not None
        assert closed.outcome["result"] == "Timeout"
        assert position.expiry == position.entry_time + timedelta(hours=1)

    def test_tp2_promotes_the_expiry_to_the_runner_window(self):
        monitor = PositionMonitor(never_closing)
        position = self._position(expiry_hours=4, runner_enabled=True)
        bars = make_bars(
            10, start=START + timedelta(minutes=5), price=102.0,
            high_pad=0.05, low_pad=0.05,
        )
        monitor.advance(position, bars)
        assert position.expiry == position.entry_time + timedelta(hours=RUNNER_MAX_HOURS)

    def test_ladder_state_survives_across_catch_up_windows(self):
        """A fresh simulator per checkpoint would un-book realised partials."""
        monitor = PositionMonitor(never_closing)
        position = self._position(expiry_hours=8, runner_enabled=True)
        # A bar whose range straddles TP1 (101.0) books the partial.
        first = make_bars(3, start=START + timedelta(minutes=5), price=101.0,
                          high_pad=0.05, low_pad=0.05)
        assert monitor.advance(position, first) is None
        assert position.trade_state["tp1_hit"] is True
        booked = position.simulator_for_replay().result.tp1_pnl
        assert booked != 0.0

        second = make_bars(3, start=START + timedelta(minutes=25), price=101.3,
                           high_pad=0.05, low_pad=0.05)
        monitor.advance(position, second)
        assert position.simulator_for_replay().result.tp1_pnl == booked, (
            "TP1 partial was re-booked or lost between catch-up windows"
        )

    def test_open_snapshot_never_books_profit(self):
        position = self._position()
        snapshot = unrealised_snapshot(position, 100.7)
        assert snapshot["profit"] == 0.0
        assert snapshot["result"] == "Open"
