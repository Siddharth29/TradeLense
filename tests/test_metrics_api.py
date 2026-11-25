import os
os.environ.setdefault("SESSION_SECRET", "testsecret")

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pandas as pd
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient

from apps.backend import api
from apps.backend.agents import AgentRunner
from apps.backend.agents import runner as runner_module
from packages.core import metrics
from packages.core.candles import Candle


@pytest.mark.asyncio
async def test_metrics_endpoint(monkeypatch):
    metrics.reset()

    def aligned_candles(n5: int, n15: int):
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        c5 = [
            Candle(time=pd.Timestamp(base.replace(minute=25, second=0, microsecond=0)), open=0, high=0, low=0, close=0, volume=0)
            for _ in range(n5)
        ]
        c15 = [
            Candle(time=pd.Timestamp(base.replace(minute=15, second=0, microsecond=0)), open=0, high=0, low=0, close=0, volume=0)
            for _ in range(n15)
        ]
        return c5, c15

    candle_service = SimpleNamespace(fetch=AsyncMock(return_value=aligned_candles(2, 2)))
    prediction = {
        "direction": "long",
        "pattern": "retest",
        "level": 100,
        "stop": 95,
        "target": 110,
        "confidence": "high",
        "status": "waiting",
    }
    monkeypatch.setattr(
        runner_module,
        "call_sniper",
        AsyncMock(return_value={"action": "hold", "prediction": prediction}),
    )
    monkeypatch.setattr(runner_module, "entry_trigger_met", lambda pred, fut: True)

    class Broker:
        def place(self, decision):
            return SimpleNamespace(pnl=5.0)

    broker = Broker()

    cfg = {
        "candle_service": candle_service,
        "broker": broker,
        "symbol": "AAA",
        "timestamp": "t",
        "candle_count_5m": 2,
        "candle_count_15m": 2,
    }

    runner = AgentRunner(cfg)
    await runner.run()

    with TestClient(api.app) as client:
        resp = client.get("/metrics/agents")
        assert resp.status_code == 200
        data = resp.json()
        assert data["AAA"]["runs"] == 1
        assert data["AAA"]["decisions"] == 1
        assert data["AAA"]["orders"] == 1
        assert data["AAA"]["wins"] == 1
        # P&L is now sourced as realized (closed comprehensive_trades) +
        # unrealized (live broker positions), NOT the in-memory price-diff sum
        # (m.pnl). With no Mongo/positions in this test both are 0, and the
        # surfaced ``pnl`` always equals realized + unrealized. The in-memory
        # recording path itself is unchanged (still feeds daily guards).
        assert "realized_pnl" in data["AAA"]
        assert "unrealized_pnl" in data["AAA"]
        assert data["AAA"]["pnl"] == pytest.approx(
            data["AAA"]["realized_pnl"] + data["AAA"]["unrealized_pnl"]
        )
        assert data["AAA"]["latency_p50"] is not None
        assert data["AAA"]["latency_p95"] is not None
        assert data["AAA"]["drift_p50"] is None
        assert data["AAA"]["drift_p95"] is None
        assert data["AAA"]["hit_rate"] == 1.0
        assert data["AAA"].get("agent_id") is None
        assert data["AAA"]["run_interval"] is None
        assert data["AAA"]["risk_percent"] is None


@pytest.mark.asyncio
async def test_metrics_latency_branches():
    """Ensure latency quantiles handle empty and multi-value histories."""
    metrics.reset()

    # Asset with two latency measurements triggers quantile calculation
    metrics.record_latency("AAA", 10)
    metrics.record_latency("AAA", 10)

    # Asset with no latency measurements remains in registry
    metrics.record_run("BBB")

    with TestClient(api.app) as client:
        resp = client.get("/metrics/agents")
        assert resp.status_code == 200
        data = resp.json()

    assert data["AAA"]["latency_p50"] == 10
    assert data["AAA"]["latency_p95"] == 10
    assert data["AAA"]["drift_p50"] is None
    assert data["AAA"]["drift_p95"] is None
    assert data["AAA"]["hit_rate"] is None
    assert data["BBB"]["latency_p50"] is None
    assert data["BBB"]["latency_p95"] is None
    assert data["BBB"]["drift_p50"] is None
    assert data["BBB"]["drift_p95"] is None
    assert data["BBB"]["hit_rate"] == 0.0
    assert data["AAA"].get("agent_id") is None
    assert data["BBB"].get("agent_id") is None
    assert data["AAA"]["run_interval"] is None
    assert data["AAA"]["risk_percent"] is None
    assert data["BBB"]["run_interval"] is None
    assert data["BBB"]["risk_percent"] is None
