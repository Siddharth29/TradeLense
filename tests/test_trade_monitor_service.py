import asyncio
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from packages.core.services.trade_monitor import TradeMonitor


@pytest.mark.asyncio
async def test_trade_monitor_emits_actions(monkeypatch):
    closes = [104, 108, 108, 108, 108]
    highs = [106, 111, 111, 111, 111]
    lows = [99, 100, 100, 100, 100]

    async def fetch(asset, n5, n15):
        c = closes.pop(0) if closes else 108
        h = highs.pop(0) if highs else 111
        l = lows.pop(0) if lows else 100
        return (
            [{"close": c, "high": h, "low": l}],
            [],
        )

    candle_service = SimpleNamespace(fetch=AsyncMock(side_effect=fetch))
    broker = SimpleNamespace(
        move_sl_to_be=MagicMock(),
        close=MagicMock(),
        partial_close=MagicMock(),
        modify_sl=MagicMock(),
    )
    journal = SimpleNamespace(log_action=MagicMock())
    monitor = TradeMonitor(candle_service, broker, journal)

    async def fast_bar(interval, tz):
        return datetime.now(timezone.utc)

    monkeypatch.setattr(
        "packages.core.services.trade_monitor.await_next_bar_close", fast_bar
    )
    # The loop waits CSM_CANDLE_BUFFER_SEC after each bar close so the broker can
    # finalise the bar (2026-08-27, audit H2). Real behaviour, but 5s per
    # iteration against this test's 1s budget means nothing would run.
    monkeypatch.setattr(
        "packages.core.services.trade_monitor.MONITOR_CANDLE_BUFFER_SEC", 0
    )

    trade_state = {"direction": "long", "entry": 100, "sl": 95, "sl_moved": False}
    rules = {"tp1": 105, "tp": 110}
    expiry = datetime.now(timezone.utc) + timedelta(minutes=30)
    monitor.queue_trade("NIFTY", trade_state, rules, expiry, "ph", "mode", 0.0)

    await asyncio.sleep(1.0)
    assert broker.close.called or broker.partial_close.called
    assert journal.log_action.call_count >= 1
    await monitor.shutdown()


@pytest.mark.asyncio
async def test_trade_monitor_hits_stop(monkeypatch):
    closes = [94, 94, 94, 94, 94]
    highs = [102, 102, 102, 102, 102]
    lows = [93, 93, 93, 93, 93]

    async def fetch(asset, n5, n15):
        c = closes.pop(0) if closes else 94
        h = highs.pop(0) if highs else 102
        l = lows.pop(0) if lows else 93
        return (
            [{"close": c, "high": h, "low": l}],
            [],
        )

    candle_service = SimpleNamespace(fetch=AsyncMock(side_effect=fetch))
    broker = SimpleNamespace(
        move_sl_to_be=MagicMock(),
        close=MagicMock(),
        partial_close=MagicMock(),
        modify_sl=MagicMock(),
    )
    monitor = TradeMonitor(candle_service, broker)

    async def fast_bar(interval, tz):
        return datetime.now(timezone.utc)

    monkeypatch.setattr(
        "packages.core.services.trade_monitor.await_next_bar_close", fast_bar
    )
    # The loop waits CSM_CANDLE_BUFFER_SEC after each bar close so the broker can
    # finalise the bar (2026-08-27, audit H2). Real behaviour, but 5s per
    # iteration against this test's 1s budget means nothing would run.
    monkeypatch.setattr(
        "packages.core.services.trade_monitor.MONITOR_CANDLE_BUFFER_SEC", 0
    )

    trade_state = {"direction": "long", "entry": 100, "sl": 95, "sl_moved": False}
    rules = {"tp1": 105, "tp": 110}
    expiry = datetime.now(timezone.utc) + timedelta(minutes=30)
    monitor.queue_trade("NIFTY", trade_state, rules, expiry, "ph", "mode", 0.0)

    await asyncio.sleep(1.0)
    assert broker.close.called or broker.modify_sl.called
    await monitor.shutdown()


@pytest.mark.asyncio
async def test_trail_sl_broker_rejection_retries_next_candle(monkeypatch):
    """A broker-rejected trail (MT5 10016) must re-fire with the SAME level
    on the next candle instead of consuming the step (which left the SL a
    full step behind until the next threshold)."""

    async def fetch(asset, n5, n15):
        # Close parked beyond the 0.5R trigger (102.5), SL 95 untouched
        return ([{"close": 103, "high": 104, "low": 100}], [])

    candle_service = SimpleNamespace(fetch=AsyncMock(side_effect=fetch))
    broker = SimpleNamespace(
        close=MagicMock(),
        partial_close=MagicMock(return_value=0.5),
        modify_sl=MagicMock(
            side_effect=[RuntimeError("Invalid stops")] + [None] * 20
        ),
    )
    monitor = TradeMonitor(candle_service, broker)

    async def fast_bar(interval, tz):
        return datetime.now(timezone.utc)

    monkeypatch.setattr(
        "packages.core.services.trade_monitor.await_next_bar_close", fast_bar
    )
    # The loop waits CSM_CANDLE_BUFFER_SEC after each bar close so the broker can
    # finalise the bar (2026-08-27, audit H2). Real behaviour, but 5s per
    # iteration against this test's 1s budget means nothing would run.
    monkeypatch.setattr(
        "packages.core.services.trade_monitor.MONITOR_CANDLE_BUFFER_SEC", 0
    )

    trade_state = {
        "direction": "long", "entry": 100, "sl": 95, "initial_stop": 95,
        "sl_moved": True, "tp1_hit": True, "tp2_hit": False, "quantity": 1.0,
    }
    rules = {"tp1": 120, "tp": 130, "trail_config": [(0.5, 1, 0.5)]}
    expiry = datetime.now(timezone.utc) + timedelta(minutes=30)
    monitor.queue_trade("BTCUSD", trade_state, rules, expiry, "ph", "mode", 0.0)

    await asyncio.sleep(1.0)
    await monitor.shutdown()

    sl_calls = [c.args for c in broker.modify_sl.call_args_list]
    assert len(sl_calls) >= 2, "rejected trail was never retried"
    assert sl_calls[0] == ("BTCUSD", 102.5)
    assert sl_calls[1] == ("BTCUSD", 102.5)  # same level retried, not skipped


@pytest.mark.asyncio
async def test_tp1_partial_fires_once_when_be_move_fails(monkeypatch):
    """TP1 is consumed by the successful partial even if the follow-up SL
    move is rejected — re-firing MOVE_SL_TO_BE would partial-close the
    remaining volume again on every candle."""

    async def fetch(asset, n5, n15):
        # Candle range straddles TP1=105 every cycle; SL 95 never touched
        return ([{"close": 104, "high": 106, "low": 100}], [])

    candle_service = SimpleNamespace(fetch=AsyncMock(side_effect=fetch))
    broker = SimpleNamespace(
        close=MagicMock(),
        partial_close=MagicMock(return_value=0.5),
        modify_sl=MagicMock(side_effect=RuntimeError("Invalid stops")),
    )
    monitor = TradeMonitor(candle_service, broker)

    async def fast_bar(interval, tz):
        return datetime.now(timezone.utc)

    monkeypatch.setattr(
        "packages.core.services.trade_monitor.await_next_bar_close", fast_bar
    )
    # The loop waits CSM_CANDLE_BUFFER_SEC after each bar close so the broker can
    # finalise the bar (2026-08-27, audit H2). Real behaviour, but 5s per
    # iteration against this test's 1s budget means nothing would run.
    monkeypatch.setattr(
        "packages.core.services.trade_monitor.MONITOR_CANDLE_BUFFER_SEC", 0
    )

    trade_state = {
        "direction": "long", "entry": 100, "sl": 95, "initial_stop": 95,
        "sl_moved": False, "tp1_hit": False, "tp2_hit": False, "quantity": 1.0,
    }
    rules = {"tp1": 105, "tp": 200, "trail_config": []}
    expiry = datetime.now(timezone.utc) + timedelta(minutes=30)
    monitor.queue_trade("BTCUSD", trade_state, rules, expiry, "ph", "mode", 0.0)

    await asyncio.sleep(1.0)
    await monitor.shutdown()

    assert broker.partial_close.call_count == 1, (
        f"TP1 partial fired {broker.partial_close.call_count} times — "
        "must be exactly once even when the BE move fails"
    )
