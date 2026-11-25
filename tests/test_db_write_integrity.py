import pytest
from datetime import datetime, timezone
from pymongo.errors import DuplicateKeyError

from datetime import datetime, timezone

import pytest
from pymongo.errors import DuplicateKeyError

from packages.db.models import TradeDoc
from packages.db.repositories import write


class _FakeUpdateResult:
    def __init__(self, upserted_id=None, matched_count=0):
        self.upserted_id = upserted_id
        self.matched_count = matched_count


class _FakeCollection:
    def __init__(self):
        self.docs: list[dict] = []

    async def update_one(self, filt, update, *, upsert=False):
        doc = update["$set"]
        for existing in self.docs:
            if (
                existing["client_order_id"] == doc["client_order_id"]
                or existing["checksum"] == doc["checksum"]
            ):
                raise DuplicateKeyError("duplicate key error")
        self.docs.append(doc)
        return _FakeUpdateResult(upserted_id=len(self.docs))


@pytest.mark.asyncio
async def test_duplicate_insert_logged_and_not_stored(monkeypatch):
    coll = _FakeCollection()
    db = {"trades": coll}
    client = {write.MONGO_DB_NAME: db}
    monkeypatch.setattr(write, "get_client", lambda: client)

    logged: list[dict] = []

    class DummyLogger:
        def error(self, ctx, msg, **fields):
            logged.append({"ctx": ctx, "msg": msg, **fields})

    monkeypatch.setattr(write, "logger", DummyLogger())

    trade1 = TradeDoc(
        symbol="XYZ",
        quantity=1.0,
        price=10.0,
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
        client_order_id="abc",
        checksum="chk",
        agent_id="agent",
        prompt_hash="ph1",
    )
    trade2 = TradeDoc(
        symbol="XYZ",
        quantity=1.0,
        price=10.0,
        timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
        client_order_id="def",
        checksum="chk",
        agent_id="agent",
        prompt_hash="ph2",
    )

    await write.insert_trades([trade1])
    await write.insert_trades([trade2])

    assert len(coll.docs) == 1
    assert logged
    entry = logged[0]
    assert entry["ctx"] == {"collection": "trades"}
    assert entry["client_order_id"] == "def"
    assert "duplicate key" in entry["error"]
