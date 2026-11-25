import logging
from datetime import timezone

import pytest

from packages.adapters.mt5_adapter import MT5Adapter


class DummyMT5:
    TIMEFRAME_M5 = 5

    def __init__(self, data, select=True):
        self.data = data
        self.select = select

    def symbol_select(self, symbol, flag):
        assert flag is True
        return self.select

    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        assert timeframe == self.TIMEFRAME_M5
        return self.data[:count]


@pytest.mark.asyncio
async def test_get_recent_bars_returns_normalised():
    raw = [
        {"time": 1, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "tick_volume": 10},
        {"time": 2, "open": 2, "high": 3, "low": 1.5, "close": 2.5, "tick_volume": 20},
    ]
    adapter = MT5Adapter()
    adapter._mt5 = DummyMT5(raw)
    bars = await adapter.get_recent_bars("ASSET", "M5", 2)
    assert len(bars) == 2
    first = bars[0]
    assert set(first) == {"ts", "open", "high", "low", "close", "tick_volume"}
    assert first["ts"].tzinfo == timezone.utc
    assert first["open"] == 1.0


@pytest.mark.asyncio
async def test_get_recent_bars_logs_and_raises(caplog):
    adapter = MT5Adapter()
    adapter._mt5 = DummyMT5([], select=False)
    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError):
            await adapter.get_recent_bars("ASSET", "M5", 1)
    assert "mt5 get_recent_bars failed" in caplog.text
