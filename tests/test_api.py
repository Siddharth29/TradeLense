import os
os.environ.setdefault("SESSION_SECRET", "testsecret")

from fastapi.testclient import TestClient
from apps.backend import api


def test_create_report(monkeypatch):
    saved = {}

    def fake_save(test_name, trades):
        saved["test_name"] = test_name
        saved["trades"] = trades

    monkeypatch.setattr(api, "save_backtest_report", fake_save)

    with TestClient(api.app) as client:
        payload = {
            "test_name": "run1",
            "trades": [
                {
                    "entry_time": "2024-01-01T09:15:00",
                    "exit_time": "2024-01-01T10:15:00",
                    "entry_price": 100,
                    "exit_price": 110,
                    "stop_loss": 95,
                    "target": 110,
                    "result": "win",
                    "points": 10,
                    "risk_to_reward": 2,
                    "api_call_id": "abc",
                }
            ],
        }
        resp = client.post("/api/reports", json=payload)
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        assert saved["test_name"] == "run1"
        assert saved["trades"][0]["entry_price"] == 100


def test_list_and_get_reports(monkeypatch):
    sample_docs = [
        {
            "_id": "1",
            "test_name": "run1",
            "created_at": "2024-01-01T00:00:00",
            "metrics": {"total_trades": 1},
        }
    ]

    def fake_fetch_reports(test_name=None, include_trades=False):
        if test_name:
            return sample_docs if test_name == "run1" else []
        return sample_docs

    monkeypatch.setattr(api, "fetch_reports", fake_fetch_reports)

    with TestClient(api.app) as client:
        resp = client.get("/api/reports")
        assert resp.status_code == 200
        assert resp.json()[0]["test_name"] == "run1"

        resp2 = client.get("/api/reports/run1")
        assert resp2.status_code == 200
        assert resp2.json()["test_name"] == "run1"
