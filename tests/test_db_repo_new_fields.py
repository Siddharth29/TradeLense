"""Unit tests for database repository with new fields support."""

import pytest
from datetime import datetime, timezone
from packages.db.repositories.gpt_responses import (
    store_gpt_response,
    store_gpt_call_summary,
    get_gpt_responses_by_asset,
    get_gpt_call_summaries_by_asset,
)


class TestDatabaseRepoNewFields:
    """Test database repository support for new audit fields."""
    
    @pytest.mark.asyncio
    async def test_store_gpt_response_with_new_fields(self):
        """Test repo unit: insert/read returns docs with new fields."""
        
        # Store GPT response with new fields
        response_id = await store_gpt_response(
            asset="NIFTY",
            agent_id="test_agent",
            model="gpt-4",
            prompt_hash="test_hash_123",
            prompt_content="Test prompt content",
            response_content='{"signal": "buy", "entry": 25000, "stop": 24900, "target": 25300}',
            response_type="json",
            latency_ms=1250.5,
            symbol="NIFTY",
            parsed_decision={"signal": "buy", "entry": 25000},
            confidence=0.8,
            pattern="breakout",
            direction="long",
            level=25000.0,
            stop_loss=24900.0,
            take_profit=25300.0,
            broker_type="mt5",
            gpt_mode="multi_expert_trend",
            # New fields for audit and TTL tracking
            ttl_minutes=90,
            active_window_c5=50,
            active_window_c15=30,
            rationale="Strong breakout pattern with high volume confirmation",
        )
        
        assert response_id is not None
        assert isinstance(response_id, str)
        
        # Retrieve the stored response and verify new fields
        responses = await get_gpt_responses_by_asset("NIFTY", limit=1)
        assert len(responses) > 0
        
        stored_response = responses[0]
        
        # Verify all new fields are preserved
        assert stored_response["ttl_minutes"] == 60
        assert stored_response["active_window_c5"] == 50
        assert stored_response["active_window_c15"] == 30
        assert stored_response["rationale"] == "Strong breakout pattern with high volume confirmation"
        
        # Verify existing fields still work
        assert stored_response["asset"] == "NIFTY"
        assert stored_response["agent_id"] == "test_agent"
        assert stored_response["confidence"] == 0.8
        assert stored_response["pattern"] == "breakout"
        assert stored_response["direction"] == "long"
        assert stored_response["broker_type"] == "mt5"
    
    @pytest.mark.asyncio
    async def test_store_gpt_response_legacy_compatibility(self):
        """Test that legacy calls without new fields still work."""
        
        # Store GPT response without new fields (legacy compatibility)
        response_id = await store_gpt_response(
            asset="BANKNIFTY",
            agent_id="legacy_agent", 
            model="gpt-3.5-turbo",
            prompt_hash="legacy_hash",
            prompt_content="Legacy prompt",
            response_content="Legacy response",
            response_type="text",
            latency_ms=800.0,
            # No new fields provided
        )
        
        assert response_id is not None
        
        # Retrieve and verify new fields are None for legacy compatibility
        responses = await get_gpt_responses_by_asset("BANKNIFTY", limit=1)
        assert len(responses) > 0
        
        stored_response = responses[0]
        
        # New fields should be None when not provided
        assert stored_response["ttl_minutes"] is None
        assert stored_response["active_window_c5"] is None
        assert stored_response["active_window_c15"] is None
        assert stored_response["rationale"] is None
        
        # Existing fields should still work
        assert stored_response["asset"] == "BANKNIFTY"
        assert stored_response["agent_id"] == "legacy_agent"
    
    @pytest.mark.asyncio
    async def test_store_gpt_call_summary_with_new_fields(self):
        """Test GPT call summary storage with new fields."""
        
        summary_id = await store_gpt_call_summary(
            asset="NIFTY",
            agent_id="test_agent",
            prompt_hash="test_hash_456",
            gpt_response_id="response_id_123",
            latency_ms=2100.3,
            response_type="json",
            decision_made=True,
            symbol="NIFTY",
            order_placed=True,
            trade_executed=True,
            trigger_met=True,
            confidence=0.75,
            pattern="retest",
            direction="short",
            broker_type="mt5",
            gpt_mode="multi_expert_judge",
            # New fields for audit and TTL tracking
            ttl_minutes=180,
            active_window_c5=75,
            active_window_c15=40,
            rationale="Retest of previous resistance with bearish divergence",
        )
        
        assert summary_id is not None
        
        # Retrieve and verify new fields in summary
        summaries = await get_gpt_call_summaries_by_asset("NIFTY", limit=1)
        assert len(summaries) > 0
        
        stored_summary = summaries[0]
        
        # Verify new fields
        assert stored_summary["ttl_minutes"] == 90
        assert stored_summary["active_window_c5"] == 75
        assert stored_summary["active_window_c15"] == 40
        assert stored_summary["rationale"] == "Retest of previous resistance with bearish divergence"
        
        # Verify existing fields
        assert stored_summary["asset"] == "NIFTY"
        assert stored_summary["agent_id"] == "test_agent"
        assert stored_summary["decision_made"] is True
        assert stored_summary["trade_executed"] is True
        assert stored_summary["pattern"] == "retest"
        assert stored_summary["direction"] == "short"
    
    @pytest.mark.asyncio
    async def test_new_fields_data_types_validation(self):
        """Test that new fields accept correct data types."""
        
        # Test with valid data types
        response_id = await store_gpt_response(
            asset="NIFTY",
            agent_id="validation_agent",
            model="gpt-4",
            prompt_hash="validation_hash",
            prompt_content="Validation test",
            response_content="{}",
            response_type="json",
            latency_ms=1000.0,
            ttl_minutes=45,  # Valid TTL value from enum
            active_window_c5=60,  # Valid window size
            active_window_c15=20,  # Valid window size
            rationale="Test rationale with detailed explanation",
        )
        
        assert response_id is not None
        
        # Retrieve and verify data types
        responses = await get_gpt_responses_by_asset("NIFTY", limit=1)
        stored_response = responses[0]
        
        assert isinstance(stored_response["ttl_minutes"], int)
        assert isinstance(stored_response["active_window_c5"], int)
        assert isinstance(stored_response["active_window_c15"], int)
        assert isinstance(stored_response["rationale"], str)
        
        # Verify values
        assert stored_response["ttl_minutes"] == 45
        assert stored_response["active_window_c5"] == 60
        assert stored_response["active_window_c15"] == 20
        assert len(stored_response["rationale"]) > 0
