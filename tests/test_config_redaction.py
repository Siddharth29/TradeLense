from fastapi.testclient import TestClient

from apps.backend import api
from packages.db.repositories import configs as config_repo

SECRET_PLACEHOLDER = "********"


def test_broker_config_redacts_secret(monkeypatch):
    async def fake_get_current(domain, env):
        assert domain == "brokers"
        return {"mt5": {"server": "s", "loginId": "l", "password": "secret"}}, 1

    monkeypatch.setattr(config_repo, "get_current", fake_get_current)

    client = TestClient(api.app)
    resp = client.get("/configs/brokers", params={"env": "dev"})
    assert resp.status_code == 200
    assert resp.json()["mt5"]["password"] == SECRET_PLACEHOLDER
