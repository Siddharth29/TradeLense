from datetime import datetime, timedelta, timezone

import pytest

from packages.adapters import mt5_adapter as mt5_mod


@pytest.mark.asyncio
@pytest.mark.parametrize("interval, minutes", [("5m", 5), ("15m", 15)])
@pytest.mark.parametrize("after_weekend", [False, True])
async def test_mt5_candles_return_requested_count_of_closed_bars(monkeypatch, interval, minutes, after_weekend):
    now = datetime(2026, 11, 16, 0, 32, tzinfo=timezone.utc)
    boundary = now.replace(minute=(now.minute // minutes) * minutes, second=0)
    step = timedelta(minutes=minutes)
    latest_closed = boundary - step
    previous = datetime(2026, 11, 13, 21, 45, tzinfo=timezone.utc) if after_weekend else latest_closed - step

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is None else now.astimezone(tz)

    monkeypatch.setattr(mt5_mod, "datetime", FixedDateTime)
    monkeypatch.setattr("packages.core.services.code_sniper_monitor._is_market_closed", lambda symbol: (False, 0))

    class DummyMT5:
        TIMEFRAME_M5 = 5
        TIMEFRAME_M15 = 15

        def copy_rates_from(self, symbol, timeframe, cutoff, count):
            assert (symbol, timeframe, cutoff, count) == (
                "XAUUSD", minutes, boundary - timedelta(seconds=1), 2
            )
            return [
                {"time": int(ts.timestamp()), "open": 1, "high": 1, "low": 1, "close": 1}
                for ts in (previous, latest_closed, boundary)
            ]

    adapter = mt5_mod.MT5Adapter()
    adapter._mt5 = DummyMT5()
    monkeypatch.setattr(adapter, "is_connected", lambda: True)

    candles = await adapter.candles("XAUUSD", interval, 2)
    assert [c.time.to_pydatetime() for c in candles] == [previous, latest_closed]
