import pandas as pd
import pytest
from datetime import datetime, timedelta
from pathlib import Path

from scripts import archive_old_data as archive
from packages.db import bootstrap


class FakeCursor:
    def __init__(self, docs):
        self.docs = docs

    async def to_list(self, _):
        return self.docs


class FakeCollection:
    def __init__(self, docs, expected_path: Path):
        self.docs = docs
        self.expected_path = expected_path
        self.deleted = False

    def find(self, filt):
        self.filter = filt
        return FakeCursor(self.docs)

    async def delete_many(self, query):
        # Ensure the Parquet file has been written before deletion.
        assert self.expected_path.exists()
        self.deleted = True
        self.delete_query = query


@pytest.mark.asyncio
async def test_archive_collection_writes_and_deletes(monkeypatch, tmp_path):
    now = datetime(2024, 1, 1)
    ttl = bootstrap._SIGNAL_TTL_SECONDS
    docs = [
        {"_id": 1, "time": now - timedelta(seconds=ttl + 1), "asset": "ABC"}
    ]
    expected = tmp_path / f"signals_{int(now.timestamp())}.parquet"
    coll = FakeCollection(docs, expected)

    monkeypatch.setattr(pd.DataFrame, "to_parquet", lambda self, path: Path(path).write_text("data"))

    await archive.archive_collection(coll, "signals", ttl, tmp_path, now=now)

    assert expected.exists()
    assert coll.deleted is True
    assert coll.delete_query == {"_id": {"$in": [1]}}
