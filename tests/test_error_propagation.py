"""Verify decision failures cannot arm a strategy or submit an order."""

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from pydantic import ValidationError

from apps.backend.agents import runner
from packages.core.multi_expert import call_expert, call_judge, multi_expert_decision_with_details
from packages.core.schemas_trade import MultiExpertSignal


@pytest.fixture
def base_candles():
    as_of = datetime(2026, 7, 14, 9, tzinfo=timezone.utc)

    def bars(interval, count):
        return [
            {
                "time": (as_of - timedelta(minutes=interval * offset)).isoformat(),
                "open": 99.0, "high": 105.0, "low": 95.0, "close": 100.0, "volume": 10,
            }
            for offset in range(count, 0, -1)
        ]

    return bars(5, 41), bars(15, 21)


@pytest.fixture
def gpt_config():
    return {"asset": "EURUSD", "agent_id": "test_agent", "model": "gpt-4o-mini", "log_prompts": False}


@pytest.mark.asyncio
class TestDecisionFailures:
    async def test_call_expert_invalid_json_raises(self, base_candles, gpt_config):
        with patch("packages.core.multi_expert.complete", return_value="not a dict"):
            with pytest.raises(ValueError, match="did not return JSON, got: <class 'str'>"):
                await call_expert("trend", *base_candles, gpt_config)

    async def test_call_expert_schema_mismatch_raises(self, base_candles, gpt_config):
        response = {"signal": "buy", "entry": "not_a_number", "stop": 95.0,
                    "target": 115.0, "ttl_minutes": 90, "confidence": 0.8}
        with patch("packages.core.multi_expert.complete", return_value=response):
            with pytest.raises(ValueError, match="Expert trend response validation failed") as error:
                await call_expert("trend", *base_candles, gpt_config)
        assert isinstance(error.value.__context__, ValidationError)
        assert error.value.__context__.errors()[0]["loc"] == ("entry",)

    async def test_call_judge_invalid_strategy_raises(self, base_candles, gpt_config):
        proposal = MultiExpertSignal(signal="buy", entry=100, stop=95, target=115,
                                     ttl_minutes=90, confidence=0.8)
        with patch("packages.core.multi_expert.complete", return_value={"code": 123}) as complete:
            with pytest.raises(ValueError, match="Code sniper judge failed with 1 proposals"):
                await call_judge(*base_candles, [proposal], gpt_config,
                                 expert_kinds={id(proposal): "trend"}, use_code_sniper=True)
        complete.assert_awaited_once()

    async def test_expert_error_skips_cycle_and_judge(self, base_candles, gpt_config, caplog):
        with patch("packages.core.multi_expert.complete", side_effect=RuntimeError("provider unavailable")) as complete:
            signal, cycle = await multi_expert_decision_with_details(
                *base_candles, gpt_config, cycle_id="expert-outage", use_code_sniper=True
            )
        complete.assert_awaited_once()
        assert signal.signal == "flat"
        assert cycle.status == "skipped"
        assert cycle.judge.final_signal is None
        assert any(record.getMessage() == "Expert call failed"
                   and record.error == "provider unavailable" for record in caplog.records)

    async def test_invalid_expert_proposal_skips_cycle_and_judge(self, base_candles, gpt_config, caplog):
        response = {
            "proposals": [{"signal": "buy", "entry": "bad price", "stop": 95.0,
                           "target": 115.0, "ttl_minutes": 90, "confidence": 0.5}],
            "candel_required_for_judge": 41,
        }
        with patch("packages.core.multi_expert.complete", return_value=response) as complete:
            signal, cycle = await multi_expert_decision_with_details(
                *base_candles, gpt_config, cycle_id="invalid-proposal", use_code_sniper=True
            )
        complete.assert_awaited_once()
        assert signal.signal == "flat"
        assert cycle.status == "skipped"
        assert cycle.judge.validation_summary["total_proposals"] == 0
        assert any(record.levelno == logging.ERROR and "Proposal validation failed" in record.getMessage()
                   for record in caplog.records)

    async def test_runner_logs_decision_failure_without_arming_or_ordering(self, base_candles, monkeypatch):
        broker = SimpleNamespace(name="stub", is_connected=Mock(return_value=True),
                                 positions=Mock(return_value=[]), place=AsyncMock())
        candle_service = SimpleNamespace(broker=broker, has_cache=Mock(return_value=True),
                                         fetch=AsyncMock(return_value=base_candles))
        live = runner.AgentRunner({
            "symbol": "EURUSD", "agent_id": "test_agent", "use_multi_expert": True,
            "broker": broker, "candle_service": candle_service,
            "candle_count_5m": len(base_candles[0]), "candle_count_15m": len(base_candles[1]),
        })
        live.set_execution_mode("run_now")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setattr("packages.core.services.code_sniper_monitor._is_market_closed",
                            lambda _: (False, 0))
        monkeypatch.setattr("packages.core.services.code_sniper_monitor.get_code_sniper_monitor_async",
                            AsyncMock(return_value=None))
        monkeypatch.setattr(runner, "validate_live_candle_window", Mock(return_value=(True, "")))
        monkeypatch.setattr(runner.trading_filters, "cleanup_expired_trades", Mock())
        monkeypatch.setattr(live, "_initialize_realtime_monitor", AsyncMock())
        monkeypatch.setattr(live, "_cleanup_expired_data", AsyncMock())
        monkeypatch.setattr(live, "_initialize_candle_cache", AsyncMock(return_value=True))
        monkeypatch.setattr(live, "_update_cache_with_fresh_candles", AsyncMock())
        monkeypatch.setattr(live, "_check_future_candle_triggers", AsyncMock(return_value=False))
        monkeypatch.setattr(live, "_check_budgets", Mock(return_value=True))
        monkeypatch.setattr(live, "_fetch_htf_candles", AsyncMock(return_value={}))
        monkeypatch.setattr(live, "_fetch_recent_trades", AsyncMock(return_value=[]))
        monkeypatch.setattr(live, "_observe_session_tape", Mock())
        arm = AsyncMock()
        monkeypatch.setattr(live, "_handle_code_sniper_signal", arm)
        error_log = Mock()
        monkeypatch.setattr(runner, "safe_error_log", error_log)
        decision = AsyncMock(side_effect=ValueError("judge unavailable"))
        monkeypatch.setattr(runner, "multi_expert_decision_with_details", decision)

        assert await live.run() is None
        decision.assert_awaited_once()
        assert decision.await_args.kwargs["use_code_sniper"] is True
        assert any("Multi-expert decision failed" in call.args[2] and call.kwargs["error"] == "judge unavailable"
                   for call in error_log.call_args_list)
        arm.assert_not_called()
        broker.place.assert_not_called()


def test_map_multi_expert_signal_valid_flat():
    signal = MultiExpertSignal(signal="flat", ttl_minutes=90, confidence=0.0)
    assert runner._map_multi_expert_signal(signal) is None


def test_map_multi_expert_signal_invalid_direction_raises():
    signal = MultiExpertSignal.model_construct(signal="invalid_direction", entry=100.0, stop=95.0, target=105.0)
    with pytest.raises(ValueError, match="Invalid signal direction: invalid_direction"):
        runner._map_multi_expert_signal(signal)
