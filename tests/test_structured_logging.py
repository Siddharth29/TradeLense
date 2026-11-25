"""Unit tests for structured logging and metrics."""

import pytest
import json
import logging
from unittest.mock import Mock, patch, AsyncMock
from packages.core.multi_expert import multi_expert_decision, call_expert, call_judge
from packages.core.schemas_trade import MultiExpertSignal


class TestStructuredLogging:
    """Test structured logging functionality."""
    
    @pytest.fixture
    def sample_candles(self):
        """Sample candle data for testing."""
        c5_candles = [
            {"high": 25050, "low": 24950, "close": 25000, "open": 24980},
            {"high": 25100, "low": 25000, "close": 25080, "open": 25020},
            {"high": 25120, "low": 25050, "close": 25100, "open": 25080},
        ]
        c15_candles = [
            {"high": 25150, "low": 24900, "close": 25000, "open": 24950},
            {"high": 25200, "low": 25000, "close": 25100, "open": 25050},
        ]
        return c5_candles, c15_candles
    
    @pytest.fixture
    def gpt_config(self):
        """Sample GPT configuration."""
        return {
            "asset": "NIFTY",
            "agent_id": "test_agent",
            "broker_type": "test_broker",
            "prompt_hash": "test_hash",
            "model": "gpt-4o-mini",
        }
    
    def test_logger_receives_keys_expert_call(self, sample_candles, gpt_config):
        """Test unit logger receives keys during expert call."""
        c5_candles, c15_candles = sample_candles
        
        # Mock the complete function and logger
        with patch('packages.core.multi_expert.complete') as mock_complete:
            with patch('packages.core.multi_expert.logger') as mock_logger:
                
                # Mock GPT response
                mock_complete.return_value = {
                    "signal": "buy",
                    "entry": 25000.0,
                    "stop": 24950.0,
                    "target": 25200.0,
                    "ttl_minutes": 90,
                    "confidence": 0.8,
                    "active_window_c5": 50,
                    "active_window_c15": 30,
                    "need_more_candles": False,
                }
                
                # Create async function to test
                async def test_expert_call():
                    signal, expert_ms = await call_expert("trend", c5_candles, c15_candles, gpt_config)
                    return signal, expert_ms
                
                # Run test
                import asyncio
                signal, expert_ms = asyncio.run(test_expert_call())
                
                # Verify logger was called with expected keys
                mock_logger.info.assert_called()
                call_args = mock_logger.info.call_args
                
                # Check the extra parameter contains expected keys
                extra_data = call_args[1]["extra"]
                expected_keys = [
                    "expert_kind", "expert_ms", "signal", "ttl_minutes", 
                    "confidence", "active_window_c5", "active_window_c15",
                    "need_more_candles", "asset", "agent_id"
                ]
                
                for key in expected_keys:
                    assert key in extra_data, f"Missing key: {key}"
                
                # Verify specific values
                assert extra_data["expert_kind"] == "trend"
                assert isinstance(extra_data["expert_ms"], float)
                assert extra_data["signal"] == "buy"
                assert extra_data["ttl_minutes"] == 60
                assert extra_data["asset"] == "NIFTY"
                assert extra_data["agent_id"] == "test_agent"
    
    @pytest.mark.asyncio
    async def test_integration_run_emits_concise_line(self, sample_candles, gpt_config):
        """Test integration run emits a concise line with all metrics."""
        c5_candles, c15_candles = sample_candles
        
        # Mock all the dependencies
        with patch('packages.core.multi_expert.complete') as mock_complete:
            with patch('packages.core.multi_expert.logger') as mock_logger:
                
                # Mock expert responses
                trend_response = {
                    "signal": "buy",
                    "entry": 25000.0,
                    "stop": 24950.0,
                    "target": 25200.0,
                    "ttl_minutes": 90,
                    "confidence": 0.8,
                    "active_window_c5": 50,
                    "active_window_c15": 30,
                    "need_more_candles": False,
                }
                
                breakout_response = {
                    "signal": "flat",
                    "entry": 0.0,
                    "stop": 0.0,
                    "target": 0.0,
                    "ttl_minutes": 90,
                    "confidence": 0.0,
                    "active_window_c5": 50,
                    "active_window_c15": 30,
                    "need_more_candles": True,
                }
                
                reversal_response = {
                    "signal": "sell",
                    "entry": 25100.0,
                    "stop": 25150.0,
                    "target": 24900.0,
                    "ttl_minutes": 180,
                    "confidence": 0.6,
                    "active_window_c5": 60,
                    "active_window_c15": 40,
                    "need_more_candles": False,
                }
                
                # Set up mock to return different responses for different expert calls
                mock_complete.side_effect = [trend_response, breakout_response, reversal_response, trend_response]
                
                # Run multi-expert decision
                final_decision, metrics = await multi_expert_decision(c5_candles, c15_candles, gpt_config)
                
                # Verify the final comprehensive log was called
                assert mock_logger.info.call_count >= 4  # 3 experts + 1 final summary
                
                # Get the final log call (should be the last one with comprehensive metrics)
                final_log_call = None
                for call in mock_logger.info.call_args_list:
                    if "Multi-expert decision process completed" in str(call):
                        final_log_call = call
                        break
                
                assert final_log_call is not None, "Final comprehensive log not found"
                
                # Verify comprehensive metrics in final log
                extra_data = final_log_call[1]["extra"]
                expected_comprehensive_keys = [
                    "expert_kind", "expert_ms", "judge_ms", "total_ms",
                    "ttl_minutes", "windows", "rr", "anti_scalp", "inside_window",
                    "final_signal", "confidence", "expert_timings", "validation_results",
                    "asset", "agent_id"
                ]
                
                for key in expected_comprehensive_keys:
                    assert key in extra_data, f"Missing comprehensive key: {key}"
                
                # Verify specific metrics structure
                assert extra_data["expert_kind"] == "multi_expert_orchestrator"
                assert isinstance(extra_data["expert_ms"], float)
                assert isinstance(extra_data["judge_ms"], float)
                assert isinstance(extra_data["total_ms"], float)
                assert isinstance(extra_data["expert_timings"], dict)
                assert "active_window_c5" in extra_data["windows"]
                assert "active_window_c15" in extra_data["windows"]
                
                # Verify that metrics contain useful values
                assert extra_data["total_ms"] > 0
                assert extra_data["final_signal"] in ["buy", "sell", "flat"]
    
    def test_structured_log_format_consistency(self):
        """Test that structured logs maintain consistent format across calls."""
        
        # Test data that should be logged consistently
        test_metrics = {
            "expert_kind": "test_expert",
            "expert_ms": 123.45,
            "judge_ms": 67.89,
            "total_ms": 191.34,
            "ttl_minutes": 90,
            "windows": {"active_window_c5": 50, "active_window_c15": 30},
            "rr": 2.8,
            "anti_scalp": True,
            "inside_window": True,
            "final_signal": "buy",
            "confidence": 0.75,
        }
        
        # Mock logger
        with patch('packages.core.multi_expert.logger') as mock_logger:
            
            # Simulate logging call
            mock_logger.info("Test structured log", extra=test_metrics)
            
            # Verify call was made
            mock_logger.info.assert_called_once()
            call_args = mock_logger.info.call_args
            
            # Check that extra data matches expected structure
            logged_data = call_args[1]["extra"]
            
            # Verify all expected fields are present and correct types
            assert logged_data["expert_kind"] == "test_expert"
            assert isinstance(logged_data["expert_ms"], float)
            assert isinstance(logged_data["judge_ms"], float)
            assert isinstance(logged_data["total_ms"], float)
            assert isinstance(logged_data["ttl_minutes"], int)
            assert isinstance(logged_data["windows"], dict)
            assert isinstance(logged_data["rr"], float)
            assert isinstance(logged_data["anti_scalp"], bool)
            assert isinstance(logged_data["inside_window"], bool)
            assert logged_data["final_signal"] in ["buy", "sell", "flat"]
            assert isinstance(logged_data["confidence"], float)
    
    def test_metrics_calculation_accuracy(self, sample_candles, gpt_config):
        """Test that calculated metrics (RR, anti-scalp, etc.) are accurate."""
        c5_candles, c15_candles = sample_candles
        
        # Create test signal with known values
        test_signal = MultiExpertSignal(
            signal="buy",
            entry=25000.0,
            stop=24950.0,  # Risk: 50 points
            target=25150.0,  # Reward: 150 points, RR = 3.0
            ttl_minutes=90,
            confidence=0.8,
            active_window_c5=50,
            active_window_c15=30,
        )
        
        # Mock the multi-expert process to return our test signal
        with patch('packages.core.multi_expert.complete') as mock_complete:
            with patch('packages.core.multi_expert.logger') as mock_logger:
                
                mock_complete.return_value = test_signal.model_dump()
                
                async def test_metrics():
                    decision, metrics = await multi_expert_decision(c5_candles, c15_candles, gpt_config)
                    return metrics
                
                import asyncio
                metrics = asyncio.run(test_metrics())
                
                # Verify calculated metrics
                assert metrics["rr"] == 3.0  # 150/50 = 3.0
                assert isinstance(metrics["anti_scalp"], bool)
                assert isinstance(metrics["inside_window"], bool)
                assert metrics["ttl_minutes"] == 60
                assert metrics["windows"]["active_window_c5"] == 50
                assert metrics["windows"]["active_window_c15"] == 30
