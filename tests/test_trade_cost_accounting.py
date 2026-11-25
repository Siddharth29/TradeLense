"""Money/evidence contracts: parallel cycles, cache billing and partial exits."""
import asyncio
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from packages.core.ai_accounting import APICall, DecisionCosts, decision_accounting, price_usage
from packages.core.operational_ledger import OperationalLedger, position_cost
from packages.core.config import MT5_MAGIC_NUMBER
from packages.db.repositories import comprehensive_logging as trades
from src.common import logger as event_logger
from scripts.logs_to_report import _exit_row


def test_cache_writes_reads_and_thinking_are_billed_without_double_counting():
    anthropic = price_usage("anthropic", "claude-sonnet-5-5", {
        "input_tokens": 100, "output_tokens": 200,
        "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 500,
        "cache_creation": {"ephemeral_5m_input_tokens": 300, "ephemeral_1h_input_tokens": 200},
    })
    assert Decimal(anthropic["cost_decimal_usd"]) == Decimal("0.00385")
    openai = price_usage("openai", "gpt-4o-mini", {
        "prompt_tokens": 1100, "completion_tokens": 200,
        "prompt_tokens_details": {"cached_tokens": 1000},
    })
    assert Decimal(openai["cost_decimal_usd"]) == Decimal("0.00021")
    gemini = price_usage("gemini", "gemini-2.5-flash", {
        "prompt_token_count": 1100, "candidates_token_count": 100,
        "thoughts_token_count": 100, "cached_content_token_count": 1000,
    })
    assert Decimal(gemini["cost_decimal_usd"]) == Decimal("0.00056")


@pytest.mark.asyncio
async def test_parallel_cycles_keep_their_own_combined_total_and_bill_bad_json():
    async def expert():
        with APICall("openai", "gpt-4o-mini") as call:
            await asyncio.sleep(0)
            call.record({"prompt_tokens": 1000, "completion_tokens": 1000})
        # Parsing/validation can fail after a billed response.
        return "invalid model JSON"

    @decision_accounting
    async def decide(cycle_id):
        await asyncio.gather(expert(), expert())
        return SimpleNamespace(metadata={}), SimpleNamespace()

    results = await asyncio.gather(decide("cycle-a"), decide("cycle-b"))
    for (signal, cycle), cycle_id in zip(results, ("cycle-a", "cycle-b")):
        assert cycle.ai_cost["total_usd"] == pytest.approx(.0015)
        assert len(cycle.ai_cost["calls"]) == 2
        assert {c["cycle_id"] for c in cycle.ai_cost["calls"]} == {cycle_id}
        assert signal.metadata["ai_cost"] == cycle.ai_cost


def test_timeout_retry_or_unknown_model_never_reports_an_exact_free_total():
    with DecisionCosts("cycle") as costs:
        with pytest.raises(TimeoutError):
            with APICall("anthropic", "claude-sonnet-5-5"):
                raise TimeoutError("usage not received")
        with APICall("openai", "gpt-4o-mini") as call:
            call.record({"prompt_tokens": 1000, "completion_tokens": 1000})
    assert costs.summary()["total_usd"] is None
    assert costs.summary()["known_subtotal_usd"] == pytest.approx(.00075)
    with APICall("anthropic", "new-model-without-a-rate") as call:
        call.record({"input_tokens": 100, "output_tokens": 100})
    assert call.record_data["cost_usd"] is None
    assert "rate card" in call.record_data["error"]


def test_malformed_sdk_metadata_is_not_traversed_or_treated_as_free():
    with APICall("anthropic", "claude-sonnet-5-5") as call:
        call.record(Mock(), response_id=Mock(), response_model=Mock())
    assert call.record_data["usage"] == {}
    assert call.record_data["cost_usd"] is None
    assert call.record_data["provider_response_id"] is None


@pytest.mark.asyncio
async def test_provider_client_records_paid_usage_before_parsing_the_response(monkeypatch):
    from packages.core import gpt_client
    monkeypatch.setattr(gpt_client, "_api_key", "test-only")
    monkeypatch.setattr(gpt_client, "_active_provider", "openai")
    response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
        "id": "provider-request-1", "model": "gpt-4o-mini-2024-07-18",
        "choices": [{"message": {"content": "not valid model JSON"}}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 1000},
    })
    monkeypatch.setattr(gpt_client, "_get_async_client", lambda _: SimpleNamespace(post=AsyncMock(return_value=response)))
    with DecisionCosts("paid-bad-json") as costs:
        result = await gpt_client.complete("test", model="gpt-4o-mini", store_response=False)
    assert result == "not valid model JSON"
    assert costs.summary()["total_usd"] == pytest.approx(.00075)
    assert costs.calls[0]["provider_response_id"] == "provider-request-1"


def test_broker_charges_include_entry_all_partials_and_credits_once(tmp_path):
    now = datetime(2026, 11, 17, tzinfo=timezone.utc)
    def deal(ticket, entry, volume, commission=0., swap=0., fee=0.):
        return {"ticket": ticket, "position_id": 91, "time": now+timedelta(minutes=ticket),
                "symbol": "XAUUSD", "entry": entry, "volume": volume, "price": 2000.,
                "type": 0, "magic": MT5_MAGIC_NUMBER, "profit": 0.,
                "commission": commission, "swap": swap, "fee": fee}
    ledger = OperationalLedger(tmp_path / "costs.sqlite3")
    legs = [deal(1, 0, 1., commission=-2.), deal(2, 1, .5, fee=-.5)]
    ledger.ingest("account-a", legs, magic=MT5_MAGIC_NUMBER)
    assert position_cost(ledger, "account-a", "91", "USD")["status"] == "accrued"
    legs.append(deal(3, 1, .5, commission=-1., swap=.25))
    ledger.ingest("account-a", legs, magic=MT5_MAGIC_NUMBER)
    ledger.ingest("account-a", list(reversed(legs)), magic=MT5_MAGIC_NUMBER)
    cost = position_cost(ledger, "account-a", "91", "USD")
    assert cost["total"] == 3.25 and cost["status"] == "final"
    assert cost["commission"] == 3. and cost["swap"] == -.25
    assert cost["deal_ids"] == ["1", "2", "3"]
    with pytest.raises(ValueError):
        position_cost(ledger, "account-b", "91", "USD")
    ledger.close()


@pytest.mark.asyncio
async def test_logonly_partial_preserves_parent_cost_reason_and_csv_evidence(monkeypatch):
    monkeypatch.setenv("PERSIST_ANALYTICS_TO_MONGO", "0")
    now = datetime.now(timezone.utc)
    events = []
    event_logger.add_sink(events.append)
    ai_cost = {"total_usd": .001, "status": "priced", "currency": "USD"}
    try:
        base = dict(symbol="XAUUSD", asset="XAUUSD", quantity=1., price=2000.,
                    entry_price=2000., agent_id="test", prompt_hash="h",
                    signal_received_at=now, trade_executed_at=now, position_ticket="cost-test")
        await trades.store_comprehensive_trade(**base, cycle_id="original-cycle", ai_cost=ai_cost,
            trade_reason="M15 support held; continuation confirmed", trigger_reason="Closed above 2000")
        await trades.store_comprehensive_trade(**base, trade_status="closed", pnl=2., exit_reason="TP1_PARTIAL")
        await trades.update_comprehensive_trade_exit(symbol="XAUUSD", exit_price=2010., exit_reason="TP",
            pnl=5., position_ticket="cost-test", broker_cost={"total": 2., "currency": "USD", "status": "final"})
    finally:
        event_logger.remove_sink(events.append)
    final = [e["payload"] for e in events if e.get("event") == "trade_exit"][-1]
    assert final["cycle_id"] == "original-cycle"
    assert final["ai_cost"] == ai_cost
    assert final["trade_reason"] == "M15 support held; continuation confirmed"
    row = _exit_row(final)
    assert row["broker_charges"] == 2. and row["ai_cost_usd"] == .001
    assert row["trade_reason"] == final["trade_reason"]


@pytest.mark.asyncio
async def test_analysis_api_exposes_saved_costs_without_nearby_cycle_attribution(monkeypatch):
    from apps.backend import api
    from packages.db.repositories.multi_expert_cycles import multi_expert_cycles_repo
    now = datetime.now(timezone.utc)
    trade = {"_id": "trade-1", "asset": "XAUUSD", "symbol": "XAUUSD",
             "trade_executed_at": now, "cycle_id": "original", "position_ticket": "91",
             "trade_reason": "Support reclaim", "trigger_reason": "Retest closed higher",
             "ai_cost": {"total_usd": .012345, "status": "priced", "currency": "USD"},
             "broker_cost": {"total": 3.25, "status": "final", "currency": "USD"}}
    monkeypatch.setattr(trades, "get_all_comprehensive_trades", AsyncMock(return_value=[trade]))
    monkeypatch.setattr(multi_expert_cycles_repo, "get_cycle", AsyncMock(return_value=None))
    # A different cycle exists nearby: it must not supply this trade's money.
    monkeypatch.setattr(multi_expert_cycles_repo, "list_cycles", AsyncMock(return_value=[]))
    response = await api.get_trading_analysis_trades(asset=None, status=None, side=None,
                                                     start=None, end=None, limit=50)
    [result] = response["trades"]
    for key in ("ai_cost", "broker_cost", "trade_reason", "trigger_reason", "cycle_id", "position_ticket"):
        assert result[key] == trade[key]


def test_backtest_schema_retains_cycle_costs_and_closed_position_evidence():
    from packages.core.backtest_normalizer import normalize_iteration_data
    from packages.db.models import BacktestIterationDoc
    now = datetime.now(timezone.utc)
    ai_cost = {"total_usd": .002, "currency": "USD", "status": "priced", "cycle_id": "armed-cycle"}
    stored = BacktestIterationDoc(**normalize_iteration_data({
        "backtest_id": "replay", "iteration": 3, "iteration_timestamp": now,
        "ai_cost": ai_cost, "trade_reason": "Support reclaim",
        "trade_outcome": {"result": "TP", "profit": 2., "ai_cost": ai_cost,
                          "trade_reason": "Support reclaim", "broker_cost": {
                              "total": 1., "status": "simulated", "currency": "USD"}},
    })).model_dump()
    assert stored["ai_cost"] == ai_cost
    assert stored["trade_outcome"]["ai_cost"] == ai_cost
    assert stored["trade_outcome"]["broker_cost"]["status"] == "simulated"
