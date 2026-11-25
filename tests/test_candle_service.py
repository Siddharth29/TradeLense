from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from packages.core.candle_service import CandleService, NoCandleData
from packages.core.candle_validation import validate_live_candle_window
from packages.core.candles import Candle


@pytest.mark.asyncio
async def test_fetch_raises_no_candle_data(monkeypatch):
    async def fake_load(asset, start=None, end=None):
        return pd.DataFrame(columns=["open", "high", "low", "close"])

    monkeypatch.setattr("packages.core.candle_service.load_candles", fake_load)
    svc = CandleService()
    with pytest.raises(NoCandleData):
        await svc.fetch("XYZ", n5=1, n15=1)


@pytest.mark.asyncio
async def test_get_candles_returns_requested_interval(monkeypatch):
    svc = CandleService()

    base = pd.Timestamp("2024-01-01 09:00", tz="UTC")
    c = Candle(time=base, open=1, high=2, low=0.5, close=1.5, volume=10)

    async def fake_fetch_incremental(symbol, interval, limit):
        assert symbol == "ABC"
        assert interval == "5m"
        assert limit == 2
        return [c, c]

    monkeypatch.setattr(svc, "fetch_incremental", fake_fetch_incremental)

    candles = await svc.get_candles("ABC", "5m", 2)
    assert candles == [c, c]


@pytest.mark.asyncio
async def test_incremental_fetch_fills_all_elapsed_bars_and_reuses_fresh_cache(monkeypatch):
    now = datetime(2026, 11, 16, 10, 32, tzinfo=timezone.utc)

    class FixedTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is None else now.astimezone(tz)

    monkeypatch.setattr("packages.core.candle_service.datetime", FixedTime)
    monkeypatch.setattr("packages.core.candle_validation.datetime", FixedTime)
    bars = [Candle(time=t, open=1, high=2, low=0.5, close=1.5, volume=10)
            for t in pd.date_range("2026-11-16 10:00", periods=6, freq="5min", tz="UTC")]
    svc = CandleService()
    svc._update_cache("BTCUSD", "5m", bars[:4])
    requests = []

    async def fetch_interval(symbol, interval, count):
        requests.append(count)
        return bars[-count:]

    monkeypatch.setattr(svc, "_fetch_interval_from_broker", fetch_interval)
    assert await svc.fetch_incremental("BTCUSD", "5m", 4) == bars[2:]
    assert await svc.fetch_incremental("BTCUSD", "5m", 4) == bars[2:]
    assert requests == [2]


@pytest.mark.asyncio
async def test_cache_retains_100_bar_live_m15_window(monkeypatch):
    now = datetime(2026, 11, 18, 17, 0, 15, tzinfo=timezone.utc)

    class FixedTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is None else now.astimezone(tz)

    monkeypatch.setattr("packages.core.candle_service.datetime", FixedTime)
    bars = [Candle(now.replace(second=0) - timedelta(minutes=15 * i), 1, 2, 0.5, 1.5, 10)
            for i in range(100, 0, -1)]
    service = CandleService()
    requests = []

    async def fetch_interval(symbol, interval, count):
        requests.append(count)
        return bars[-count:]

    monkeypatch.setattr(service, "_fetch_interval_from_broker", fetch_interval)
    assert await service.get_candles("EURUSD", "15m", 100) == bars
    assert await service.get_candles("EURUSD", "15m", 100) == bars
    bars.append(replace(bars[-1], time=now.replace(second=0)))
    now += timedelta(minutes=15)
    assert await service.get_candles("EURUSD", "15m", 100) == bars[-100:]
    assert requests == [100, 1]


def test_live_window_rejects_old_gap_nan_and_incomplete_data():
    now = datetime(2026, 11, 18, 21, 30, 15, tzinfo=timezone.utc)
    bars = [Candle(now.replace(second=0) - timedelta(minutes=5 * i), 1, 2, 0.5, 1.5, 10)
            for i in range(32, 0, -1)]
    kwargs = dict(expected_count=30, interval_minutes=5, asset="EURUSD", current_time=now)

    assert validate_live_candle_window(bars[-30:], **kwargs) == (True, "")
    valid, reason = validate_live_candle_window(bars[-29:], **kwargs)
    assert not valid and "Expected 30" in reason
    valid, reason = validate_live_candle_window(bars[1:3] + bars[4:], **kwargs)
    assert not valid and "Gap detected" in reason
    bad_bar = replace(bars[2], low=float("nan"))
    valid, reason = validate_live_candle_window([bad_bar, *bars[3:]], **kwargs)
    assert not valid and "Invalid candle" in reason
