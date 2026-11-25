import os

os.environ.setdefault("SESSION_SECRET", "testsecret")

from fastapi.testclient import TestClient
import pytest

from apps.backend import api
from packages.db.repositories import configs


class _InsertResult:
    def __init__(self, inserted_id):
        self.inserted_id = inserted_id


class _UpdateResult:
    def __init__(self, *, upserted_id=None, matched_count=0):
        self.upserted_id = upserted_id
        self.matched_count = matched_count


class _Cursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, key, direction):
        reverse = direction < 0
        self._docs.sort(key=lambda d: d[key], reverse=reverse)
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    async def to_list(self, length=None):  # pragma: no cover - passthrough
        return list(self._docs)


class _Collection:
    def __init__(self):
        self.docs = []

    def with_options(self, **kwargs):  # pragma: no cover - passthrough
        return self

    async def find_one(self, filt, projection=None):
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in filt.items()):
                if projection:
                    return {k: doc[k] for k in doc if projection.get(k, 1)}
                return dict(doc)
        return None

    async def insert_one(self, doc):
        self.docs.append(dict(doc))
        return _InsertResult(len(self.docs))

    async def update_one(self, filt, update, *, upsert=False):
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in filt.items()):
                doc.update(update["$set"])
                return _UpdateResult(matched_count=1)
        if upsert:
            new_doc = {**filt, **update["$set"]}
            self.docs.append(new_doc)
            return _UpdateResult(upserted_id=len(self.docs))
        return _UpdateResult()

    def find(self, filt, projection=None):
        def _match(doc):
            for k, v in filt.items():
                val = doc.get(k)
                if isinstance(v, dict):
                    gt = v.get("$gt")
                    lt = v.get("$lt")
                    if gt is not None and not (val > gt):
                        return False
                    if lt is not None and not (val < lt):
                        return False
                else:
                    if val != v:
                        return False
            return True

        docs = [
            {k: d[k] for k in d if (projection or {}).get(k, 1)}
            for d in self.docs
            if _match(d)
        ]
        return _Cursor(docs)


@pytest.mark.asyncio
async def test_versions_and_rollback(monkeypatch):
    cfg_coll = _Collection()
    hist_coll = _Collection()

    async def _fake_get_client():
        return cfg_coll, hist_coll

    monkeypatch.setattr(configs, "get_client", _fake_get_client)

    await configs.put("svc", "dev", {"a": 1})
    await configs.put("svc", "dev", {"a": 2})

    client = TestClient(api.app)

    resp = client.get("/configs/svc/versions", params={"env": "dev"})
    assert resp.status_code == 200
    versions = resp.json()["versions"]
    assert [v["version"] for v in versions] == [1, 2]

    resp = client.get(
        "/configs/svc/versions", params={"env": "dev", "limit": 1}
    )
    page = resp.json()
    assert [v["version"] for v in page["versions"]] == [1]
    assert page["nextCursor"] == 1

    resp = client.get(
        "/configs/svc/versions", params={"env": "dev", "cursor": 1, "limit": 1}
    )
    page = resp.json()
    assert [v["version"] for v in page["versions"]] == [2]
    assert page["nextCursor"] is None

    resp = client.post(
        "/configs/svc/rollback", json={"env": "dev", "version": 1}
    )
    assert resp.status_code == 200
    assert resp.json()["version"] == 3

    doc, _ = await configs.get_current("svc", "dev")
    assert doc == {"a": 1}

