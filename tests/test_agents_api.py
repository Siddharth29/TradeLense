import asyncio
from fastapi.testclient import TestClient

from apps.backend import api
from apps.backend.agents import Supervisor
import apps.backend.agents.supervisor as sup_mod
from packages.db.repositories import agents as agents_repo


class _TestSupervisor(Supervisor):
    async def run_once(self, asset: str, cfg: dict | None = None):  # type: ignore[override]
        return f"once:{asset}"


def setup_module(module):
    pass


def test_agent_lifecycle(monkeypatch):
    event = asyncio.Event()

    async def slow_run(asset, n, cfg):
        await event.wait()

    store: dict[str, dict[str, object]] = {}

    async def _list():
        return [agents_repo.AgentRecord(**doc) for doc in store.values()]

    async def _upsert(agent: agents_repo.AgentRecord):
        store[agent.id] = {
            "id": agent.id,
            "assistant_id": agent.assistant_id,
            "symbol": agent.symbol,
            "candle_count_5m": agent.candle_count_5m,
            "candle_count_15m": agent.candle_count_15m,
            "broker": agent.broker,
            "expiry_date": agent.expiry_date,
            "risk_percent": agent.risk_percent,
            "run_interval": agent.run_interval,
        }

    async def _delete(agent_id: str):
        store.pop(agent_id, None)

    monkeypatch.setattr(agents_repo, "list_agents", _list)
    monkeypatch.setattr(agents_repo, "upsert_agent", _upsert)
    monkeypatch.setattr(agents_repo, "delete_agent", _delete)

    sup = _TestSupervisor(slow_run)
    monkeypatch.setattr(api, "supervisor", sup)
    async def _wait(*a, **k):
        await event.wait()
        return datetime.now(timezone.utc)
    monkeypatch.setattr(sup_mod.scheduler, "await_next_bar_close", _wait)
    monkeypatch.setattr(sup_mod.session, "is_session_open", lambda dt, asset="NSE": True)

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
    from datetime import datetime, timezone

    cfg = AgentConfig(
        agent_id="alpha",
        candles_5_count=100,
        candles_15_count=100,
        broker=BrokerType.MT5,
        gpt=GptConfig(api_key="k"),
        risk=RiskConfig(),
        instrument=InstrumentConfig(symbol="XAUUSD"),
        admin=AdminConfig(env=Env.DEV, created_at=datetime.now(timezone.utc)),
    )

    async def fake_get(agent_id: str, env: str):
        assert agent_id == "AAA"
        return cfg, "chk"

    async def fake_put(agent_id: str, c, env: str, *, if_match_checksum=None):
        pass

    monkeypatch.setattr(config_repo, "get_agent_config", fake_get)
    monkeypatch.setattr(config_repo, "put_agent_config", fake_put)

    with TestClient(api.app) as client:
        payload = {
            "id": "AAA",
            "assistant_id": "assist-1",
            "symbol": "AAA",
            "candle_count_5m": 100,
            "candle_count_15m": 100,
            "broker": "mt5",
            "expiry_date": "2024-01-10",
            "risk_percent": 1,
            "run_interval": "hourly",
        }
        resp = client.put("/agents/AAA", json=payload)
        assert resp.status_code == 200
        assert resp.json() == payload

        resp = client.get("/agents")
        assert resp.status_code == 200
        assert resp.json() == {
            "agents": [
                {
                    "id": "AAA",
                    "assistant_id": "assist-1",
                    "symbol": "AAA",
                    "candle_count_5m": 100,
                    "candle_count_15m": 100,
                    "broker": "mt5",
                    "expiry_date": "2024-01-10",
                    "risk_percent": 1,
                    "run_interval": "hourly",
                }
            ]
        }

        resp = client.post("/agents/AAA/start", json={"N": 1})
        assert resp.status_code == 200
        assert resp.json() == {"status": "started"}

        resp = client.get("/agents/AAA/status")
        assert resp.status_code == 200
        status_data = resp.json()
        assert status_data["running"] is True
        assert status_data["run_interval"] == "1h"
        assert status_data["risk_percent"] == 1

        resp = client.post("/agents/AAA/run-now")
        assert resp.status_code == 200
        data = resp.json()
        assert data["result"] == "once:AAA"
        assert data["config"]["agent_id"] == "alpha"
        assert data["config"]["run_interval"] == "hourly"
        assert data["config"]["risk_percent"] == 1

        resp = client.post("/agents/AAA/stop")
        assert resp.status_code == 200
        assert resp.json() == {"status": "stopped"}

        resp = client.get("/agents/AAA/status")
        assert resp.status_code == 200
        stopped_data = resp.json()
        assert stopped_data["running"] is False
        assert stopped_data["run_interval"] == "1h"
        assert stopped_data["risk_percent"] == 1

        resp = client.delete("/agents/AAA")
        assert resp.status_code == 204

        resp = client.get("/agents")
        assert resp.status_code == 200
        assert resp.json() == {"agents": []}


def test_run_now_starts_agent_when_missing(monkeypatch):
    from datetime import datetime, timezone
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

    cfg = AgentConfig(
        agent_id="alpha",
        candles_5_count=100,
        candles_15_count=100,
        broker=BrokerType.MT5,
        gpt=GptConfig(api_key="k"),
        risk=RiskConfig(),
        instrument=InstrumentConfig(symbol="XAUUSD"),
        admin=AdminConfig(env=Env.DEV, created_at=datetime.now(timezone.utc)),
    )

    async def fake_get(agent_id: str, env: str):
        assert agent_id == "AAA"
        return cfg, "chk"

    class FakeSupervisor:
        def __init__(self):
            self._cfg: dict[str, dict] = {}
            self._tasks: dict[str, object] = {}

        def start(self, asset: str, c: dict):
            self._cfg[asset] = c
            self._tasks[asset] = object()

        async def run_once(self, asset: str, cfg: dict | None = None):
            return f"once:{asset}"

    fake_sup = FakeSupervisor()
    monkeypatch.setattr(api, "supervisor", fake_sup)
    monkeypatch.setattr(config_repo, "get_agent_config", fake_get)

    client = TestClient(api.app)
    resp = client.post("/agents/AAA/run-now")
    assert resp.status_code == 200
    data = resp.json()
    assert data["result"] == "once:AAA"
    assert data["config"]["agent_id"] == "alpha"
    assert data["config"]["candles_5_count"] == 100
    assert data["config"]["run_interval"] == "hourly"
    assert data["config"]["risk_percent"] == 1
    assert "AAA" in fake_sup._cfg
    assert "AAA" in fake_sup._tasks
    assert fake_sup._cfg["AAA"]["run_interval"] == "hourly"
    assert fake_sup._cfg["AAA"]["risk_percent"] == 1


def test_run_now_background_acks_started_immediately(monkeypatch):
    # Patch the cfg-prep seam directly: this test covers the background-ack
    # contract of the endpoint, not agent lookup / service creation.
    async def fake_prepare(symbol: str, overrides):
        assert symbol == "AAA"
        return {
            "agent_id": "alpha",
            "symbol": "XAUUSD",
            "run_interval": "hourly",
            "risk_percent": 1,
            "N": 1,
        }

    class FakeSupervisor:
        def __init__(self):
            self._cfg: dict[str, dict] = {}
            self._tasks: dict[str, object] = {}
            self.run_once_calls: list[str] = []

        def start(self, asset: str, c: dict):
            self._cfg[asset] = c
            self._tasks[asset] = object()

        async def run_once(self, asset: str, cfg: dict | None = None):
            self.run_once_calls.append(asset)
            return f"once:{asset}"

        def status(self, asset: str):
            return {"running": asset in self._tasks}

    fake_sup = FakeSupervisor()
    monkeypatch.setattr(api, "supervisor", fake_sup)
    monkeypatch.setattr(api, "_prepare_runtime_cfg", fake_prepare)

    client = TestClient(api.app)
    resp = client.post("/agents/AAA/run-now?background=true")
    assert resp.status_code == 200
    # The ack must NOT wait for the cycle result: no "result"/"config" payload,
    # just a deterministic started acknowledgement for sequential launchers.
    assert resp.json() == {"status": "started", "symbol": "AAA", "background": True}
    assert "AAA" in fake_sup._cfg
    assert "AAA" in fake_sup._tasks

    # The agent is immediately visible as running via the status endpoint the
    # launcher polls before starting the next agent.
    resp = client.get("/agents/AAA/status")
    assert resp.status_code == 200
    assert resp.json()["running"] is True
