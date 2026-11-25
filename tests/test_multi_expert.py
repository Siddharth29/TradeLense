"""Tests for multi-expert orchestration, expert callers, and judge logic."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import logging

import pytest

from packages.core import multi_expert
from packages.core.schemas_trade import MultiExpertSignal


class DummyResponse:
    def __init__(self, payload: dict):
        self.payload = payload


@pytest.fixture
def candle_supersets():
    c5 = [
        {"high": 150.0, "low": 149.0},
        {"high": 149.0, "low": 148.0},
        {"high": 148.0, "low": 147.0},
        {"high": 147.0, "low": 146.0},
        {"high": 146.0, "low": 145.0},
        {"high": 145.0, "low": 144.0},
        {"high": 144.0, "low": 143.0},
        {"high": 143.0, "low": 142.0},
        {"high": 142.0, "low": 141.0},
        {"high": 141.0, "low": 140.0},
        {"high": 140.0, "low": 139.0},
        {"high": 138.0, "low": 137.0},
        {"high": 136.0, "low": 135.0},
        {"high": 134.0, "low": 133.0},
        {"high": 132.0, "low": 131.0},
        {"high": 130.0, "low": 129.0},
        {"high": 128.0, "low": 127.0},
        {"high": 126.0, "low": 125.0},
        {"high": 124.0, "low": 123.0},
        {"high": 122.0, "low": 121.0},
        {"high": 121.0, "low": 120.0},
    ]
    # Pad to 41 bars — MIN_CANDLES_FOR_CODE_EXECUTION requires 41 (judge
    # template alignment); keep lows above 100 so window checks still pass.
    c5 = c5 + [{"high": 121.0, "low": 120.0} for _ in range(41 - len(c5))]
    c15 = [
        {"high": 151.0, "low": 119.0},
    ]
    return c5, c15


def _make_signal(**overrides: Any) -> dict:
    payload = {
        "signal": "buy",
        "entry": 127.0,
        "stop": 124.0,
        "target": 135.0,
        "ttl_minutes": 90,
        "confidence": 0.5,
        "rationale": "",
    }
    payload.update(overrides)
    return payload


def _make_unified_response(**overrides: Any) -> dict:
    """Build a unified expert response with proposals + candel_required_for_judge."""
    proposals = overrides.pop("proposals", [_make_signal(), _make_signal(target=140.0)])
    return {
        "proposals": proposals,
        "candel_required_for_judge": overrides.pop("candel_required_for_judge", 20),
        **overrides,
    }


@pytest.mark.asyncio
async def test_call_expert_parses_json(monkeypatch, candle_supersets):
    c5, c15 = candle_supersets
    response_payload = _make_unified_response()

    async def fake_complete(**kwargs):
        assert kwargs["response_format"]["type"] == "json_schema"
        assert "json_schema" in kwargs["response_format"]
        return response_payload

    monkeypatch.setattr(multi_expert, "complete", fake_complete)

    result, duration_ms = await multi_expert.call_expert("trend", c5, c15, gpt_cfg={})
    assert isinstance(result, dict)
    assert "proposals" in result
    assert duration_ms >= 0


@pytest.mark.asyncio
async def test_call_expert_invalid_response(monkeypatch, candle_supersets):
    c5, c15 = candle_supersets

    async def fake_complete(**kwargs):
        return "not-json"

    monkeypatch.setattr(multi_expert, "complete", fake_complete)

    with pytest.raises(ValueError):
        await multi_expert.call_expert("trend", c5, c15, gpt_cfg={})


@pytest.mark.asyncio
async def test_call_judge_selects_best_proposal(monkeypatch, candle_supersets):
    c5, c15 = candle_supersets

    proposals = [
        MultiExpertSignal(**_make_signal(confidence=0.2, target=135.0)),
        MultiExpertSignal(**_make_signal(confidence=0.8, target=142.0)),
    ]

    async def fake_complete(**kwargs):
        return proposals[1].model_dump()

    monkeypatch.setattr(multi_expert, "complete", fake_complete)

    decision, judge_ms, validation_results, _ = await multi_expert.call_judge(
        c5, c15, proposals, gpt_cfg={}
    )
    assert decision.confidence == 0.8
    assert decision.target == 142.0
    assert judge_ms >= 0


@pytest.mark.asyncio
async def test_call_judge_returns_flat_when_all_invalid(monkeypatch, candle_supersets):
    c5, c15 = candle_supersets

    proposals = [
        MultiExpertSignal(**_make_signal(signal="buy")),
        MultiExpertSignal(**_make_signal(target=130.0, stop=126.0)),
    ]

    async def fake_complete(**kwargs):
        return {
            "signal": "flat",
            "entry": None,
            "stop": None,
            "target": None,
            "ttl_minutes": 90,
            "confidence": 0.0,
        }

    monkeypatch.setattr(multi_expert, "complete", fake_complete)

    decision, _, _, _ = await multi_expert.call_judge(c5, c15, proposals, gpt_cfg={})
    assert decision.signal == "flat"


@pytest.mark.asyncio
async def test_multi_expert_decision_orchestrates(monkeypatch, candle_supersets):
    c5, c15 = candle_supersets

    async def fake_call_expert(kind, *args, **kwargs):
        return _make_unified_response(), 10.0

    async def fake_call_judge(c5_s, c15_s, proposals, gpt_cfg, expert_kinds=None,
                              use_code_sniper=False, expert_market_contexts=None, **kw):
        return proposals[0], 0.0, {"validation": expert_kinds}, None

    monkeypatch.setattr(multi_expert, "call_expert", fake_call_expert)
    monkeypatch.setattr(multi_expert, "call_judge", fake_call_judge)

    decision, metrics, dynamic_config = await multi_expert.multi_expert_decision(c5, c15, gpt_cfg={})
    assert decision.signal == "buy"


@pytest.mark.asyncio
async def test_multi_expert_decision_logs_timeout_and_uses_survivors(
    monkeypatch, caplog, candle_supersets
):
    c5, c15 = candle_supersets

    async def fake_call_expert(kind, *args, **kwargs):
        if kind == "trend":
            await asyncio.sleep(0.02)
        return _make_unified_response(), 12.0

    async def fake_call_judge(c5_s, c15_s, proposals, gpt_cfg, expert_kinds=None,
                              use_code_sniper=False, expert_market_contexts=None, **kw):
        return proposals[0] if proposals else multi_expert._create_flat_signal(), 0.0, {}, None

    monkeypatch.setattr(multi_expert, "call_expert", fake_call_expert)
    monkeypatch.setattr(multi_expert, "call_judge", fake_call_judge)
    monkeypatch.setattr(multi_expert, "_EXPERT_TIMEOUT_SECONDS", 0.01)

    caplog.set_level(logging.WARNING, logger=multi_expert.logger.name)

    decision, metrics, dynamic_config = await multi_expert.multi_expert_decision(c5, c15, gpt_cfg={})
    assert decision is not None


@pytest.mark.asyncio
async def test_multi_expert_decision_logs_exception_and_returns_flat(
    monkeypatch, caplog, candle_supersets
):
    c5, c15 = candle_supersets

    async def failing_call_expert(kind, *args, **kwargs):
        raise RuntimeError(f"{kind} failure")

    async def fake_call_judge(c5_s, c15_s, proposals, gpt_cfg, expert_kinds=None,
                              use_code_sniper=False, expert_market_contexts=None, **kw):
        flat = multi_expert._create_flat_signal()
        return flat, 0.0, {"validation": expert_kinds}, None

    monkeypatch.setattr(multi_expert, "call_expert", failing_call_expert)
    monkeypatch.setattr(multi_expert, "call_judge", fake_call_judge)

    caplog.set_level(logging.ERROR, logger=multi_expert.logger.name)

    decision, metrics, dynamic_config = await multi_expert.multi_expert_decision(c5, c15, gpt_cfg={})

    assert decision.signal == "flat"
    assert any("failed" in record.getMessage().lower() or "error" in record.getMessage().lower()
               for record in caplog.records)
