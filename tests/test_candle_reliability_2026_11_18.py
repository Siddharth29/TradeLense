"""Partial-history recovery from the November 18 incident; no broker connections."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from packages.adapters import mt5_adapter as mt5_module
from packages.core.candle_service import CandleFetchFailed, CandleService
from packages.core.candle_validation import validate_timeframe_consistency
from packages.core.candles import Candle, MarketClosedError


NOW = datetime(2026, 11, 18, 14, 0, 15, tzinfo=timezone.utc)


def windows():
    c5 = [Candle(NOW.replace(minute=30, second=0) - timedelta(hours=1)
                 + timedelta(minutes=5 * i), 100 + i, 102 + i, 99 + i, 101 + i, 10 + i)
          for i in range(6)]
    c15 = [Candle(parts[0].time, parts[0].open, max(b.high for b in parts),
                  min(b.low for b in parts), parts[-1].close, sum(b.volume for b in parts))
           for parts in (c5[:3], c5[3:])]
    return c5, c15


@pytest.mark.parametrize("field,value", [("high", 101), ("low", 100),
                                         ("close", 100), ("volume", 1)])
def test_partial_m15_is_rejected_even_with_closed_valid_timestamp(field, value):
    c5, c15 = windows()
    c15[0] = replace(c15[0], **{field: value})
    valid, reason = validate_timeframe_consistency(c5, c15)
    assert not valid
    assert field in reason and str(c15[0].time) in reason


def test_consistency_only_checks_complete_overlap_and_optional_volume():
    c5, c15 = windows()
    assert validate_timeframe_consistency(c5, c15) == (True, "")
    # A short M5 lookback does not license fabricating older M15 constituents.
    assert validate_timeframe_consistency(c5[1:], [replace(c15[0], volume=1)]) == (True, "")
    assert validate_timeframe_consistency([replace(b, volume=None) for b in c5], c15) == (True, "")


@pytest.mark.asyncio
async def test_decision_pair_rejects_and_evicts_partial_cached_history(monkeypatch):
    c5, c15 = windows()
    service = CandleService()
    service._update_cache("EURUSD", "5m", c5)
    service._update_cache("EURUSD", "15m", c15)

    async def get(symbol, interval, limit, force_broker=False):
        return c5 if interval == "5m" else [replace(c15[0], volume=1), c15[1]]

    monkeypatch.setattr(service, "get_candles", get)
    with pytest.raises(CandleFetchFailed, match="M5/M15 mismatch"):
        await service.fetch("EURUSD", n5=6, n15=2)
    assert not service.has_cache("EURUSD")


@pytest.mark.asyncio
async def test_forced_decision_window_repairs_older_partial_despite_fresh_newest_bar(monkeypatch):
    c5, c15 = windows()
    service = CandleService()
    service._update_cache("EURUSD", "5m", c5)
    service._update_cache("EURUSD", "15m", [replace(c15[0], volume=1), c15[1]])
    reads = []

    async def fetch(symbol, interval, count):
        reads.append((interval, count))
        return (c5 if interval == "5m" else c15)[-count:]

    monkeypatch.setattr(service, "_fetch_interval_from_broker", fetch)
    assert await service.fetch("EURUSD", n5=6, n15=2, force_broker=True) == (c5, c15)
    assert sorted(reads) == [("15m", 2), ("5m", 6)]
    assert service._cache_15m["EURUSD_15m"] == c15


def rates(latest, count=3):
    return np.array([
        (int((latest - timedelta(minutes=5 * i)).timestamp()), 100., 101., 99., 100., 10)
        for i in range(count - 1, -1, -1)
    ], dtype=[("time", "i8"), ("open", "f8"), ("high", "f8"),
              ("low", "f8"), ("close", "f8"), ("tick_volume", "i8")])


@pytest.mark.asyncio
@pytest.mark.parametrize("live_quote", [True, False])
async def test_frozen_history_has_one_bounded_recovery_only_with_live_quote(monkeypatch, live_quote):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(mt5_module, "datetime", Clock)
    stale = rates(NOW.replace(minute=0, second=0) - timedelta(hours=1))
    fresh = rates(NOW.replace(minute=0, second=0), count=4)  # includes forming bar
    calls = []

    def from_pos(*args):
        calls.append(args)
        return fresh

    fake = SimpleNamespace(
        TIMEFRAME_M5=5, TIMEFRAME_M15=15,
        copy_rates_from=lambda *args: stale,
        copy_rates_from_pos=from_pos,
        symbol_info_tick=lambda symbol: SimpleNamespace(time=NOW.timestamp() - (0 if live_quote else 3600)),
    )
    adapter = mt5_module.MT5Adapter()
    adapter._mt5 = fake
    monkeypatch.setattr(adapter, "is_connected", lambda: True)
    if live_quote:
        result = await adapter._fetch_candles_internal("XAGUSD", "5m", 3)
        assert len(result) == 3
        assert result[-1].time == NOW.replace(minute=0, second=0) - timedelta(minutes=5)
        assert len(calls) == 1
    else:
        with pytest.raises(MarketClosedError):
            await adapter._fetch_candles_internal("XAGUSD", "5m", 3)
        assert not calls


@pytest.mark.asyncio
async def test_boundary_read_waits_for_finalization_before_reading(monkeypatch):
    clock = NOW.replace(second=0)
    calls = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock

    async def sleep(seconds):
        nonlocal clock
        calls.append(("wait", seconds))
        clock += timedelta(seconds=seconds)

    def read(*args):
        calls.append(("read", clock))
        return rates(NOW.replace(minute=0, second=0) - timedelta(minutes=5))

    monkeypatch.setattr(mt5_module, "datetime", Clock)
    monkeypatch.setattr(mt5_module.asyncio, "sleep", sleep)
    adapter = mt5_module.MT5Adapter()
    adapter._mt5 = SimpleNamespace(TIMEFRAME_M5=5, TIMEFRAME_M15=15, copy_rates_from=read)
    monkeypatch.setattr(adapter, "is_connected", lambda: True)
    await adapter._fetch_candles_internal("EURUSD", "5m", 3)
    assert calls[0] == ("wait", mt5_module.MT5_CANDLE_FINALIZATION_BUFFER_SEC)
    assert calls[1][0] == "read"
    assert calls[1][1] > NOW.replace(second=0)


@pytest.mark.asyncio
async def test_range_refresh_replaces_partial_older_bar_and_preserves_gap_history(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(mt5_module, "datetime", Clock)
    seed = rates(NOW.replace(minute=0, second=0) - timedelta(minutes=5))
    # Model a prior-session bar supplied only by count-based history.
    seed[0]["time"] -= 3 * 24 * 3600
    seed[1]["tick_volume"] = 1
    revised = seed[1:].copy()
    revised[0]["tick_volume"] = 805
    revised[0]["low"] = 98.0
    adapter = mt5_module.MT5Adapter()
    adapter._mt5 = SimpleNamespace(
        TIMEFRAME_M5=5, TIMEFRAME_M15=15,
        copy_rates_from=lambda *args: seed,
        copy_rates_range=lambda *args: revised,
    )
    monkeypatch.setattr(adapter, "is_connected", lambda: True)
    result = await adapter._fetch_candles_internal("EURUSD", "5m", 3)
    assert len(result) == 3
    assert result[0].time.timestamp() == seed[0]["time"]
    assert result[1].volume == 805
    assert result[1].low == 98.0
