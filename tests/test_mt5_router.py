from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from apps.backend import api
from apps.backend.routers import mt5
from packages.core.config import MONGO_DB_NAME


class DummyAdapter:
    def __init__(self, recorder):
        self.recorder = recorder

    def login(self, server: str, login: str, password: str) -> None:
        self.recorder["args"] = (server, login, password)


class DummyCollection:
    def __init__(self, sink):
        self.sink = sink

    async def insert_one(self, doc):
        self.sink["doc"] = doc

    async def update_many(self, flt, update):
        self.sink["filter"] = flt
        self.sink["update"] = update


class DummyDB(dict):
    def __init__(self, coll):
        super().__init__()
        self.coll = coll

    def __getitem__(self, name):
        assert name == "broker_sessions"
        return self.coll


class DummyClient(dict):
    def __init__(self, db):
        super().__init__()
        self.db = db

    def __getitem__(self, name):
        assert name == MONGO_DB_NAME
        return self.db


@pytest.mark.asyncio
async def test_mt5_connect_logs_in_and_persists(monkeypatch):
    recorded = {}
    stored = {}

    monkeypatch.setattr(mt5, "adapter", DummyAdapter(recorded))
    coll = DummyCollection(stored)
    db = DummyDB(coll)
    client = DummyClient(db)
    monkeypatch.setattr(mt5, "get_client", lambda: client)
    # Don't touch the on-disk cred cache during tests.
    monkeypatch.setattr(mt5, "save_mt5_credentials", lambda **kw: None)

    async def fake_get_current(domain, env):
        return {"mt5": {"server": "srv", "loginId": "1", "password": "pw"}}, None

    monkeypatch.setattr(mt5.config_repo, "get_current", fake_get_current)

    with TestClient(api.app) as client_app:
        resp = client_app.get("/brokers/mt5/connect")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    assert recorded["args"] == ("srv", "1", "pw")
    doc = stored["doc"]
    assert doc["broker"] == "mt5"
    assert doc["server"] == "srv"
    assert doc["login"] == "1"


@pytest.mark.asyncio
async def test_mt5_connect_saves_config_when_missing(monkeypatch):
    recorded = {}
    stored = {}

    monkeypatch.setattr(mt5, "adapter", DummyAdapter(recorded))
    coll = DummyCollection(stored)
    db = DummyDB(coll)
    client = DummyClient(db)
    monkeypatch.setattr(mt5, "get_client", lambda: client)
    # Don't touch the on-disk cred cache during tests.
    monkeypatch.setattr(mt5, "save_mt5_credentials", lambda **kw: None)

    async def fake_get_current(domain, env):
        return {}, None

    captured = {}

    async def fake_put(domain, env, cfg, *, if_match_checksum=None):
        captured["cfg"] = cfg
        captured["checksum"] = if_match_checksum

    monkeypatch.setattr(mt5.config_repo, "get_current", fake_get_current)
    monkeypatch.setattr(mt5.config_repo, "put", fake_put)

    with TestClient(api.app) as client_app:
        resp = client_app.post(
            "/brokers/mt5/connect",
            json={"server": "srv", "loginId": "1", "password": "pw"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    assert recorded["args"] == ("srv", "1", "pw")
    assert captured["cfg"]["mt5"]["server"] == "srv"


@pytest.mark.asyncio
async def test_mt5_status_true_when_connected(monkeypatch):
    # /status now reflects REAL terminal health (adapter.is_connected), not the
    # mere existence of a session token.
    class ConnectedAdapter:
        def is_connected(self):
            return True

    monkeypatch.setattr(mt5, "adapter", ConnectedAdapter())
    with TestClient(api.app) as client_app:
        resp = client_app.get("/brokers/mt5/status")
        assert resp.status_code == 200
        assert resp.json() == {"connected": True}


@pytest.mark.asyncio
async def test_mt5_status_false_when_disconnected(monkeypatch):
    class DisconnectedAdapter:
        def is_connected(self):
            return False

    monkeypatch.setattr(mt5, "adapter", DisconnectedAdapter())
    with TestClient(api.app) as client_app:
        resp = client_app.get("/brokers/mt5/status")
        assert resp.status_code == 200
        assert resp.json() == {"connected": False}


@pytest.mark.asyncio
async def test_mt5_wallet_returns_info(monkeypatch):
    info = {
        "login": 1,
        "name": "acct",
        "server": "srv",
        "currency": "USD",
        "balance": 3.0,
        "equity": 4.0,
        "margin": 2.0,
        "free_margin": 1.0,
        "leverage": 100,
    }
    recorded = {}

    class DummyAdapter:
        def wallet_info(self):
            return info

    async def fake_insert(doc):
        recorded["doc"] = doc.model_dump()

    monkeypatch.setattr(mt5, "adapter", DummyAdapter())
    monkeypatch.setattr(mt5, "insert_wallet_snapshot", fake_insert)

    with TestClient(api.app) as client_app:
        api.app.state.broker_tokens = {
            "mt5": {"expires_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()}
        }
        resp = client_app.get("/brokers/mt5/wallet")
        assert resp.status_code == 200
        assert resp.json()["balance"] == 3.0

    assert recorded["doc"]["equity"]["used_margin"] == 2.0


@pytest.mark.asyncio
async def test_mt5_wallet_requires_session(monkeypatch):
    async def fake_get_session(request):
        return None

    monkeypatch.setattr(mt5, "_get_session", fake_get_session)
    with TestClient(api.app) as client_app:
        resp = client_app.get("/brokers/mt5/wallet")
        assert resp.status_code == 401
        assert resp.json() == {"detail": {"needs_reauth": True}}


@pytest.mark.asyncio
async def test_mt5_disconnect_clears_state_and_marks_inactive(monkeypatch):
    recorded = {}
    coll = DummyCollection(recorded)
    db = DummyDB(coll)
    client = DummyClient(db)
    monkeypatch.setattr(mt5, "get_client", lambda: client)

    with TestClient(api.app) as client_app:
        api.app.state.broker_tokens = {"mt5": {"server": "srv"}}
        resp = client_app.post("/brokers/mt5/disconnect")
        assert resp.status_code == 200
        assert resp.json() == {"disconnected": True}
        assert "mt5" not in getattr(api.app.state, "broker_tokens", {})

    assert recorded["filter"] == {"broker": "mt5", "active": True}
    assert recorded["update"] == {"$set": {"active": False}}


