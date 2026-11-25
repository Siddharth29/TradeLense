"""Concurrency sanity tests for multi_expert_decision.

Verifies that concurrent agents get isolated decisions (no cross-talk) when
running through the real flow with mocked GPT responses.

NOTE: the GPT mock is applied ONCE around the whole gather and routes
responses by the ``asset`` kwarg. Per-task ``with patch(...)`` blocks are
concurrency-UNSAFE: the first task to finish restores the real function while
the other tasks are mid-flight (this exact bug made these tests flaky-fail).
"""

import asyncio
from unittest.mock import patch

import pytest

from packages.core.multi_expert import multi_expert_decision


def _agent_config(agent_id: str, asset: str) -> dict:
    return {
        "asset": asset,
        "agent_id": agent_id,
        "broker_type": "test_broker",
        "prompt_hash": f"hash_{agent_id}",
        "model": "gpt-4o-mini",
        "store_response": False,
    }


def _agent_response(agent_id: str, base_price: float) -> dict:
    """Flat signal dict — valid for both expert-proposal and judge parsing."""
    return {
        "signal": "buy",
        "entry": base_price,
        "stop": base_price * 0.996,
        "target": base_price * 1.012,  # 3:1 RR, distances above floors
        "ttl_minutes": 90,
        "confidence": 0.8,
        "rationale": f"Agent {agent_id} analysis at {base_price}",
    }


def _candles(base_price: float, n5: int = 41, n15: int = 5):
    c5 = [
        {"high": base_price * 1.04, "low": base_price * 0.96,
         "close": base_price, "open": base_price * 0.999}
        for _ in range(n5)
    ]
    c15 = [
        {"high": base_price * 1.04, "low": base_price * 0.96,
         "close": base_price, "open": base_price * 0.998}
        for _ in range(n15)
    ]
    return c5, c15


def _asset_routed_mock(responses_by_asset: dict):
    """side_effect that returns each agent's response based on the asset kwarg."""
    def route(*args, **kwargs):
        asset = kwargs.get("asset")
        assert asset in responses_by_asset, f"unexpected asset in GPT call: {asset}"
        return responses_by_asset[asset]
    return route


async def _run_agents(agents):
    responses = {a["asset"]: _agent_response(a["agent_id"], a["base_price"]) for a in agents}

    async def one(a):
        c5, c15 = _candles(a["base_price"])
        decision, metrics, _dyncfg = await multi_expert_decision(
            c5, c15, _agent_config(a["agent_id"], a["asset"])
        )
        return {"agent": a, "decision": decision, "metrics": metrics}

    with patch("packages.core.multi_expert.complete") as mock_complete:
        mock_complete.side_effect = _asset_routed_mock(responses)
        return await asyncio.gather(*[one(a) for a in agents])


class TestConcurrencySanity:

    @pytest.mark.asyncio
    async def test_multi_agent_concurrency_sanity(self):
        """3 concurrent agents complete and stay agent-local."""
        agents = [
            {"agent_id": "agent_001", "asset": "NIFTY", "base_price": 25000.0},
            {"agent_id": "agent_002", "asset": "BANKNIFTY", "base_price": 45000.0},
            {"agent_id": "agent_003", "asset": "SENSEX", "base_price": 75000.0},
        ]
        results = await _run_agents(agents)
        assert len(results) == 3
        for r in results:
            assert r["decision"] is not None, f"{r['agent']['asset']} produced no decision"
            assert r["decision"].signal == "buy"
            assert r["decision"].entry == r["agent"]["base_price"], (
                f"{r['agent']['asset']} got entry {r['decision'].entry} — cross-talk!"
            )

    @pytest.mark.asyncio
    async def test_orchestrator_reentrancy(self):
        """10 concurrent agents: unique, correctly-mapped entries (no interference)."""
        agents = [
            {"agent_id": f"stress_{i:03d}", "asset": f"ASSET_{i}",
             "base_price": 10000.0 + i * 1000.0}
            for i in range(10)
        ]
        results = await _run_agents(agents)
        entries = [r["decision"].entry for r in results]
        assert len(set(entries)) == len(agents), "entries must be unique per agent"
        for r in results:
            assert r["decision"].entry == r["agent"]["base_price"]

    @pytest.mark.asyncio
    async def test_concurrent_different_timeframes(self):
        """Agents on different price scales process their own candle data."""
        agents = [
            {"agent_id": "tf_1", "asset": "TF_ASSET_1", "base_price": 25000.0},
            {"agent_id": "tf_2", "asset": "TF_ASSET_2", "base_price": 45000.0},
            {"agent_id": "tf_3", "asset": "TF_ASSET_3", "base_price": 75000.0},
        ]
        results = await _run_agents(agents)
        for r in results:
            assert r["decision"].entry == r["agent"]["base_price"], (
                f"{r['agent']['agent_id']} should use its own price data"
            )
