"""Regression tests for the frozen-feed → MarketClosedError fix.

Reproduces the 2026-07-03 production incident (logs (42).json): the US-linked
assets (XAU/XAG/USOIL/USTEC) closed early at 16:55 UTC for the July-4 holiday,
so at 19:02 UTC the broker feed was ~25 intervals (2h5m) stale while the
scheduled-close guard still reported "open".  The old code treated that as a
retryable fetch error and burned the whole multi-layer retry+reconnect budget
against a shut market (168 errors / 744 warnings in ~2 min).

The fix: when the newest candle is more than
``MT5_STALE_MARKET_CLOSED_INTERVALS`` intervals behind the requested end,
``_fetch_candles_internal`` raises the typed :class:`MarketClosedError` instead
of a retryable ``ValueError``.  ``candles()`` re-raises it without retrying, and
``CandleService.get_candles`` re-raises it without forcing another broker fetch.
"""

import asyncio
from datetime import datetime, timezone, timedelta

import numpy as np
import pytest

from packages.adapters.mt5_adapter import MT5Adapter
from packages.core.candles import MarketClosedError
from packages.core.candle_service import CandleService
from packages.core.config import MT5_STALE_MARKET_CLOSED_INTERVALS


_RATES_DTYPE = np.dtype(
    [
        ("time", "<i8"),
        ("open", "<f8"),
        ("high", "<f8"),
        ("low", "<f8"),
        ("close", "<f8"),
        ("tick_volume", "<u8"),
        ("spread", "<i4"),
        ("real_volume", "<u8"),
    ]
)


def _make_rates(last_candle_time: datetime, count: int, minutes: int) -> np.ndarray:
    """Build an MT5-style structured array of `count` closed candles ending at
    `last_candle_time` (inclusive), spaced `minutes` apart."""
    rows = []
    for i in range(count):
        ts = last_candle_time - timedelta(minutes=minutes * (count - 1 - i))
        rows.append(
            (int(ts.timestamp()), 100.0, 101.0, 99.0, 100.5, 1000, 2, 0)
        )
    return np.array(rows, dtype=_RATES_DTYPE)


class _FakeMT5:
    """Minimal stand-in for the MetaTrader5 module used by MT5Adapter fetches."""

    TIMEFRAME_M5 = 5
    TIMEFRAME_M15 = 15

    def __init__(self, last_candle_time: datetime, count: int, minutes: int):
        self._rates = _make_rates(last_candle_time, count, minutes)
        self.calls = 0

    def copy_rates_from(self, symbol, timeframe, cutoff, count):
        self.calls += 1
        return self._rates

    def last_error(self):
        return (0, "ok")


def _build_adapter(fake_mt5: _FakeMT5) -> MT5Adapter:
    adapter = MT5Adapter()
    adapter._mt5 = fake_mt5
    adapter._credentials = {"server": "s", "login": "1", "password": "p"}
    # is_connected() normally pokes MT5 over IPC — keep the fetch path pure.
    adapter.is_connected = lambda: True
    return adapter


@pytest.fixture(autouse=True)
def _market_open(monkeypatch):
    """Force the scheduled-close guard to report 'open' so the fetch path runs —
    exactly the incident condition (holiday early-close the calendar doesn't know)."""
    monkeypatch.setattr(
        "packages.core.services.code_sniper_monitor._is_market_closed",
        lambda symbol, now=None: (False, 0),
    )


def test_frozen_feed_raises_market_closed():
    """25-interval-stale feed (the incident) → MarketClosedError, not RuntimeError."""
    now = datetime.now(timezone.utc)
    last = now.replace(second=0, microsecond=0) - timedelta(minutes=125)  # 25×5m behind
    fake = _FakeMT5(last, count=150, minutes=5)
    adapter = _build_adapter(fake)

    with pytest.raises(MarketClosedError):
        asyncio.run(adapter.candles("XAGUSD", "5m", 150))


def test_frozen_feed_does_not_burn_retry_budget():
    """The graceful path must fetch ONCE — no retry/reconnect storm."""
    now = datetime.now(timezone.utc)
    last = now.replace(second=0, microsecond=0) - timedelta(minutes=125)
    fake = _FakeMT5(last, count=150, minutes=5)
    adapter = _build_adapter(fake)

    with pytest.raises(MarketClosedError):
        asyncio.run(adapter.candles("XAGUSD", "5m", 150, max_retries=2))

    # Old behaviour: 3 attempts (max_retries+1). Fixed: exactly one fetch.
    assert fake.calls == 1


def test_moderate_lag_still_retries():
    """Lag between 2× and the frozen threshold stays a retryable ValueError so a
    genuine transient broker delay at a close boundary still gets its retries."""
    now = datetime.now(timezone.utc)
    # 4 intervals behind: > 2× (retryable) but < frozen threshold (6×).
    assert 2 < 4 < MT5_STALE_MARKET_CLOSED_INTERVALS
    last = now.replace(second=0, microsecond=0) - timedelta(minutes=20)
    fake = _FakeMT5(last, count=150, minutes=5)
    adapter = _build_adapter(fake)

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(adapter.candles("XAGUSD", "5m", 150, max_retries=2))

    assert not isinstance(exc.value, MarketClosedError)
    assert fake.calls == 3  # retried, budget spent as before


def test_candle_service_reraises_market_closed_without_forcing_fetch():
    """CandleService.get_candles must bubble MarketClosedError up, not swallow it
    and force another broker round-trip against the frozen feed."""
    now = datetime.now(timezone.utc)
    last = now.replace(second=0, microsecond=0) - timedelta(minutes=125)
    fake = _FakeMT5(last, count=150, minutes=5)
    adapter = _build_adapter(fake)

    service = CandleService(broker=adapter)
    with pytest.raises(MarketClosedError):
        asyncio.run(service.get_candles("XAGUSD", "5m", 150))

    # One broker fetch total: the incremental attempt raised MarketClosedError and
    # get_candles re-raised instead of running the force-broker fallback.
    assert fake.calls == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
