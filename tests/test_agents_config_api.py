from datetime import datetime, timezone

from fastapi.testclient import TestClient

from apps.backend import api
from packages.db.config_schemas import (
    AgentConfig,
    BrokerType,
    GptConfig,
    RiskConfig,
    InstrumentConfig,
    AdminConfig,
    Env,
)
from packages.db.repositories import configs as config_repo


def _build_cfg() -> AgentConfig:
    return AgentConfig(
        agent_id="alpha",
        candles_5_count=100,
        candles_15_count=100,
        broker=BrokerType.MT5,
        gpt=GptConfig(api_key="k"),
        risk=RiskConfig(),
        instrument=InstrumentConfig(symbol="XAUUSD"),
        admin=AdminConfig(env=Env.DEV, created_at=datetime.now(timezone.utc)),
    )


def test_put_and_get_agents_config(monkeypatch):
    stored: AgentConfig | None = None

    async def fake_put(cfg: AgentConfig, env: str, *, if_match_checksum: str | None = None):
        nonlocal stored
        stored = cfg

    async def fake_get(env: str):
        return stored, "chk"

    monkeypatch.setattr(config_repo, "put_agents_config", fake_put)
    monkeypatch.setattr(config_repo, "get_agents_config", fake_get)

    client = TestClient(api.app)

    payload = _build_cfg().model_dump(mode="json")

    resp = client.put("/configs/agents", json=payload, params={"env": "dev"})
    assert resp.status_code == 200
    assert stored is not None
    assert stored.agent_id == "alpha"

    resp = client.get("/configs/agents", params={"env": "dev"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["agent_id"] == "alpha"
    assert data["candles_5_count"] == 100
    assert data["candles_15_count"] == 100


def test_put_agents_config_invalid_payload(monkeypatch):
    called = False

    async def fake_put(cfg: AgentConfig, env: str, *, if_match_checksum: str | None = None):
        nonlocal called
        called = True

    monkeypatch.setattr(config_repo, "put_agents_config", fake_put)

    client = TestClient(api.app)

    resp = client.put(
        "/configs/agents",
        json={"candles_5_count": 100, "candles_15_count": 100},
        params={"env": "dev"},
    )
    assert resp.status_code == 400
    assert not called
    data = resp.json()
    assert any(err["loc"][-1] == "agent_id" for err in data["detail"])


def test_put_agents_config_allows_environment_key(monkeypatch):
    stored: AgentConfig | None = None

    async def fake_put(cfg: AgentConfig, env: str, *, if_match_checksum: str | None = None):
        nonlocal stored
        stored = cfg

    monkeypatch.setattr(config_repo, "put_agents_config", fake_put)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-env-key")

    client = TestClient(api.app)

    payload = _build_cfg().model_dump(mode="json")
    payload["gpt"].pop("api_key", None)

    resp = client.put("/configs/agents", json=payload, params={"env": "dev"})
    assert resp.status_code == 200
    assert stored is not None
    assert stored.agent_id == "alpha"
    assert stored.model_dump(mode="json")["gpt"]["api_key"] is None
