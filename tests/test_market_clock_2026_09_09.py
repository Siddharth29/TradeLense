"""MT5 timestamps denote opens; decisions and fills require closed bars."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from packages.core.backtest_armed_strategy import ArmedStrategy
from packages.core.backtest_bar_feed import ClosedBarFeed
from packages.core.backtest_position_monitor import OpenPosition, PositionMonitor, position_signature
from packages.core.backtest_runner import BacktestRunner


NOW = datetime(2026, 7, 14, 12, tzinfo=timezone.utc)


def bars(minutes, count=160):
    return [{"time": NOW + timedelta(minutes=minutes * (i - count + 2)),
             "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
             "volume": 1.0} for i in range(count)]


def indexed_feed(data):
    index = {pd.Timestamp(b["time"]): i for i, b in enumerate(data)}
    return ClosedBarFeed(data, sorted(index), index)


def runner():
    return BacktestRunner("BTCUSD", NOW, NOW + timedelta(days=1),
                          candle_count_5m=100, candle_count_15m=50)


def test_feed_requires_the_complete_bar_not_just_its_timestamp():
    data = bars(5)
    feed = indexed_feed(data)
    assert feed.last_bar_closed_at_or_before(data[0]["time"]) is None
    assert feed.last_bar_closed_at_or_before(NOW)["time"] == NOW - timedelta(minutes=5)
    assert feed.last_bar_closed_at_or_before(NOW + timedelta(minutes=4))["time"] == NOW - timedelta(minutes=5)
    assert [b["time"] for b in feed.bars_closed_between(NOW, NOW + timedelta(minutes=5))] == [NOW]


@pytest.mark.asyncio
async def test_model_receives_only_m5_and_m15_bars_available_at_checkpoint(monkeypatch):
    r = runner()
    r._cached_5m_candles, r._cached_15m_candles = bars(5), bars(15)
    r._sorted_5m_times = [pd.Timestamp(b["time"]) for b in r._cached_5m_candles]
    r._sorted_15m_times = [pd.Timestamp(b["time"]) for b in r._cached_15m_candles]
    r._candle_5m_index_map = {t: i for i, t in enumerate(r._sorted_5m_times)}
    captured = {}

    async def model(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(signal="flat", model_dump=lambda: {"signal": "flat"}), {}

    monkeypatch.setattr("packages.core.backtest_runner.multi_expert_decision_with_details", model)
    monkeypatch.setattr("packages.core.backtest_runner.backtest_repo",
                        SimpleNamespace(add_iteration=AsyncMock()))
    await r._execute_iteration(pd.Timestamp(NOW), "clock-test", 1)
    assert captured["c5_superset"][-1]["time"] == NOW - timedelta(minutes=5)
    assert captured["c15_superset"][-1]["time"] == NOW - timedelta(minutes=15)
    for key, minutes in (("c5_superset", 5), ("c15_superset", 15)):
        assert all(b["time"] + timedelta(minutes=minutes) <= NOW for b in captured[key])
    r._cached_5m_candles[-2]["close"] = 9999.0  # unavailable 12:00 bar
    assert r._price_at(NOW) == 100.0


def test_trigger_fills_at_close_and_previous_wick_does_not_forbid_entry():
    r = runner()
    bar = {"time": NOW, "open": 100.0, "high": 103.0, "low": 97.0, "close": 102.0}
    armed = ArmedStrategy("", {}, {}, NOW, NOW + timedelta(hours=1), 1)
    r._execute_triggered_setup = lambda *args, **kwargs: {
        "ok": True, "entry": 102.0, "stop": 99.0, "tp1": 106.0, "tp2": 109.0, "quantity": .1}
    position, rejection = r._open_position_from_trigger(
        armed, {"direction": "long"}, bar, entry_iteration=1, refuse_signature=None)
    assert rejection is None
    assert position.entry_time == NOW + timedelta(minutes=5)
    assert position.last_evaluated_bar_time == position.entry_time


def test_distinct_fx_setups_do_not_collapse_into_two_decimal_duplicate():
    assert position_signature(1.13796, 1.1386, "short") != position_signature(1.1412, 1.1420, "short")
    assert position_signature(1.13796, 1.1386, "short") == position_signature(1.13796, 1.1386, "short")


def test_native_stop_occurs_before_timeout_at_bar_close():
    p = OpenPosition("BTCUSD", {"direction": "long", "entry": 100.0, "sl": 90.0,
                               "initial_stop": 90.0},
                     {"tp1": 112.0, "tp": 120.0, "runner_enabled": True},
                     .1, {}, NOW, 1, NOW + timedelta(minutes=5), NOW)
    bar = {"time": NOW, "open": 100.0, "high": 104.0, "low": 89.0, "close": 103.0}
    closed = PositionMonitor(lambda *args, **kwargs: (False, "")).step(p, bar)
    assert closed.outcome["result"] == "SL"
    assert closed.outcome["exit_price"] == 90.0
    assert closed.outcome["exit_time"] == NOW + timedelta(minutes=5)


def test_ttl_is_checked_when_trigger_bar_is_available():
    r = runner()
    data = bars(5)
    r._bar_feed = indexed_feed(data)
    armed = ArmedStrategy("", {}, {}, NOW, NOW + timedelta(minutes=5), 1)
    r._run_armed_strategy_on_bar = lambda *args: pytest.fail("expired strategy was executed")
    advanced = r._advance_closed_bars_to(
        NOW + timedelta(minutes=5), open_position=None, armed_strategy=armed,
        last_processed_bar_time=NOW, entry_iteration=2, refuse_signature=None)
    assert advanced.armed_strategy is None


def test_backtest_preserves_structural_tp1_when_capping_tp2():
    ok, tp1, tp2, reason = runner()._validate_trade_levels(100.0, 99.0, 101.2, 112.0, True)
    assert ok, reason
    assert tp1 == 101.2
    assert tp2 == 106.0
