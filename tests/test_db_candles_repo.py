import pytest
from datetime import datetime, timezone, timedelta

from packages.db.repositories import candles


class _FakeCursor:
    def __init__(self, docs):
        self.docs = docs
        self._limit = None

    def sort(self, *args, **kwargs):  # pragma: no cover - passthrough
        return self

    def limit(self, n):  # pragma: no cover - simple setter
        self._limit = n
        return self

    async def to_list(self, length=None):
        if self._limit is None:
            return self.docs
        return self.docs[: self._limit]


class _FakeCollection:
    def __init__(self):
        self.docs: list[dict] = []
        self.last_filter = None
        self.last_projection = None

    async def update_one(self, filt, update, *, upsert=False):
        doc = update["$set"]
        for i, existing in enumerate(self.docs):
            if existing["asset"] == filt["asset"] and existing["ts"] == filt["ts"]:
                self.docs[i] = doc
                return
        self.docs.append(doc)

    def find(self, filt, projection):
        self.last_filter = filt
        self.last_projection = projection
        docs = [d for d in self.docs if d["asset"] == filt.get("asset")]
        if ts_cond := filt.get("ts"):
            if "$gte" in ts_cond:
                docs = [d for d in docs if d["ts"] >= ts_cond["$gte"]]
            if "$lt" in ts_cond:
                docs = [d for d in docs if d["ts"] < ts_cond["$lt"]]
        docs.sort(key=lambda d: d["ts"])
        return _FakeCursor(docs)


@pytest.mark.asyncio
async def test_insert_many_upserts(monkeypatch):
    coll = _FakeCollection()
    client = {candles.MONGO_DB_NAME: {"candles": coll}}
    monkeypatch.setattr(candles, "get_client", lambda: client)

    ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
    doc = {"asset": "XYZ", "ts": ts, "open": 1, "high": 1, "low": 1, "close": 1}
    await candles.insert_many([doc])
    doc2 = {"asset": "XYZ", "ts": ts, "open": 2, "high": 2, "low": 2, "close": 2}
    await candles.insert_many([doc2])
    assert coll.docs == [doc2]


@pytest.mark.asyncio
async def test_get_many_filters(monkeypatch):
    coll = _FakeCollection()
    client = {candles.MONGO_DB_NAME: {"candles": coll}}
    monkeypatch.setattr(candles, "get_client", lambda: client)

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    later = start + timedelta(minutes=5)
    await candles.insert_many([
        {"asset": "XYZ", "ts": start, "open": 1, "high": 1, "low": 1, "close": 1},
        {"asset": "XYZ", "ts": later, "open": 2, "high": 2, "low": 2, "close": 2},
    ])

    result = await candles.get_many("XYZ", start=start, end=later, limit=1)
    assert len(result) == 1
    assert coll.last_filter["asset"] == "XYZ"
    assert coll.last_filter["ts"] == {"$gte": start, "$lt": later}
    assert coll.last_projection == {"_id": 0}


class _FakeCollectionTime:
    def __init__(self):
        self.docs: list[dict] = []
        self.last_filter = None
        self.last_projection = None

    async def update_one(self, filt, update, *, upsert=False):
        doc = update["$set"]
        for i, existing in enumerate(self.docs):
            if existing["asset"] != filt["asset"]:
                continue
            if "slot_id" in filt and existing.get("slot_id") == filt.get("slot_id"):
                self.docs[i] = doc
                return
            if "time" in filt and existing.get("time") == filt.get("time"):
                self.docs[i] = doc
                return
        self.docs.append(doc)

    def find(self, filt, projection):
        self.last_filter = filt
        self.last_projection = projection
        docs = [d for d in self.docs if d["asset"] == filt.get("asset")]
        if t_cond := filt.get("time"):
            if "$gte" in t_cond:
                docs = [d for d in docs if d["time"] >= t_cond["$gte"]]
            if "$lt" in t_cond:
                docs = [d for d in docs if d["time"] < t_cond["$lt"]]
        docs.sort(key=lambda d: d["time"])
        return _FakeCursor(docs)


@pytest.mark.asyncio
async def test_insert_get_many_5m(monkeypatch):
    coll = _FakeCollectionTime()
    client = {candles.MONGO_DB_NAME: {"candles_5m": coll}}
    monkeypatch.setattr(candles, "get_client", lambda: client)

    ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
    doc = {"asset": "XYZ", "time": ts, "open": 1, "high": 1, "low": 1, "close": 1}
    await candles.insert_many_5m([doc])
    result = await candles.get_many_5m("XYZ")
    assert result == [doc]


@pytest.mark.asyncio
async def test_insert_get_many_15m(monkeypatch):
    coll = _FakeCollectionTime()
    client = {candles.MONGO_DB_NAME: {"candles_15m": coll}}
    monkeypatch.setattr(candles, "get_client", lambda: client)

    ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
    doc = {"asset": "XYZ", "time": ts, "open": 1, "high": 1, "low": 1, "close": 1}
    await candles.insert_many_15m([doc])
    result = await candles.get_many_15m("XYZ")
    assert result == [doc]


@pytest.mark.asyncio
async def test_insert_many_uses_slot_id(monkeypatch):
    coll = _FakeCollectionTime()
    client = {candles.MONGO_DB_NAME: {"candles_5m": coll}}
    monkeypatch.setattr(candles, "get_client", lambda: client)

    ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
    docs = [
        {"asset": "XYZ", "slot_id": 1, "time": ts, "open": 1, "high": 1, "low": 1, "close": 1},
        {"asset": "XYZ", "slot_id": 1, "time": ts, "open": 2, "high": 2, "low": 2, "close": 2},
    ]
    await candles.insert_many_5m(docs)
    assert coll.docs == [docs[-1]]
