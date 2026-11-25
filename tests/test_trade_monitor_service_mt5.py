from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, AsyncMock
import asyncio

import pytest

from packages.core.services.trade_monitor import TradeMonitor
from packages.adapters.mt5_adapter import MT5Adapter


@pytest.mark.asyncio
async def test_trade_monitor_uses_wait_for_bar(monkeypatch):
    closes = [104, 108]
    highs = [106, 111]
    lows = [99, 100]

    async def fake_wait_for_bar(adapter, symbol, interval):
        return {
            "close": closes.pop(0),
            "high": highs.pop(0),
            "low": lows.pop(0),
            "ts": datetime.now(timezone.utc),
            "open": 0.0,
            "tick_volume": 0,
        }

    async def fail_bar(interval, tz):
        raise AssertionError("await_next_bar_close should not be called")

    monkeypatch.setattr(
        "packages.core.services.trade_monitor.wait_for_bar", fake_wait_for_bar
    )
    monkeypatch.setattr(
        "packages.core.services.trade_monitor.await_next_bar_close", fail_bar
    )
    for name in ("upsert_monitor_state", "update_monitor_state", "clear_monitor_state"):
        monkeypatch.setattr(f"packages.core.services.trade_monitor.{name}", AsyncMock())
    monkeypatch.setattr(
        "packages.db.repositories.comprehensive_logging.update_comprehensive_trade_exit",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr("packages.db.repositories.comprehensive_logging.append_sl_trail", AsyncMock())

    candle_service = MT5Adapter()
    # An unsplittable min-lot position may move its stop without inventing
    # a partial fill. A broker missing the partial-close API cannot confirm it.
    broker = SimpleNamespace(move_sl_to_be=MagicMock(), close=MagicMock(),
                             partial_close=MagicMock(return_value=False))
    journal = SimpleNamespace(log_action=MagicMock())
    completed = asyncio.Event()
    monitor = TradeMonitor(candle_service, broker, journal,
                           on_trade_closed=lambda *_: completed.set())

    trade_state = {"direction": "long", "entry": 100, "sl": 95, "sl_moved": False}
    rules = {"tp1": 105, "tp": 110}
    expiry = datetime.now(timezone.utc) + timedelta(minutes=30)
    monitor.queue_trade("XAUUSD", trade_state, rules, expiry, "ph", "mode", 0.0)

    try:
        await asyncio.wait_for(completed.wait(), timeout=2.)
        assert broker.move_sl_to_be.called
        assert broker.close.called
        assert journal.log_action.call_count == 2
    finally:
        await monitor.shutdown()

