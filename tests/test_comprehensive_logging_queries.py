"""Query + helper coverage for comprehensive_logging: recent-closed-trades
(AI prompt context), slot/hourly getters, strategy-expiry and slot deletion.

All via faked Mongo collections — no server required, no live logic touched.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from packages.db.repositories import comprehensive_logging as cl


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
    def __init__(self, find_docs=None, deleted=1, modified=1):
        self.find_docs = find_docs or []
        self._deleted = deleted
        self._modified = modified
        self.inserted = []
        self.updated = []

    def find(self, filt=None, projection=None):
        return _FakeCursor(self.find_docs)

    async def to_list(self, length=None):
        return list(self.find_docs)

    async def insert_one(self, doc):
        self.inserted.append(doc)
        return type("R", (), {"inserted_id": "sid-1"})()

    async def delete_one(self, filt):
        return type("R", (), {"deleted_count": self._deleted})()

    async def update_one(self, filt, update):
        self.updated.append((filt, update))
        return type("R", (), {"modified_count": self._modified})()


def _install(monkeypatch, colls: dict, enabled=True):
    """colls maps collection-name -> fake collection."""
    client = {cl.MONGO_DB_NAME: colls}
    monkeypatch.setattr(cl, "get_client", lambda: client)
    monkeypatch.setattr(cl, "analytics_persistence_enabled", lambda: enabled)


# --------------------------------------------------------------------------
# get_recent_closed_trades
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recent_closed_trades_shapes_and_freshness_cap(monkeypatch):
    now = datetime.now(timezone.utc)
    docs = [
        {"side": "long", "entry_price": 100.0, "exit_price": 105.0, "pnl": 5.0,
         "exit_reason": "MT5_SL", "trade_closed_at": now - timedelta(minutes=10)},
        {"side": "short", "entry_price": 200.0, "exit_price": 210.0, "pnl": -8.0,
         "exit_reason": "SL", "trade_closed_at": now - timedelta(minutes=600)},  # stale
    ]
    _install(monkeypatch, {cl._COLLECTION_COMPREHENSIVE_TRADES: _FakeColl(find_docs=docs)})

    out = await cl.get_recent_closed_trades("BTCUSD", limit=6, max_bars=12)

    assert len(out) == 1  # the 600-min-old trade exceeds the 12-bar freshness cap
    row = out[0]
    assert row["result"] == "win" and row["side"] == "buy"
    assert row["move_pct"] == 5.0
    assert row["pnl"] == 5.0
    assert row["bars_elapsed"] == 2


@pytest.mark.asyncio
async def test_recent_closed_trades_loss_and_scratch(monkeypatch):
    now = datetime.now(timezone.utc)
    docs = [
        {"side": "short", "entry_price": 200.0, "exit_price": 198.0, "pnl": -1.0,
         "exit_reason": "SL", "trade_closed_at": now - timedelta(minutes=5)},
        {"side": "long", "entry_price": 50.0, "exit_price": 50.0, "pnl": 0.0,
         "exit_reason": "BE", "trade_closed_at": now - timedelta(minutes=6)},
    ]
    _install(monkeypatch, {cl._COLLECTION_COMPREHENSIVE_TRADES: _FakeColl(find_docs=docs)})

    out = await cl.get_recent_closed_trades("BTCUSD", limit=6)
    results = [r["result"] for r in out]
    assert "loss" in results and "scratch" in results


# --------------------------------------------------------------------------
# slot / hourly getters
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_getters_with_time_filters(monkeypatch):
    """Exercise the start_time/end_time filter branches of every getter."""
    a = datetime.now(timezone.utc) - timedelta(hours=2)
    b = datetime.now(timezone.utc)
    docs = [{"asset": "BTCUSD"}]
    _install(monkeypatch, {
        cl._COLLECTION_COMPREHENSIVE_TRADES: _FakeColl(find_docs=docs),
        cl._COLLECTION_SLOT_CANDLE_MATCH: _FakeColl(find_docs=docs),
        cl._COLLECTION_HOURLY_CANDLES: _FakeColl(find_docs=docs),
    })
    assert await cl.get_comprehensive_trades("BTCUSD", start_time=a, end_time=b) == docs
    assert await cl.get_all_comprehensive_trades(start_time=a, end_time=b) == docs
    assert await cl.get_slot_candle_matches("BTCUSD", start_time=a, end_time=b) == docs
    assert await cl.get_all_slot_candle_matches(start_time=a, end_time=b) == docs
    assert await cl.get_hourly_candles("BTCUSD", start_time=a, end_time=b) == docs
    assert await cl.get_all_hourly_candles(start_time=a, end_time=b) == docs


@pytest.mark.asyncio
async def test_slot_and_hourly_getters(monkeypatch):
    slot_docs = [{"asset": "BTCUSD", "slot_id": 1}]
    hourly_docs = [{"asset": "BTCUSD", "close": 100.0}]
    _install(monkeypatch, {
        cl._COLLECTION_SLOT_CANDLE_MATCH: _FakeColl(find_docs=slot_docs),
        cl._COLLECTION_HOURLY_CANDLES: _FakeColl(find_docs=hourly_docs),
    })

    assert await cl.get_slot_candle_matches("BTCUSD") == slot_docs
    assert await cl.get_all_slot_candle_matches() == slot_docs
    assert await cl.get_hourly_candles("BTCUSD") == hourly_docs
    assert await cl.get_all_hourly_candles() == hourly_docs


# --------------------------------------------------------------------------
# store_strategy_expiry + delete_slot_candle_match
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_store_strategy_expiry_logonly_and_mongo(monkeypatch):
    # log-only → synthetic id, no insert
    _install(monkeypatch, {cl._COLLECTION_STRATEGY_EXPIRIES: _FakeColl()}, enabled=False)
    sid = await cl.store_strategy_expiry({"asset": "BTCUSD", "reason": "expired"})
    assert isinstance(sid, str) and sid

    # mongo → inserted id
    coll = _FakeColl()
    _install(monkeypatch, {cl._COLLECTION_STRATEGY_EXPIRIES: coll}, enabled=True)
    mid = await cl.store_strategy_expiry({"asset": "BTCUSD"})
    assert mid == "sid-1"
    assert len(coll.inserted) == 1


@pytest.mark.asyncio
async def test_delete_slot_candle_match(monkeypatch):
    _install(monkeypatch, {cl._COLLECTION_SLOT_CANDLE_MATCH: _FakeColl(deleted=1)})
    assert await cl.delete_slot_candle_match("507f1f77bcf86cd799439011") is True

    _install(monkeypatch, {cl._COLLECTION_SLOT_CANDLE_MATCH: _FakeColl(deleted=0)})
    assert await cl.delete_slot_candle_match("507f1f77bcf86cd799439011") is False


# --------------------------------------------------------------------------
# store_slot_candle_match / store_hourly_candle / update trigger (Mongo writes)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_store_slot_candle_match_inserts(monkeypatch):
    coll = _FakeColl()
    _install(monkeypatch, {cl._COLLECTION_SLOT_CANDLE_MATCH: coll}, enabled=True)
    now = datetime.now(timezone.utc)

    sid = await cl.store_slot_candle_match(
        asset="BTCUSD", slot_id=1, slot_timestamp=now, gpt_response_id="g1",
        level_drawn_at=now, level_value=65000.0, direction="long", trigger_met=True,
        agent_id="a1", prompt_hash="h1",
        entry_price=65000.0, stop_loss=64500.0, take_profit=66000.0,
        trigger_candle_open=64900.0, trigger_candle_close=65010.0,
    )
    assert sid == "sid-1"
    assert len(coll.inserted) == 1
    assert coll.inserted[0]["asset"] == "BTCUSD"


@pytest.mark.asyncio
async def test_store_hourly_candle_inserts(monkeypatch):
    coll = _FakeColl()
    _install(monkeypatch, {cl._COLLECTION_HOURLY_CANDLES: coll}, enabled=True)
    now = datetime.now(timezone.utc)

    sid = await cl.store_hourly_candle(
        asset="BTCUSD", symbol="BTCUSDm", slot_id=1, slot_timestamp=now,
        open=100.0, high=110.0, low=95.0, close=105.0, candle_timestamp=now,
        volume=1234.0, market_session="london",
    )
    assert sid == "sid-1"
    assert coll.inserted[0]["close"] == 105.0


@pytest.mark.asyncio
async def test_update_slot_candle_match_trigger(monkeypatch):
    coll = _FakeColl(modified=1)
    _install(monkeypatch, {cl._COLLECTION_SLOT_CANDLE_MATCH: coll}, enabled=True)
    now = datetime.now(timezone.utc)

    ok = await cl.update_slot_candle_match_trigger(
        "mid1", trigger_met=True, trigger_timestamp=now,
        trigger_candle_open=100.0, trigger_candle_high=101.0, trigger_candle_low=99.0,
        trigger_candle_close=100.5, trigger_candle_volume=10.0,
        entry_price=100.5, trade_executed=True, trade_id="t1",
    )
    assert ok is True
    filt, update = coll.updated[0]
    assert update["$set"]["trigger_met"] is True
    assert update["$set"]["entry_price"] == 100.5

    coll2 = _FakeColl(modified=0)
    _install(monkeypatch, {cl._COLLECTION_SLOT_CANDLE_MATCH: coll2}, enabled=True)
    assert await cl.update_slot_candle_match_trigger("mid1", trigger_met=False) is False
