import pytest

from packages.core.config import MONGO_DB_NAME
from packages.db.repositories import configs


class FakeCollection:
    def __init__(self, name: str):
        self.name = name
        self.created_indexes = []

    async def create_index(self, key, **kwargs):
        self.created_indexes.append((key, kwargs))

    def with_options(self, **kwargs):  # pragma: no cover - passthrough
        return self


class FakeDB:
    def __init__(self):
        self.collections = {}

    def __getitem__(self, name: str):
        if name not in self.collections:
            self.collections[name] = FakeCollection(name)
        return self.collections[name]


class FakeClient:
    def __init__(self):
        self.dbs = {}

    def __getitem__(self, name: str):
        if name not in self.dbs:
            self.dbs[name] = FakeDB()
        return self.dbs[name]


@pytest.mark.asyncio
async def test_get_client_returns_collections_and_ttl(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(configs, "_get_client", lambda: client)
    configs._TTL_ENSURED = False

    cfg_coll, history_coll = await configs.get_client()

    assert cfg_coll is client[MONGO_DB_NAME][configs.CONFIGS_COLLECTION]
    assert history_coll is client[MONGO_DB_NAME][configs.CONFIG_HISTORY_COLLECTION]

    # TTL index should be created once on the history collection
    created = [i for i in history_coll.created_indexes if i[0] == "updated_at"]
    assert len(created) == 1
    assert (
        created[0][1].get("expireAfterSeconds") == configs.HISTORY_TTL_SECONDS
    )

    # Subsequent calls should not create additional indexes
    await configs.get_client()
    created = [i for i in history_coll.created_indexes if i[0] == "updated_at"]
    assert len(created) == 1
