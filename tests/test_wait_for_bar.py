from datetime import datetime, timezone, timedelta
import asyncio

import pytest

from packages.adapters.mt5_adapter import wait_for_bar


class DummyAdapter:
    def __init__(self) -> None:
        self.calls = 0

    async def get_recent_bars(self, symbol: str, interval: str, count: int):
        self.calls += 1
        t0 = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
        t1 = t0 + timedelta(minutes=5)
        t2 = t1 + timedelta(minutes=5)

        def _bar(ts):
            return {
                "ts": ts,
                "open": 0.0,
                "high": 0.0,
                "low": 0.0,
                "close": 0.0,
                "tick_volume": 0,
            }

        if self.calls < 3:
            return [_bar(t0), _bar(t1)]
        return [_bar(t1), _bar(t2)]


@pytest.mark.asyncio
async def test_wait_for_bar_returns_new_bar(monkeypatch):
    dummy = DummyAdapter()

    async def fast_sleep(_: float) -> None:  # type: ignore[override]
        return None

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    bar = await wait_for_bar(dummy, "X", "M5")
    assert bar["ts"] == datetime(2024, 1, 1, 0, 5, tzinfo=timezone.utc)
    assert dummy.calls >= 3

