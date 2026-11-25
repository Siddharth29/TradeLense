"""Unit tests for the metrics/persistence repository functions added for the
live-dashboard work: SL-trail capture, realized-P&L aggregation, the daily-P&L
curve, equity samples, and token-spend aggregation.

Mongo is faked (no server required). The fakes mimic the Motor surface the
repos actually use: ``find().sort().limit().to_list()``, ``insert_one``,
``update_one``, ``create_index`` and ``aggregate()`` (both ``async for`` and
``.to_list()``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from packages.db.repositories import comprehensive_logging as cl
from packages.db.repositories import equity_samples as eq
from packages.db.repositories import gpt_responses as gptr


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


class _FakeAggCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def __aiter__(self):
        self._it = iter(self._docs)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration

    async def to_list(self, length=None):
        return list(self._docs)


class _FakeColl:
    """Configurable fake collection.

    * ``match`` — predicate(filter_dict) -> docs to return from ``find``; when
      omitted, every ``find`` returns ``find_docs``.
    * ``agg_docs`` — docs returned from ``aggregate``.
    """

    def __init__(self, find_docs=None, agg_docs=None, match=None):
        self.find_docs = find_docs or []
        self.agg_docs = agg_docs or []
        self._match = match
        self.inserted = []
        self.updated = []
        self.indexes = []

    def find(self, filt=None, projection=None):
        if self._match is not None:
            return _FakeCursor(self._match(filt or {}))
        return _FakeCursor(self.find_docs)

    def aggregate(self, pipeline):
        return _FakeAggCursor(self.agg_docs)

    async def insert_one(self, doc):
        self.inserted.append(doc)
        return type("R", (), {"inserted_id": "id1"})()

    async def update_one(self, filt, update):
        self.updated.append((filt, update))
        return None

    async def create_index(self, *a, **k):
        self.indexes.append(a)
        return "idx"


def _install_cl(monkeypatch, coll):
    client = {cl.MONGO_DB_NAME: {cl._COLLECTION_COMPREHENSIVE_TRADES: coll}}
    monkeypatch.setattr(cl, "get_client", lambda: client)
    monkeypatch.setattr(cl, "analytics_persistence_enabled", lambda: True)


def _install_eq(monkeypatch, coll):
    client = {eq.MONGO_DB_NAME: {eq._COLLECTION_EQUITY_SAMPLES: coll}}
    monkeypatch.setattr(eq, "get_client", lambda: client)
    monkeypatch.setattr(eq, "analytics_persistence_enabled", lambda: True)
    monkeypatch.setattr(eq, "_index_ensured", False)


# --------------------------------------------------------------------------
# append_sl_trail
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_append_sl_trail_pushes_to_matched_doc(monkeypatch):
    doc = {"_id": "abc", "symbol": "BTCUSDm", "position_ticket": "T1", "trade_status": "open"}
    coll = _FakeColl(match=lambda f: [doc] if f.get("position_ticket") == "T1" else [])
    _install_cl(monkeypatch, coll)

    ok = await cl.append_sl_trail("BTCUSDm", 101.5, label="trail-1", position_ticket="T1")

    assert ok is True
    assert len(coll.updated) == 1
    filt, update = coll.updated[0]
    assert "$push" in update and "sl_trail" in update["$push"]
    entry = update["$push"]["sl_trail"]
    assert entry["sl"] == 101.5 and entry["label"] == "trail-1"


@pytest.mark.asyncio
async def test_append_sl_trail_returns_false_when_no_open_doc(monkeypatch):
    coll = _FakeColl(match=lambda f: [])  # nothing ever matches
    _install_cl(monkeypatch, coll)

    ok = await cl.append_sl_trail("BTCUSDm", 101.5, position_ticket="T1")

    assert ok is False
    assert coll.updated == []


@pytest.mark.asyncio
async def test_append_sl_trail_logonly_is_noop_true(monkeypatch):
    monkeypatch.setattr(cl, "analytics_persistence_enabled", lambda: False)
    # get_client must NOT be called in log-only mode.
    monkeypatch.setattr(cl, "get_client", lambda: (_ for _ in ()).throw(AssertionError("called")))

    ok = await cl.append_sl_trail("BTCUSDm", 101.5, position_ticket="T1")
    assert ok is True


# --------------------------------------------------------------------------
# aggregate_realized_pnl
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_aggregate_realized_pnl_maps_group_docs(monkeypatch):
    coll = _FakeColl(agg_docs=[
        {"_id": "BTCUSD", "realized_pnl": 42.5, "wins": 3, "losses": 1, "trades": 4},
        {"_id": "XAUUSD", "realized_pnl": -7.0, "wins": 0, "losses": 2, "trades": 2},
    ])
    _install_cl(monkeypatch, coll)

    out = await cl.aggregate_realized_pnl(since=datetime.now(timezone.utc) - timedelta(days=1))

    assert out["BTCUSD"] == {"realized_pnl": 42.5, "wins": 3, "losses": 1, "trades": 4}
    assert out["XAUUSD"]["realized_pnl"] == -7.0


@pytest.mark.asyncio
async def test_aggregate_realized_pnl_empty_when_disabled(monkeypatch):
    monkeypatch.setattr(cl, "analytics_persistence_enabled", lambda: False)
    out = await cl.aggregate_realized_pnl()
    assert out == {}


# --------------------------------------------------------------------------
# get_daily_pnl_curve
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_daily_pnl_curve_accumulates(monkeypatch):
    base = datetime(2026, 6, 27, 6, 0, tzinfo=timezone.utc)
    coll = _FakeColl(find_docs=[
        {"trade_closed_at": base, "pnl": 5.0},
        {"trade_closed_at": base + timedelta(minutes=5), "pnl": -2.0},
        {"trade_closed_at": base + timedelta(minutes=10), "pnl": 3.0},
    ])
    _install_cl(monkeypatch, coll)

    pts = await cl.get_daily_pnl_curve(base - timedelta(hours=1))

    assert [p["pnl"] for p in pts] == [5.0, 3.0, 6.0]  # cumulative
    assert all(p["time"].tzinfo is not None for p in pts)


@pytest.mark.asyncio
async def test_get_daily_pnl_curve_empty_when_disabled(monkeypatch):
    monkeypatch.setattr(cl, "analytics_persistence_enabled", lambda: False)
    assert await cl.get_daily_pnl_curve(datetime.now(timezone.utc)) == []


# --------------------------------------------------------------------------
# equity_samples
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_insert_equity_sample_writes_and_indexes(monkeypatch):
    coll = _FakeColl()
    _install_eq(monkeypatch, coll)

    ok = await eq.insert_equity_sample("mt5", 1010.5, balance=1000.0)

    assert ok is True
    assert len(coll.inserted) == 1
    assert coll.inserted[0]["equity"] == 1010.5
    assert coll.inserted[0]["balance"] == 1000.0
    assert coll.indexes  # index ensured on first write


@pytest.mark.asyncio
async def test_get_equity_samples_filters_and_orders(monkeypatch):
    now = datetime.now(timezone.utc)
    coll = _FakeColl(find_docs=[
        {"timestamp": now, "equity": 1000.0},
        {"timestamp": now + timedelta(minutes=1), "equity": 1005.0},
    ])
    _install_eq(monkeypatch, coll)

    rows = await eq.get_equity_samples(now - timedelta(hours=1), end=now + timedelta(hours=1))
    assert len(rows) == 2
    assert rows[1]["equity"] == 1005.0


@pytest.mark.asyncio
async def test_get_equity_samples_empty_when_disabled(monkeypatch):
    monkeypatch.setattr(eq, "analytics_persistence_enabled", lambda: False)
    assert await eq.get_equity_samples(datetime.now(timezone.utc)) == []


# --------------------------------------------------------------------------
# aggregate_token_spend
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_aggregate_token_spend_sums(monkeypatch):
    coll = _FakeColl(agg_docs=[{"_id": None, "tokens": 4321}])

    async def fake_coll():
        return coll

    monkeypatch.setattr(gptr, "get_gpt_responses_collection", fake_coll)
    monkeypatch.setattr(gptr, "analytics_persistence_enabled", lambda: True)

    total = await gptr.aggregate_token_spend(since=datetime.now(timezone.utc) - timedelta(days=1))
    assert total == 4321.0


@pytest.mark.asyncio
async def test_aggregate_token_spend_zero_when_disabled(monkeypatch):
    monkeypatch.setattr(gptr, "analytics_persistence_enabled", lambda: False)
    assert await gptr.aggregate_token_spend() == 0.0
