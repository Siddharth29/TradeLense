import pytest
from datetime import datetime, timezone

from packages.db.repositories import read


class _FakeCursor:
    def __init__(self, docs):
        self.docs = docs
        self._limit = None

    def sort(self, *args, **kwargs):  # pragma: no cover - trivial passthrough
        return self

    def limit(self, n):  # pragma: no cover - simple assignment
        self._limit = n
        return self

    async def to_list(self, length=None):
        if self._limit is not None:
            return self.docs[: self._limit]
        return self.docs


class _FakeCollection:
    def __init__(self, docs):
        self.docs = docs
        self.last_filter = None
        self.last_projection = None

    def find(self, filt, projection):
        self.last_filter = filt
        self.last_projection = projection
        return _FakeCursor(self.docs)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "func, coll_name",
    [
        (read.get_trades, "trades"),
        (read.get_orders, "orders"),
        (read.get_fills, "fills"),
        (read.list_signals, "signals"),
    ],
)
async def test_asset_time_queries(monkeypatch, func, coll_name):
    docs = [{"asset": "XYZ", "time": 1}]
    coll = _FakeCollection(docs)
    client = {read.MONGO_DB_NAME: {coll_name: coll}}
    monkeypatch.setattr(read, "get_client", lambda: client)

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    result = await func("XYZ", start=start, limit=1)
    assert result == docs
    assert coll.last_projection == {"_id": 0}
    assert coll.last_filter["asset"] == "XYZ"
    assert coll.last_filter["time"]["$gte"] == start


@pytest.mark.asyncio
async def test_list_logs(monkeypatch):
    docs = [{"time": 1}, {"time": 2}]
    coll = _FakeCollection(docs)
    client = {read.MONGO_DB_NAME: {"logs": coll}}
    monkeypatch.setattr(read, "get_client", lambda: client)

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    result = await read.list_logs(start=start, limit=1)
    assert result == docs[:1]
    assert coll.last_filter == {"time": {"$gte": start}}
    assert coll.last_projection == {"_id": 0}


@pytest.mark.asyncio
async def test_get_candles(monkeypatch):
    docs = [{"asset": "XYZ", "time": 1}, {"asset": "XYZ", "time": 2}]
    coll = _FakeCollection(docs)
    client = {read.MONGO_DB_NAME: {"candles_5m": coll}}
    monkeypatch.setattr(read, "get_client", lambda: client)

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 2, tzinfo=timezone.utc)
    result = await read.get_candles("XYZ", start=start, end=end, limit=2)
    assert result == docs[:2]
    assert coll.last_filter["asset"] == "XYZ"
    assert coll.last_filter["time"] == {"$gte": start, "$lt": end}
    assert coll.last_projection == {"_id": 0}
