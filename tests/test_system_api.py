import os
os.environ.setdefault("SESSION_SECRET", "testsecret")

from fastapi.testclient import TestClient

from apps.backend import api
import asyncio


def test_flags_toggle():
    with TestClient(api.app) as client:
        resp = client.get("/flags")
        assert resp.status_code == 200
        assert resp.json() == {}
        resp = client.post("/flags/test", json={"value": True})
        assert resp.status_code == 200
        assert resp.json()["test"] is True
        resp = client.get("/flags")
        assert resp.json()["test"] is True


def test_health_readiness_metrics():
    with TestClient(api.app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "time" in data and "status" in data

        resp = client.get("/readiness")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data["ready"], bool)
        assert isinstance(data["blockers"], dict)

        resp = client.get("/metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert "equity_curve" in data
        assert "daily_pnl" in data
        assert "win_rate" in data


def test_kill(monkeypatch):
    called = {}

    def fake_exit(code):
        called["code"] = code

    monkeypatch.setattr(os, "_exit", fake_exit)

    class DummyLoop:
        def call_later(self, _delay, func):
            func()

    monkeypatch.setattr(asyncio, "get_event_loop", lambda: DummyLoop())

    with TestClient(api.app) as client:
        resp = client.post("/kill")
        assert resp.status_code == 200
        assert resp.json()["status"] == "shutting down"

    assert called.get("code") == 0
