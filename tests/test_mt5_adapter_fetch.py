import pytest
from datetime import timezone

from packages.adapters.mt5_adapter import MT5Adapter


class DummyMT5:
    TIMEFRAME_M5 = 5
    TIMEFRAME_M15 = 15

    def symbol_select(self, symbol, flag):
        assert flag is True
        return True

    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        base = 100 if timeframe == self.TIMEFRAME_M5 else 200
        return [
            {
                "time": base + i,
                "open": float(i),
                "high": float(i + 1),
                "low": float(i - 1),
                "close": float(i + 0.5),
                "tick_volume": i,
            }
            for i in range(count)
        ]


@pytest.mark.asyncio
async def test_get_recent_bars_returns_expected_counts():
    adapter = MT5Adapter()
    adapter._mt5 = DummyMT5()
    bars5 = await adapter.get_recent_bars("BTCUSD", "M5", 50)
    bars15 = await adapter.get_recent_bars("BTCUSD", "M15", 30)
    assert len(bars5) == 50
    assert len(bars15) == 30
    assert bars5 and bars15
    assert bars5[0]["ts"].tzinfo == timezone.utc
