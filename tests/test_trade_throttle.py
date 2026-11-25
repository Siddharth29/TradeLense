"""Tests for the live-path over-trading guard (trade_throttle, 2026-06-20).

The guard is read-only over comprehensive_trades and fail-open. These tests
cover the daily trade cap, the time-based consecutive-SL cooldown (including
naive Mongo datetimes — the tz footgun), streak-break, the disable flag, and
fail-open on a backend error.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from packages.core.risk import trade_throttle as tt
from packages.core.config import (
    BACKTEST_MAX_TRADES_PER_DAY,
    BACKTEST_COOLDOWN_SL_THRESHOLD,
    TRADE_COOLDOWN_MINUTES,
)

_NOW = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)


class _FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, field, direction):
        self._docs.sort(key=lambda d: d.get(field) or datetime.min, reverse=(direction == -1))
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    async def to_list(self, length=None):
        return list(self._docs)


class _FakeColl:
    def __init__(self, count, recent):
        self._count = count
        self._recent = recent

    async def count_documents(self, filt):
        return self._count

    def find(self, filt, projection=None):
        return _FakeCursor(self._recent)


def _install(monkeypatch, count=0, recent=None):
    coll = _FakeColl(count, recent or [])
    client = {tt.MONGO_DB_NAME: {"comprehensive_trades": coll}}
    monkeypatch.setattr(tt, "get_client", lambda: client)
    monkeypatch.setattr(tt, "_utc_now", lambda: _NOW)
    return coll


def _loss(minutes_ago):
    # NAIVE close time on purpose — mirrors Motor (tz_aware=False).
    return {
        "pnl": -2.5,
        "exit_reason": "SL",
        "trade_closed_at": (_NOW - timedelta(minutes=minutes_ago)).replace(tzinfo=None),
    }


def _win(minutes_ago):
    return {
        "pnl": 3.0,
        "exit_reason": "TP2",
        "trade_closed_at": (_NOW - timedelta(minutes=minutes_ago)).replace(tzinfo=None),
    }


@pytest.mark.asyncio
async def test_allows_under_cap_no_streak(monkeypatch):
    _install(monkeypatch, count=2, recent=[])
    blocked, reason = await tt.trading_throttled("BTCUSD")
    assert blocked is False and reason == ""


@pytest.mark.asyncio
async def test_blocks_at_daily_cap(monkeypatch):
    _install(monkeypatch, count=BACKTEST_MAX_TRADES_PER_DAY, recent=[])
    blocked, reason = await tt.trading_throttled("BTCUSD")
    assert blocked is True
    assert "daily trade cap" in reason


@pytest.mark.asyncio
async def test_blocks_on_consecutive_sl_within_cooldown(monkeypatch):
    # threshold consecutive losses, newest 10 min ago (< cooldown) → blocked.
    recent = [_loss(10), _loss(25), _loss(40)][:BACKTEST_COOLDOWN_SL_THRESHOLD]
    _install(monkeypatch, count=BACKTEST_COOLDOWN_SL_THRESHOLD, recent=recent)
    blocked, reason = await tt.trading_throttled("BTCUSD")
    assert blocked is True
    assert "cooldown" in reason.lower()


@pytest.mark.asyncio
async def test_cooldown_clears_after_window(monkeypatch):
    # Same streak but newest loss is older than the cooldown window → allowed.
    old = TRADE_COOLDOWN_MINUTES + 10
    recent = [_loss(old), _loss(old + 15), _loss(old + 30)][:BACKTEST_COOLDOWN_SL_THRESHOLD]
    _install(monkeypatch, count=BACKTEST_COOLDOWN_SL_THRESHOLD, recent=recent)
    blocked, reason = await tt.trading_throttled("BTCUSD")
    assert blocked is False


@pytest.mark.asyncio
async def test_streak_broken_by_a_win(monkeypatch):
    # A win among the recent closes breaks the streak → allowed.
    recent = [_loss(5), _win(20), _loss(35)][:BACKTEST_COOLDOWN_SL_THRESHOLD]
    _install(monkeypatch, count=3, recent=recent)
    blocked, _ = await tt.trading_throttled("BTCUSD")
    assert blocked is False


@pytest.mark.asyncio
async def test_disabled_flag_allows(monkeypatch):
    monkeypatch.setattr(tt, "TRADE_THROTTLE_ENABLED", False)
    # Even at the cap, disabled → allowed (and no backend touched).
    monkeypatch.setattr(tt, "get_client", lambda: (_ for _ in ()).throw(AssertionError("touched")))
    blocked, reason = await tt.trading_throttled("BTCUSD")
    assert blocked is False and reason == ""


@pytest.mark.asyncio
async def test_fail_open_on_error(monkeypatch):
    def _boom():
        raise RuntimeError("mongo down")
    monkeypatch.setattr(tt, "get_client", _boom)
    blocked, reason = await tt.trading_throttled("BTCUSD")
    assert blocked is False and reason == ""


@pytest.mark.asyncio
async def test_empty_asset_allows(monkeypatch):
    _install(monkeypatch, count=99, recent=[_loss(1)] * 5)
    blocked, _ = await tt.trading_throttled("")
    assert blocked is False
