"""Tests for POST /agents/{symbol}/start and _prepare_runtime_cfg.

Rewritten 2026-06-11: agents are now resolved via agents_repo.list_agents()
(per-agent records), not config_repo.get_agent_config; services are created
inside _prepare_runtime_cfg (patched here); override validation runs against
the pre-services data snapshot.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from apps.backend import api
from packages.db.repositories import configs as config_repo


def _agent_record(symbol="XAUUSD"):
    return SimpleNamespace(
        id="alpha",
        symbol=symbol,
        candle_count_5m=100,
        candle_count_15m=100,
        broker="mt5",
        risk_percent=1.0,
        run_interval=300,
        prompt_id=None,
    )


def _full_override():
    """A complete, valid AgentConfig-shaped override payload."""
    return {
        "agent_id": "alpha",
        "candles_5_count": 3,
        "broker": "mt5",
        "gpt": {"api_key": "k"},
        "risk": {},
        "instrument": {"symbol": "XAUUSD"},
        "admin": {"env": "dev", "created_at": datetime.now(timezone.utc).isoformat()},
    }


def _patch_common(monkeypatch, record, started):
    async def fake_list_agents():
        return [record]

    async def fake_services(symbol, cfg):
        return cfg  # no live broker/candle service in unit tests

    def fake_start(asset, c):
        started["asset"] = asset
        started["cfg"] = c

    monkeypatch.setattr(api.agents_repo, "list_agents", fake_list_agents)
    monkeypatch.setattr(api, "_create_services_for_agent", fake_services)
    monkeypatch.setattr(api, "supervisor", SimpleNamespace(start=fake_start))


def test_start_agent_uses_agent_record(monkeypatch):
    started = {}
    _patch_common(monkeypatch, _agent_record(), started)
    client = TestClient(api.app)

    resp = client.post("/agents/XAUUSD/start")
    assert resp.status_code == 200
    assert resp.json()["status"] == "started"
    assert started["asset"] == "XAUUSD"
    assert started["cfg"]["candle_count_5m"] == 100
    assert started["cfg"]["agent_id"] == "alpha"


def test_start_agent_unknown_symbol_404(monkeypatch):
    started = {}
    _patch_common(monkeypatch, _agent_record(symbol="BTCUSD"), started)
    client = TestClient(api.app)

    resp = client.post("/agents/XAUUSD/start")
    assert resp.status_code == 404
    assert started == {}


def test_start_agent_override_merges_and_persists(monkeypatch):
    started = {}
    _patch_common(monkeypatch, _agent_record(), started)

    persisted = {}

    async def fake_put(agent_id, cfg, env):
        persisted["agent_id"] = agent_id
        persisted["cfg"] = cfg

    monkeypatch.setattr(config_repo, "put_agent_config", fake_put)
    client = TestClient(api.app)

    resp = client.post("/agents/XAUUSD/start", json=_full_override())
    assert resp.status_code == 200, resp.text
    # Override propagated to the runtime cfg handed to the supervisor
    assert started["cfg"]["candle_count_5m"] == 3
    assert started["cfg"]["candles_5_count"] == 3
    # And the validated config was persisted
    assert persisted["agent_id"] == "XAUUSD"
    assert persisted["cfg"].candles_5_count == 3


def test_start_agent_null_override_value_400(monkeypatch):
    started = {}
    _patch_common(monkeypatch, _agent_record(), started)
    client = TestClient(api.app)

    resp = client.post("/agents/XAUUSD/start", json={"candles_5_count": None})
    assert resp.status_code == 400
