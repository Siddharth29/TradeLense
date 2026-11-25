"""E2E tests for the multi-expert decision flow (unified expert + judge).

Rewritten 2026-06-11: the old file mocked the retired 3-expert flow and
asserted a metrics schema that no longer exists. Current contract:
``multi_expert_decision`` returns (decision, cycle_doc, dynamic_config);
cycle_doc is {} when store_response=False; the judge decision is authoritative.
"""

from unittest.mock import patch

import pytest

from packages.core.multi_expert import multi_expert_decision


def _candles(base=25000.0, n5=41, n15=5):
    c5 = [{"high": base * 1.04, "low": base * 0.96, "close": base, "open": base * 0.999}
          for _ in range(n5)]
    c15 = [{"high": base * 1.04, "low": base * 0.96, "close": base, "open": base * 0.998}
           for _ in range(n15)]
    return c5, c15


def _cfg(**over):
    cfg = {
        "asset": "NIFTY",
        "agent_id": "test_agent",
        "broker_type": "test_broker",
        "prompt_hash": "test_hash_e2e",
        "model": "gpt-4o-mini",
        "store_response": False,
    }
    cfg.update(over)
    return cfg


def _resp(signal="buy", entry=25000.0, stop=24900.0, target=25300.0, conf=0.8, ttl=90):
    return {
        "signal": signal,
        "entry": entry,
        "stop": stop,
        "target": target,
        "ttl_minutes": ttl,
        "confidence": conf,
        "rationale": "test rationale",
    }


class TestE2EMultiExpert:

    @pytest.mark.asyncio
    async def test_e2e_run_now_multi_expert_happy_path(self):
        c5, c15 = _candles()
        expert = _resp(entry=25000.0, conf=0.8)
        judge = _resp(entry=25050.0, stop=24950.0, target=25350.0, conf=0.75)

        with patch("packages.core.multi_expert.complete") as mock_complete:
            mock_complete.side_effect = [expert, judge]
            decision, metrics, _dyncfg = await multi_expert_decision(c5, c15, _cfg())

        # The judge decision is authoritative — not the expert proposal.
        assert decision.signal == "buy"
        assert decision.entry == 25050.0
        assert decision.stop == 24950.0
        assert decision.target == 25350.0
        assert decision.ttl_minutes == 90
        assert isinstance(metrics, dict)  # {} with store_response=False

    @pytest.mark.asyncio
    async def test_e2e_run_now_flat_decision(self):
        c5, c15 = _candles()
        expert = _resp(entry=25000.0, conf=0.5)
        judge = {
            "signal": "flat", "entry": None, "stop": None, "target": None,
            "ttl_minutes": 90, "confidence": 0.0, "rationale": "no edge",
        }

        with patch("packages.core.multi_expert.complete") as mock_complete:
            mock_complete.side_effect = [expert, judge]
            decision, _metrics, _dyncfg = await multi_expert_decision(c5, c15, _cfg())

        assert decision.signal == "flat"
        assert decision.confidence == 0.0

    @pytest.mark.asyncio
    async def test_e2e_run_now_with_database_persistence(self):
        """store_response=False must not touch the DB and returns an empty cycle doc."""
        c5, c15 = _candles()
        resp = _resp(ttl=180, conf=0.85)
        # Deprecated V1 fields must still be tolerated (extra="ignore").
        resp.update({"active_window_c5": 75, "active_window_c15": 40,
                     "need_more_candles": False})

        with patch("packages.core.multi_expert.complete") as mock_complete,              patch("packages.db.repositories.gpt_responses.store_gpt_response") as mock_store:
            mock_complete.side_effect = [resp, dict(resp)]
            decision, metrics, _dyncfg = await multi_expert_decision(c5, c15, _cfg())

        assert decision.signal == "buy"
        assert decision.ttl_minutes == 180
        assert metrics == {}
        mock_store.assert_not_called()
