"""Regression tests for recent-trades retrieval (2026-06-20 fixes).

Covers the three failure modes audited in the decision-flow review:

1. tz bug — Motor is not ``tz_aware``, so BSON datetimes decode NAIVE. The old
   ``now (aware) - t (naive)`` raised ``TypeError`` on every call, silently
   zeroing recent-trades context (and the re-entry gate). These tests feed naive
   datetimes (the exact prod condition) and assert no crash + correct math.
2. recency — the query must anchor on ``trade_closed_at`` (not execution time).
3. freshness cap — ``max_bars`` must drop pre-outage trades measured against the
   live request time, so a restart after downtime injects no stale history.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from packages.db.repositories import comprehensive_logging as cl
from packages.core.multi_expert import _most_recent_closed_trade


_NOW = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)


class _FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, field, direction):
        self._docs.sort(
            key=lambda d: d.get(field) or datetime.min,
            reverse=(direction == -1),
        )
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    async def to_list(self, length=None):
        return list(self._docs)


class _FakeCollection:
    """Minimal stand-in honoring asset/status + trade_closed_at $gte filtering."""

    def __init__(self, docs):
        self._docs = docs
        self.last_filter = None
        self.last_projection = None

    @staticmethod
    def _naive(dt):
        return dt.replace(tzinfo=None) if dt is not None and dt.tzinfo else dt

    def find(self, filt, projection=None):
        self.last_filter = filt
        self.last_projection = projection
        out = []
        for d in self._docs:
            if d.get("asset") != filt.get("asset"):
                continue
            if d.get("trade_status") != filt.get("trade_status"):
                continue
            cond = filt.get("trade_closed_at") or {}
            gte = cond.get("$gte")
            if gte is not None:
                tc = d.get("trade_closed_at")
                if tc is None or self._naive(tc) < self._naive(gte):
                    continue
            out.append(d)
        return _FakeCursor(out)


def _install(monkeypatch, docs):
    coll = _FakeCollection(docs)
    client = {cl.MONGO_DB_NAME: {cl._COLLECTION_COMPREHENSIVE_TRADES: coll}}
    monkeypatch.setattr(cl, "get_client", lambda: client)

    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return _NOW

    monkeypatch.setattr(cl, "datetime", _Now)
    return coll


def _doc(asset, minutes_ago, side="buy", entry=100.0, exit=101.0, pnl=1.0,
         status="closed", reason="TP2"):
    # NAIVE datetimes on purpose — mirrors Motor's default (tz_aware=False).
    closed = (_NOW - timedelta(minutes=minutes_ago)).replace(tzinfo=None)
    return {
        "asset": asset, "trade_status": status, "side": side,
        "entry_price": entry, "exit_price": exit, "pnl": pnl,
        "exit_reason": reason, "trade_closed_at": closed,
        "trade_executed_at": closed,
    }


# ── 1. tz bug: naive datetimes must not raise, and bars must compute ─────────

@pytest.mark.asyncio
async def test_naive_datetimes_do_not_crash(monkeypatch):
    _install(monkeypatch, [_doc("BTCUSD", 10), _doc("BTCUSD", 30)])
    res = await cl.get_recent_closed_trades("BTCUSD", lookback_hours=24, limit=6)
    # Newest-close first; bars_elapsed = minutes // 5.
    assert [r["bars_elapsed"] for r in res] == [2, 6]
    assert [r["ago_min"] for r in res] == [10, 30]
    assert all(isinstance(r["bars_elapsed"], int) for r in res)


def test_as_utc_normalizes_naive():
    naive = datetime(2026, 6, 20, 11, 0, 0)
    assert cl._as_utc(naive).tzinfo == timezone.utc
    aware = datetime(2026, 6, 20, 11, 0, 0, tzinfo=timezone.utc)
    assert cl._as_utc(aware) is aware
    assert cl._as_utc(None) is None


# ── 2. recency anchored on close time, not execution time ───────────────────

@pytest.mark.asyncio
async def test_query_anchors_on_close_time(monkeypatch):
    coll = _install(monkeypatch, [_doc("XAUUSD", 5)])
    await cl.get_recent_closed_trades("XAUUSD", lookback_hours=24, limit=6)
    assert "trade_closed_at" in coll.last_filter
    assert "trade_executed_at" not in coll.last_filter


@pytest.mark.asyncio
async def test_close_beyond_lookback_excluded(monkeypatch):
    # Closed 30h ago — outside the 24h coarse DB filter.
    _install(monkeypatch, [_doc("BTCUSD", 30 * 60)])
    res = await cl.get_recent_closed_trades("BTCUSD", lookback_hours=24, limit=6)
    assert res == []


# ── 3. freshness cap drops pre-outage trades vs the live request time ───────

@pytest.mark.asyncio
async def test_max_bars_caps_stale_trades(monkeypatch):
    # A: 10 min (2 bars), B: 30 min (6 bars), C: 5h (60 bars, stale).
    _install(monkeypatch, [
        _doc("BTCUSD", 10), _doc("BTCUSD", 30), _doc("BTCUSD", 300),
    ])
    capped = await cl.get_recent_closed_trades("BTCUSD", lookback_hours=24, limit=6, max_bars=24)
    assert [r["bars_elapsed"] for r in capped] == [2, 6]  # 60-bar trade dropped

    uncapped = await cl.get_recent_closed_trades("BTCUSD", lookback_hours=24, limit=6, max_bars=None)
    assert [r["bars_elapsed"] for r in uncapped] == [2, 6, 60]  # all kept


@pytest.mark.asyncio
async def test_limit_respected_after_cap(monkeypatch):
    _install(monkeypatch, [_doc("BTCUSD", m) for m in (5, 10, 15, 20)])
    res = await cl.get_recent_closed_trades("BTCUSD", lookback_hours=24, limit=2, max_bars=24)
    assert len(res) == 2
    assert [r["bars_elapsed"] for r in res] == [1, 2]  # nearest two closes


# ── consumer: the re-entry gate reads the freshest close ────────────────────

@pytest.mark.asyncio
async def test_reentry_gate_picks_smallest_bars(monkeypatch):
    _install(monkeypatch, [_doc("BTCUSD", 30), _doc("BTCUSD", 5)])
    res = await cl.get_recent_closed_trades("BTCUSD", lookback_hours=24, limit=6, max_bars=24)
    latest = _most_recent_closed_trade(res)
    assert latest is not None
    assert latest["bars_elapsed"] == 1  # 5 min ago
    assert _most_recent_closed_trade([]) is None
    assert _most_recent_closed_trade([{"bars_elapsed": None}]) is None
