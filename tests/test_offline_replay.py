from unittest.mock import Mock

from fastapi.testclient import TestClient

from apps.backend.api import app
from apps.backend.routers import backtest, mt5
from packages.db import client as db


def test_offline_app_browses_without_starting_backtests_or_broker_actions(monkeypatch):
    monkeypatch.setenv("TRADELENS_OFFLINE", "true")
    monkeypatch.setattr(db, "_offline_client", None)
    unexpected = Mock(side_effect=AssertionError("Offline request started a connected action"))
    monkeypatch.setattr(backtest, "BacktestRunner", unexpected)
    monkeypatch.setattr(mt5.adapter, "login", unexpected)

    with TestClient(app) as client:
        assert client.get("/api/backtest/list").status_code == 200
        assert client.get("/metrics").status_code == 200
        assert client.get("/configs/model-config").status_code == 200
        assert client.get("/brokers/mt5/status").json() == {"connected": False, "offline": True}

        response = client.post("/api/backtest/run", json={"test_name": "Offline run"})
        assert response.status_code == 409
        assert "requires connected mode" in response.json()["detail"]
        for route in (
            "/api/backtest/example/restart",
            "/brokers/mt5/connect",
            "/agents/BTCUSD/start",
            "/api/backtest/fetch-historical-data",
        ):
            assert client.post(route).status_code == 409

    unexpected.assert_not_called()
