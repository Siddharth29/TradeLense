from fastapi.testclient import TestClient
from apps.backend import api


def test_list_endpoint(monkeypatch):
    monkeypatch.setattr(
        api,
        "_FAKE_TRADES",
        [
            {"asset": "AAA", "time": "2024-01-01T00:00:00Z", "agent_id": "a1", "prompt_hash": "ph1"},
            {"asset": "AAA", "time": "2024-01-01T00:05:00Z", "agent_id": "a1", "prompt_hash": "ph2"},
            {"asset": "BBB", "time": "2024-01-01T00:10:00Z"},
        ],
    )
    with TestClient(api.app) as client:
        resp = client.get("/journal/list?limit=2")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["entries"]) == 2
        assert isinstance(data.get("nextCursor"), str)
        assert data["entries"][0]["agent_id"] == "a1"
        assert "prompt_hash" in data["entries"][0]


def test_candles_endpoint(monkeypatch):
    monkeypatch.setattr(
        api,
        "_FAKE_CANDLES",
        [
            {
                "asset": "AAA",
                "time": "2024-01-01T00:00:00Z",
                "open": 1,
                "high": 2,
                "low": 0.5,
                "close": 1.5,
            },
            {
                "asset": "AAA",
                "time": "2024-01-01T00:05:00Z",
                "open": 1.5,
                "high": 2.5,
                "low": 1.3,
                "close": 2.0,
            },
        ],
    )
    with TestClient(api.app) as client:
        resp = client.get("/journal/AAA/candles?limit=1")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["candles"]) == 1
        assert isinstance(data.get("nextCursor"), str)


def test_trades_endpoint(monkeypatch):
    monkeypatch.setattr(
        api,
        "_FAKE_TRADES",
        [
            {
                "asset": "AAA",
                "time": "2024-01-01T00:00:00Z",
                "type": "open",
                "agent_id": "a1",
                "prompt_hash": "ph1",
            },
            {
                "asset": "AAA",
                "time": "2024-01-01T00:05:00Z",
                "type": "closed",
                "agent_id": "a1",
                "prompt_hash": "ph2",
            },
        ],
    )
    with TestClient(api.app) as client:
        resp = client.get("/journal/AAA/trades")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert "timestamp" in data[0]
        assert data[0]["agent_id"] == "a1"
        assert "prompt_hash" in data[0]
        assert data[0]["type"] == "open"
