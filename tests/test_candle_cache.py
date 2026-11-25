import pandas as pd
import pytest
from packages.core.candle_cache import CandleCache
from packages.core.candles import Candle


def test_initialize_from_dicts_and_limit_to_ten():
    cache = CandleCache("NIFTY")
    base = pd.Timestamp("2024-01-01 09:00:00", tz="UTC")
    candles = []
    for i in range(12):
        candles.append({
            "time": (base + pd.Timedelta(minutes=5 * i)).isoformat(),
            "open": float(i),
            "high": float(i) + 0.1,
            "low": float(i) - 0.1,
            "close": float(i) + 0.05,
            "volume": float(i),
        })

    assert cache.initialize_from_sniper(candles)
    stats = cache.get_stats()
    # Cache no longer trims to 10 — capacity is config-driven (150 by default)
    assert stats["candle_count"] == 12

    hist = cache.get_historical_candles()
    assert hist is not None
    assert len(hist) == 12

    current = cache.get_current_candle()
    assert isinstance(current["time"], str)
    expected_time = (base + pd.Timedelta(minutes=5 * 11)).isoformat()
    assert current["time"] == expected_time


@pytest.mark.asyncio
async def test_fetch_latest_candle_only_uses_service():
    cache = CandleCache("ABC")
    base = pd.Timestamp("2024-01-01 09:00", tz="UTC")
    initial = [
        Candle(time=base + pd.Timedelta(minutes=5 * i), open=1, high=1, low=1, close=1, volume=1)
        for i in range(10)
    ]
    assert cache.initialize_from_sniper(initial)

    new_candle = Candle(
        time=base + pd.Timedelta(minutes=50), open=2, high=2, low=2, close=2, volume=2
    )

    class DummyService:
        async def get_latest_closed_candle(self, asset, interval):
            assert asset == "ABC"
            assert interval == "5m"
            return new_candle

    fetched = await cache.fetch_latest_candle_only(DummyService())
    assert fetched == new_candle
