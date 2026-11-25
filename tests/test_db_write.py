import pytest
from datetime import datetime, timezone

from packages.db.models import TradeDoc, OrderDoc
from packages.db.repositories import write


class _FakeUpdateResult:
    def __init__(self, upserted_id=None, matched_count=0):
        self.upserted_id = upserted_id
        self.matched_count = matched_count


class _FakeCollection:
    def __init__(self, fail: bool = False):
        self.docs: list[dict] = []
        self.fail = fail

    async def update_one(self, filt, update, *, upsert=False):
        if self.fail:
            raise RuntimeError("boom")
        doc = update["$set"]
        for i, existing in enumerate(self.docs):
            if (
                existing["client_order_id"] == filt["$or"][0]["client_order_id"]
                or existing["checksum"] == filt["$or"][1]["checksum"]
            ):
                self.docs[i] = doc
                return _FakeUpdateResult(matched_count=1)
        self.docs.append(doc)
        return _FakeUpdateResult(upserted_id=len(self.docs))


class _FakeDB(dict):
    def __getitem__(self, name):  # pragma: no cover - simple passthrough
        return super().__getitem__(name)


class _FakeClient(dict):
    def __getitem__(self, name):  # pragma: no cover - simple passthrough
        return super().__getitem__(name)


@pytest.mark.asyncio
async def test_insert_trades_idempotent(monkeypatch):
    coll = _FakeCollection()
    db = {"trades": coll}
    client = {write.MONGO_DB_NAME: db}
    monkeypatch.setattr(write, "get_client", lambda: client)

    trade = TradeDoc(
        symbol="XYZ",
        quantity=1.0,
        price=10.0,
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
        client_order_id="abc",
        checksum="chk",
        agent_id="agent",
        prompt_hash="ph",
    )
    await write.insert_trades([trade])
    await write.insert_trades([trade])
    assert len(coll.docs) == 1


@pytest.mark.asyncio
async def test_insert_orders_idempotent(monkeypatch):
    coll = _FakeCollection()
    db = {"orders": coll}
    client = {write.MONGO_DB_NAME: db}
    monkeypatch.setattr(write, "get_client", lambda: client)

    order = OrderDoc(
        symbol="XYZ",
        side="buy",
        quantity=1.0,
        status="open",
        price=None,
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
        client_order_id="abc",
        checksum="chk",
    )
    await write.insert_orders([order])
    await write.insert_orders([order])
    assert len(coll.docs) == 1


@pytest.mark.asyncio
async def test_insert_orders_logs_error(monkeypatch):
    coll = _FakeCollection(fail=True)
    db = {"orders": coll}
    client = {write.MONGO_DB_NAME: db}
    monkeypatch.setattr(write, "get_client", lambda: client)

    logged: list[dict] = []

    class DummyLogger:
        def error(self, ctx, msg, **fields):  # pragma: no cover - simple wrapper
            logged.append({"ctx": ctx, "msg": msg, **fields})

    monkeypatch.setattr(write, "logger", DummyLogger())

    order = OrderDoc(
        symbol="XYZ",
        side="buy",
        quantity=1.0,
        status="open",
        price=None,
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
        client_order_id="ord1",
        checksum="chk1",
    )
    await write.insert_orders([order])
    assert logged
    entry = logged[0]
    assert entry["ctx"] == {"collection": "orders"}
    assert entry["error"] == "boom"
    assert entry["client_order_id"] == "ord1"
