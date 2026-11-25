import pytest

from packages.db.repositories import configs
from packages.core.trade_lifecycle import OptimisticLockError
from packages.db.config_schemas import (
    AgentConfig,
    BrokerType,
    GptConfig,
    RiskConfig,
    InstrumentConfig,
    AdminConfig,
    Env,
)
from datetime import datetime, timezone
from typing import Any, Mapping
from fastapi.testclient import TestClient
from apps.backend import api


class _InsertResult:
    def __init__(self, inserted_id):
        self.inserted_id = inserted_id


class _UpdateResult:
    def __init__(self, *, upserted_id=None, matched_count=0):
        self.upserted_id = upserted_id
        self.matched_count = matched_count


class _Cursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, key, direction):
        reverse = direction < 0
        self._docs.sort(key=lambda d: d[key], reverse=reverse)
        return self

    async def to_list(self, length=None):  # pragma: no cover - simple passthrough
        return list(self._docs)


class _Collection:
    def __init__(self):
        self.docs = []

    def with_options(self, **kwargs):  # pragma: no cover - passthrough
        return self

    async def find_one(self, filt, projection=None):
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in filt.items()):
                if projection:
                    return {k: doc[k] for k in doc if projection.get(k, 1)}
                return dict(doc)
        return None

    async def insert_one(self, doc):
        self.docs.append(dict(doc))
        return _InsertResult(len(self.docs))

    async def update_one(self, filt, update, *, upsert=False):
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in filt.items()):
                doc.update(update["$set"])
                return _UpdateResult(matched_count=1)
        if upsert:
            new_doc = {**filt, **update["$set"]}
            self.docs.append(new_doc)
            return _UpdateResult(upserted_id=len(self.docs))
        return _UpdateResult()

    def find(self, filt, projection=None):
        docs = [
            {k: d[k] for k in d if (projection or {}).get(k, 1)}
            for d in self.docs
            if all(d.get(k) == v for k, v in filt.items())
        ]
        return _Cursor(docs)


@pytest.mark.asyncio
async def test_put_get_list_and_rollback(monkeypatch):
    cfg_coll = _Collection()
    hist_coll = _Collection()

    async def _fake_get_client():
        return cfg_coll, hist_coll

    monkeypatch.setattr(configs, "get_client", _fake_get_client)

    cfg_id, hist_id = await configs.put("svc", "dev", {"a": 1})
    assert cfg_id is not None
    doc, checksum = await configs.get_current("svc", "dev")
    assert doc == {"a": 1}
    assert checksum

    with pytest.raises(OptimisticLockError):
        await configs.put("svc", "dev", {"a": 2}, if_match_checksum="bad")

    await configs.put("svc", "dev", {"a": 2}, if_match_checksum=checksum)
    doc, checksum2 = await configs.get_current("svc", "dev")
    assert doc == {"a": 2}
    assert checksum2 != checksum

    versions = await configs.list_versions("svc", "dev")
    assert versions == [1, 2]

    await configs.rollback("svc", "dev", 1)
    doc, _ = await configs.get_current("svc", "dev")
    assert doc == {"a": 1}

    versions = await configs.list_versions("svc", "dev")
    assert versions == [1, 2, 3]


@pytest.mark.asyncio
async def test_agents_config_round_trip(monkeypatch):
    cfg_coll = _Collection()
    hist_coll = _Collection()

    async def _fake_get_client():
        return cfg_coll, hist_coll

    monkeypatch.setattr(configs, "get_client", _fake_get_client)

    cfg = AgentConfig(
        agent_id="alpha",
        candles_5_count=100,
        candles_15_count=100,
        broker=BrokerType.MT5,
        gpt=GptConfig(api_key="k"),
        risk=RiskConfig(
            daily_max_loss=0.1,
            max_trades_per_day=2,
            cooldown_minutes=5,
            per_asset={"XAUUSD": {"mt5_risk_per_trade": 0.05}},
        ),
        instrument=InstrumentConfig(symbol="XAUUSD"),
        admin=AdminConfig(env=Env.DEV, created_at=datetime.now(timezone.utc)),
    )

    await configs.put_agents_config(cfg, "dev")
    stored, _ = await configs.get_agents_config("dev")
    assert stored is not None
    assert stored.agent_id == "alpha"
    assert stored.risk.daily_max_loss == 0.1
    assert stored.risk.max_trades_per_day == 2
    assert stored.risk.cooldown_minutes == 5
    assert stored.risk.per_asset["XAUUSD"].mt5_risk_per_trade == 0.05
    assert len(hist_coll.docs) == 1


@pytest.mark.asyncio
async def test_agents_config_migration(monkeypatch):
    cfg_coll = _Collection()
    hist_coll = _Collection()

    async def _fake_get_client():
        return cfg_coll, hist_coll

    monkeypatch.setattr(configs, "get_client", _fake_get_client)

    # The single candle_count alias is still supported; nested config uses
    # the current credentials/provider and MT5 instrument contracts.
    cfg_coll.docs.append(
        {
            "domain": "agents",
            "env": "dev",
            "doc": {
                "agent_id": "alpha",
                "candle_count": 3,
                "broker": "mt5",
                "gpt": {"api_key": "k", "provider": "openai"},
                "risk": {
                    "daily_max_loss": 0.0,
                    "max_trades_per_day": 0,
                    "cooldown_minutes": 0,
                    "per_asset": {},
                },
                "instrument": {
                    "symbol": "XAUUSD",
                },
                "flags": {"enable_backtest": False, "enable_stream": False},
                "admin": {
                    "env": "dev",
                    "created_at": datetime.now(timezone.utc),
                },
            },
            "checksum": "chk",
        }
    )

    stored, _ = await configs.get_agents_config("dev")
    assert stored is not None
    assert stored.candles_5_count == 3
    assert stored.candles_15_count == 3


def test_broker_config_api_validation(monkeypatch):
    stored: Mapping[str, Any] | None = None

    async def fake_put(domain: str, env: str, cfg: Mapping[str, Any], *, if_match_checksum: str | None = None):
        nonlocal stored
        stored = cfg

    async def fake_get_current(domain: str, env: str):
        return stored, "chk"

    monkeypatch.setattr(configs, "put", fake_put)
    monkeypatch.setattr(configs, "get_current", fake_get_current)

    client = TestClient(api.app)

    payload = {"mt5": {
        "server": "fixture-server", "loginId": "fixture-login",
        "password": "fixture-only", "mode": "live",
    }}
    resp = client.put("/configs/brokers", json=payload, params={"env": "dev"})
    assert resp.status_code == 200
    assert stored == {"mt5": {**payload["mt5"], "walletUrl": None}}

    resp = client.get("/configs/brokers", params={"env": "dev"})
    assert resp.status_code == 200
    assert resp.json() == {"mt5": {
        **payload["mt5"], "password": "********", "walletUrl": None,
    }}

    resp = client.put(
        "/configs/brokers",
        json={"mt5": {**payload["mt5"], "mode": "invalid"}},
        params={"env": "dev"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"][0]["loc"] == ["mode"]
    assert stored == {"mt5": {**payload["mt5"], "walletUrl": None}}


def test_get_broker_config_handles_invalid(monkeypatch):
    async def fake_get_current(domain: str, env: str):
        return {"mt5": {"symbols": ["XAUUSD"]}}, "chk"

    monkeypatch.setattr(configs, "get_current", fake_get_current)

    client = TestClient(api.app)
    resp = client.get("/configs/brokers", params={"env": "dev"})
    assert resp.status_code == 200
    assert resp.json() == {}


def test_gpt_config_api_validation(monkeypatch):
    stored: Mapping[str, Any] | None = None

    async def fake_put(domain: str, env: str, cfg: Mapping[str, Any], *, if_match_checksum: str | None = None):
        nonlocal stored
        stored = cfg

    async def fake_get_current(domain: str, env: str):
        return stored, "chk"

    monkeypatch.setattr(configs, "put", fake_put)
    monkeypatch.setattr(configs, "get_current", fake_get_current)

    client = TestClient(api.app)

    payload = {"api_key": "k", "provider": "openai", "prompt_id": "intraday-v1"}
    resp = client.put("/configs/gpt", json=payload, params={"env": "dev"})
    assert resp.status_code == 200
    assert stored == {**payload, "gemini_api_key": None, "anthropic_api_key": None}

    resp = client.get("/configs/gpt", params={"env": "dev"})
    assert resp.status_code == 200
    assert resp.json() == {
        **payload,
        "api_key": "********",
        "gemini_api_key": None,
        "anthropic_api_key": None,
    }

    resp = client.put(
        "/configs/gpt",
        json={**payload, "provider": "invalid-provider"},
        params={"env": "dev"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"][0]["loc"] == ["provider"]
    assert stored == {**payload, "gemini_api_key": None, "anthropic_api_key": None}

