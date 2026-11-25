"""Tests for the in-memory recent-trades ring buffer (2026-06-22).

The prompt's RECENT_TRADES context is now served from a process-global
``deque(maxlen=N)`` per asset instead of a freshness-capped Mongo query per
cycle. Every full close pushes one record; every AI call reads the last N back
with no DB round-trip and no freshness cap, so the section is always populated.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from packages.core.services import recent_trades_memory as rtm
from packages.db.repositories import comprehensive_logging as cl


def _at(minutes_ago: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)


def setup_function(_):
    rtm.reset()


def test_keeps_last_three_newest_first():
    for i, pnl in enumerate([-1.0, 2.0, -3.0, 4.0, -5.0]):
        rtm.record_closed_trade(
            "BTCUSD", side="long", entry=100.0, exit=101.0,
            outcome="MT5_SL", pnl=pnl, closed_at=_at(50 - i),
        )
    out = rtm.get_recent_trades("BTCUSD")
    assert [r["pnl"] for r in out] == [-5.0, 4.0, -3.0]  # last 3, newest first


def test_partial_rows_excluded():
    rtm.record_closed_trade("XAUUSD", side="long", entry=2000, exit=2010,
                            outcome="TP1_PARTIAL", pnl=5)
    rtm.record_closed_trade("XAUUSD", side="long", entry=2000, exit=2010,
                            outcome="TP2_PARTIAL", pnl=5)
    assert rtm.get_recent_trades("XAUUSD") == []


def test_result_from_pnl_not_label():
    # Trailing SL: a winner exits labelled "MT5_SL" — result must read from money.
    rtm.record_closed_trade("BTCUSD", side="sell", entry=100, exit=99,
                            outcome="MT5_SL", pnl=6.45, closed_at=_at(5))
    out = rtm.get_recent_trades("BTCUSD")
    assert out[0]["outcome"] == "MT5_SL" and out[0]["result"] == "win"


def test_ago_min_recomputed_per_read():
    rtm.record_closed_trade("BTCUSD", side="buy", entry=100, exit=101,
                            outcome="TP2", pnl=1.0, closed_at=_at(30))
    out = rtm.get_recent_trades("BTCUSD")
    assert out[0]["ago_min"] in (29, 30, 31)
    assert out[0]["bars_elapsed"] == out[0]["ago_min"] // 5


def test_side_normalized():
    rtm.record_closed_trade("BTCUSD", side="long", entry=1, exit=2,
                            outcome="TP2", pnl=1.0, closed_at=_at(1))
    assert rtm.get_recent_trades("BTCUSD")[0]["side"] == "buy"


def test_seed_only_when_empty():
    rtm.seed_from_views("GBPUSD", [
        {"ago_min": 30, "side": "sell", "entry": 1.32, "exit": 1.31,
         "move_pct": 0.7, "outcome": "TP2", "result": "win", "pnl": 10.0},
    ])
    assert rtm.get_recent_trades("GBPUSD")[0]["pnl"] == 10.0
    # A live close is the source of truth; a later seed must not clobber it.
    rtm.record_closed_trade("GBPUSD", side="buy", entry=1.30, exit=1.29,
                            outcome="MT5_SL", pnl=-2.0, closed_at=_at(1))
    rtm.seed_from_views("GBPUSD", [
        {"ago_min": 99, "side": "sell", "entry": 9, "exit": 9,
         "outcome": "X", "result": "loss", "pnl": -1.0},
    ])
    front = rtm.get_recent_trades("GBPUSD")[0]
    assert front["side"] == "buy" and front["pnl"] == -2.0


# ── close hook through the real update path ────────────────────────────────

class _FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, field, direction):
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    async def to_list(self, length=None):
        return list(self._docs)


class _FakeColl:
    def __init__(self, doc):
        self.doc = doc
        self.updated = []

    def find(self, filt, projection=None):
        if self.doc.get("trade_status") != "open":
            return _FakeCursor([])
        if filt.get("position_ticket") == self.doc.get("position_ticket"):
            return _FakeCursor([self.doc])
        if filt.get("symbol") == self.doc.get("symbol"):
            return _FakeCursor([self.doc])
        return _FakeCursor([])

    async def update_one(self, filt, update):
        self.updated.append((filt, update))
        self.doc["trade_status"] = "closed"
        return None


@pytest.mark.asyncio
async def test_close_populates_buffer(monkeypatch):
    # This test exercises the Mongo close path (mocked collection), so it must
    # run with analytics persistence enabled — the default is now log-only.
    monkeypatch.setenv("PERSIST_ANALYTICS_TO_MONGO", "1")
    doc = {"_id": "abc", "symbol": "BTCUSDm", "asset": "BTCUSD",
           "position_ticket": "T1", "side": "sell", "entry_price": 100.0,
           "trade_status": "open"}
    coll = _FakeColl(doc)
    client = {cl.MONGO_DB_NAME: {cl._COLLECTION_COMPREHENSIVE_TRADES: coll}}
    monkeypatch.setattr(cl, "get_client", lambda: client)

    ok = await cl.update_comprehensive_trade_exit(
        symbol="BTCUSDm", exit_price=99.0, exit_reason="MT5_SL", pnl=6.45,
        position_ticket="T1",
    )
    assert ok is True
    # Recorded under the *asset* key (BTCUSD), not the suffixed symbol.
    out = rtm.get_recent_trades("BTCUSD")
    assert len(out) == 1
    assert out[0]["side"] == "sell" and out[0]["result"] == "win"
    assert out[0]["outcome"] == "MT5_SL" and out[0]["entry"] == 100.0
    assert rtm.get_recent_trades("BTCUSDm") == []
