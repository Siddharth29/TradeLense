import pytest

from packages.core.config import MONGO_DB_NAME
from packages.db import bootstrap


class FakeCollection:
    def __init__(self, name: str):
        self.name = name
        self.created_indexes = []

    async def create_index(self, keys, **kwargs):
        self.created_indexes.append((keys, kwargs))


class FakeDB:
    def __init__(self):
        self.collections = {}

    async def list_collection_names(self):
        return list(self.collections.keys())

    async def create_collection(self, name):
        coll = FakeCollection(name)
        self.collections[name] = coll
        return coll

    def __getitem__(self, name):
        if name not in self.collections:
            self.collections[name] = FakeCollection(name)
        return self.collections[name]


class FakeClient:
    def __init__(self):
        self.dbs = {}

    def __getitem__(self, name):
        if name not in self.dbs:
            self.dbs[name] = FakeDB()
        return self.dbs[name]


@pytest.mark.asyncio
async def test_init_db_creates_collections_and_indexes(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(bootstrap, "get_client", lambda: client)

    await bootstrap.init_db()

    db = client[MONGO_DB_NAME]
    expected = {
        "trades",
        "orders",
        "fills",
        "signals",
        "logs",
        "candles_5m",
        "candles_15m",
        "daily_metrics",
        "candles",
        "broker_sessions",
        "wallet_snapshots",
        "agent_runs",
    }
    assert set(db.collections.keys()) == expected

    for name, coll in db.collections.items():
        keys = [k for k, _ in coll.created_indexes]
        if name == "candles":
            assert [("asset", 1), ("ts", 1)] in keys
            assert "client_order_id" not in keys
        elif name == "broker_sessions":
            assert "expires_at" in keys
            ttl_opts = next(o for k, o in coll.created_indexes if k == "expires_at")
            assert ttl_opts.get("expireAfterSeconds") == 0
        elif name == "wallet_snapshots":
            assert "as_of" in keys
            ttl_opts = next(o for k, o in coll.created_indexes if k == "as_of")
            assert ttl_opts.get("expireAfterSeconds") == 60 * 60 * 24 * 7
        elif name == "agent_runs":
            assert [("asset", 1), ("slot_id", 1)] in keys
            ttl_opts = next(o for k, o in coll.created_indexes if k == "time")
            assert ttl_opts.get("expireAfterSeconds") == 60 * 60 * 24 * 30
        else:
            assert [("asset", 1), ("time", 1)] in keys
            assert "client_order_id" in keys
            unique_opts = next(
                o for k, o in coll.created_indexes if k == "client_order_id"
            )
            assert unique_opts.get("unique") is True
            if name in {"trades", "orders", "fills", "signals"}:
                assert "checksum" in keys
                chk_opts = next(o for k, o in coll.created_indexes if k == "checksum")
                assert chk_opts.get("unique") is True
            if name in {"logs", "signals"}:
                ttl_opts = next(o for k, o in coll.created_indexes if k == "time")
                expected_ttl = (
                    60 * 60 * 24 if name == "logs" else 60 * 60 * 24 * 30
                )
                assert ttl_opts.get("expireAfterSeconds") == expected_ttl
