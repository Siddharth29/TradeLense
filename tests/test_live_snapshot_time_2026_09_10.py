"""Live prompt/risk context must use the snapshot's close-time session."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from apps.backend.agents.runner import AgentRunner
from packages.core.multi_expert import _cycle_htf_context, _compute_stop_hint


AS_OF = datetime(2026, 7, 14, 8, tzinfo=timezone.utc)
M5 = [{"time": AS_OF - timedelta(minutes=5), "close": 100.0}]


def test_prompt_regime_and_live_stop_floor_use_identical_snapshot_time(monkeypatch):
    captured = []

    def htf(candles, asset, as_of=None):
        captured.append(("htf", as_of))
        return {"htf_trend": "up"}

    def floor(atr, price, asset, as_of=None):
        captured.append(("floor", as_of))
        return 1.0

    monkeypatch.setattr("packages.core.market_structure.atr_from_candles", lambda *a: 1.0)
    monkeypatch.setattr("packages.core.market_structure.compute_htf_context", htf)
    monkeypatch.setattr("packages.core.market_structure.recommended_stop_distance", floor)
    monkeypatch.setattr("packages.core.multi_expert.compute_htf_context", htf)
    monkeypatch.setattr("packages.core.multi_expert.atr_from_candles", lambda *a: 1.0)
    monkeypatch.setattr("packages.core.multi_expert.recommended_stop_distance", floor)
    monkeypatch.setattr("packages.core.config.ATR_STOP_ENABLED", True)
    live = AgentRunner.__new__(AgentRunner)
    live._compute_cycle_risk_meta({}, M5, "EURUSD")
    cfg = {"asset": "EURUSD", "as_of": AS_OF}
    _cycle_htf_context(cfg)
    _compute_stop_hint(M5, cfg)
    assert len(captured) == 4
    assert all(stamp == AS_OF for _, stamp in captured)


@pytest.mark.asyncio
async def test_slow_htf_fetch_cannot_advance_the_already_frozen_m5_snapshot(monkeypatch):
    live = AgentRunner.__new__(AgentRunner)
    candles = [SimpleNamespace(time=AS_OF - timedelta(hours=h), open=100., high=101.,
                               low=99., close=100., volume=10.) for h in (2, 1, 0)]
    live.broker = SimpleNamespace(htf_candles=AsyncMock(return_value=candles))
    monkeypatch.setattr("packages.core.config.HTF_CONTEXT_ENABLED", True)
    monkeypatch.setattr("packages.core.config.HTF_CANDLE_COUNTS", {"1h": 3})
    monkeypatch.setattr("packages.core.config.HTF_CANDLE_COUNTS_BY_ASSET", {})
    result = await live._fetch_htf_candles("EURUSD", as_of=AS_OF)
    assert len(result["1h"]) == 2
    assert result["1h"][-1]["time"] == (AS_OF - timedelta(hours=1)).isoformat()
