"""Unit tests for the core trade-persistence repository (comprehensive_logging).

Covers the log-only code paths (in-process registry + durable events, no Mongo)
and the Mongo paths via a faked collection. These are read/observe-only tests of
existing behaviour — they do not modify the live trade-recording logic.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from packages.db.repositories import comprehensive_logging as cl


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, *a, **k):
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    async def to_list(self, length=None):
        return list(self._docs)


class _FakeColl:
    def __init__(self, find_docs=None, deleted=1):
        self.find_docs = find_docs or []
        self._deleted = deleted
        self.inserted = []

    def find(self, filt=None, projection=None):
        return _FakeCursor(self.find_docs)

    async def delete_one(self, filt):
        return type("R", (), {"deleted_count": self._deleted})()

    async def insert_one(self, doc):
        self.inserted.append(doc)
        return type("R", (), {"inserted_id": "mongoid-1"})()


def _install(monkeypatch, coll, enabled=True):
    client = {cl.MONGO_DB_NAME: {cl._COLLECTION_COMPREHENSIVE_TRADES: coll}}
    monkeypatch.setattr(cl, "get_client", lambda: client)
    monkeypatch.setattr(cl, "analytics_persistence_enabled", lambda: enabled)


# --------------------------------------------------------------------------
# _exit_price_sane (pure)
# --------------------------------------------------------------------------

def test_exit_price_sane_branches():
    assert cl._exit_price_sane({"entry_price": 100.0}, 105.0) is True   # within 20%
    assert cl._exit_price_sane({"entry_price": 100.0}, 130.0) is False  # >20% away
    assert cl._exit_price_sane({"entry_price": 0.0}, 999.0) is True     # no baseline → accept
    assert cl._exit_price_sane({"entry_price": "bad"}, 1.0) is True     # bad type → accept


# --------------------------------------------------------------------------
# store_comprehensive_trade — log-only path
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_store_comprehensive_trade_logonly_returns_synthetic_id(monkeypatch):
    monkeypatch.setattr(cl, "analytics_persistence_enabled", lambda: False)
    captured = {}
    monkeypatch.setattr(cl, "register_open_trade", lambda key, info: captured.update({"key": key, "info": info}))

    tid = await cl.store_comprehensive_trade(
        symbol="BTCUSDm",
        asset="BTCUSD",
        quantity=0.01,
        price=65000.0,
        entry_price=65000.0,
        agent_id="a1",
        prompt_hash="h1",
        signal_received_at=datetime.now(timezone.utc),
        trade_executed_at=datetime.now(timezone.utc),
        side="buy",
        position_ticket="T1",
    )

    assert isinstance(tid, str) and tid
    # Registered under the broker ticket with the entry context for close matching.
    assert captured["key"] == "T1"
    assert captured["info"]["asset"] == "BTCUSD"
    assert captured["info"]["entry_price"] == 65000.0


# --------------------------------------------------------------------------
# update_comprehensive_trade_exit — log-only path
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_update_exit_logonly_records_and_returns_true(monkeypatch):
    monkeypatch.setattr(cl, "analytics_persistence_enabled", lambda: False)
    monkeypatch.setattr(cl, "pop_open_trade", lambda pt, tid: {"asset": "BTCUSD", "side": "buy", "entry_price": 100.0})
    recorded = {}

    import packages.core.services.recent_trades_memory as rtm
    monkeypatch.setattr(rtm, "record_closed_trade", lambda *a, **k: recorded.update(k) or recorded.update({"args": a}))

    ok = await cl.update_comprehensive_trade_exit(
        symbol="BTCUSDm", exit_price=101.0, exit_reason="TP2", pnl=1.0, position_ticket="T1",
    )

    assert ok is True
    assert recorded.get("pnl") == 1.0
    assert recorded.get("outcome") == "TP2"


# --------------------------------------------------------------------------
# get_comprehensive_trades / get_all_comprehensive_trades (Mongo path)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_comprehensive_trades_returns_docs(monkeypatch):
    docs = [{"asset": "BTCUSD", "pnl": 1.0}, {"asset": "BTCUSD", "pnl": 2.0}]
    _install(monkeypatch, _FakeColl(find_docs=docs))
    out = await cl.get_comprehensive_trades("BTCUSD", limit=10)
    assert out == docs


@pytest.mark.asyncio
async def test_get_all_comprehensive_trades_returns_docs(monkeypatch):
    docs = [{"asset": "XAUUSD"}]
    _install(monkeypatch, _FakeColl(find_docs=docs))
    out = await cl.get_all_comprehensive_trades(limit=5)
    assert out == docs


# --------------------------------------------------------------------------
# delete_comprehensive_trade (Mongo path)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_comprehensive_trade_true_when_removed(monkeypatch):
    _install(monkeypatch, _FakeColl(deleted=1))
    ok = await cl.delete_comprehensive_trade("507f1f77bcf86cd799439011")
    assert ok is True


@pytest.mark.asyncio
async def test_delete_comprehensive_trade_false_when_missing(monkeypatch):
    _install(monkeypatch, _FakeColl(deleted=0))
    ok = await cl.delete_comprehensive_trade("507f1f77bcf86cd799439011")
    assert ok is False


# --------------------------------------------------------------------------
# store_comprehensive_trade — Mongo path
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_store_comprehensive_trade_mongo_inserts_and_returns_id(monkeypatch):
    coll = _FakeColl()
    _install(monkeypatch, coll, enabled=True)

    tid = await cl.store_comprehensive_trade(
        symbol="XAUUSDm",
        asset="XAUUSD",
        quantity=0.01,
        price=2400.0,
        entry_price=2400.0,
        agent_id="a1",
        prompt_hash="h1",
        signal_received_at=datetime.now(timezone.utc),
        trade_executed_at=datetime.now(timezone.utc),
        side="sell",
        stop_loss=2410.0,
        take_profit=2380.0,
    )

    assert tid == "mongoid-1"
    assert len(coll.inserted) == 1
    doc = coll.inserted[0]
    assert doc["asset"] == "XAUUSD" and doc["entry_price"] == 2400.0
    assert doc["trade_status"] == "open"
