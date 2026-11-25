from __future__ import annotations

import asyncio
import json
import pytest

from apps.backend.routers import stream
from packages.core.config import MONGO_DB_NAME


@pytest.mark.asyncio
async def test_trades_stream_events_and_heartbeat(monkeypatch):
    class DummyStream:
        def __init__(self):
            self.calls = 0

        async def next(self):
            self.calls += 1
            if self.calls == 1:
                return {"foo": "bar"}
            if self.calls == 2:
                raise asyncio.TimeoutError
            await asyncio.sleep(3600)

        async def __aenter__(self):  # pragma: no cover - simple stub
            return self

        async def __aexit__(self, exc_type, exc, tb):  # pragma: no cover - simple stub
            pass

    class DummyColl:
        def watch(self):  # pragma: no cover - simple stub
            return DummyStream()

    class DummyDB(dict):
        def __getitem__(self, name):  # pragma: no cover - simple stub
            assert name == "trades"
            return DummyColl()

    class DummyClient(dict):
        def __getitem__(self, name):  # pragma: no cover - simple stub
            assert name == MONGO_DB_NAME
            return DummyDB()

    class DummyESR:
        def __init__(self, iterator, headers=None):
            self.iterator = iterator

    monkeypatch.setattr(stream, "get_client", lambda: DummyClient())
    monkeypatch.setattr(stream, "EventSourceResponse", DummyESR)

    resp = await stream.stream_trades()
    gen = resp.iterator
    first = await anext(gen)
    second = await anext(gen)
    await gen.aclose()

    first_data = json.loads(first["data"])
    second_data = json.loads(second["data"])

    assert first_data == {"foo": "bar"}
    assert second_data["type"] == "heartbeat"
    assert "time" in second_data
