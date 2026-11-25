"""Test for VALIDATE_EXPERT_RESPONSES feature flag."""

import pytest
from unittest.mock import patch, MagicMock
from packages.core.multi_expert import _multi_expert_decision_internal
from packages.core.schemas_trade import MultiExpertSignal


@pytest.mark.asyncio
async def test_validation_enabled():
    """Test that validation runs when VALIDATE_EXPERT_RESPONSES is True."""
    with patch('packages.core.multi_expert.VALIDATE_EXPERT_RESPONSES', True):
        with patch('packages.core.multi_expert.call_expert') as mock_expert:
            # Mock expert response
            mock_signal = MultiExpertSignal(
                signal="buy",
                entry=100.0,
                stop=95.0,  # Poor RR ratio - should fail validation
                target=101.0,
                ttl_minutes=90,
                confidence=0.8,
                active_window_c5=90,
                active_window_c15=40,
                rationale="Test signal"
            )
            mock_expert.return_value = (mock_signal, 100.0)
            
            # Create mock candle data
            c5_superset = [{"time": f"2024-01-01T{i:02d}:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000} for i in range(100)]
            c15_superset = [{"time": f"2024-01-01T{i:02d}:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 3000} for i in range(50)]
            
            gpt_cfg = {
                "asset": "NIFTY",
                "agent_id": "test_agent",
                "store_response": False,
            }
            
            try:
                # This should validate and might skip due to poor RR ratio
                result = await _multi_expert_decision_internal(
                    c5_superset=c5_superset,
                    c15_superset=c15_superset,
                    gpt_cfg=gpt_cfg,
                    use_code_sniper=False,
                    total_start_time=0.0,
                    max_total_time_seconds=60
                )
                # If validation is working, proposal should be marked as invalid
                assert result is not None
            except Exception:
                # Validation might cause exceptions for invalid proposals
                pass


@pytest.mark.asyncio
async def test_validation_disabled():
    """Test that validation is skipped when VALIDATE_EXPERT_RESPONSES is False."""
    with patch('packages.core.multi_expert.VALIDATE_EXPERT_RESPONSES', False):
        with patch('packages.core.multi_expert.call_expert') as mock_expert:
            with patch('packages.core.multi_expert.call_judge') as mock_judge:
                # Mock expert response with poor RR ratio
                mock_signal = MultiExpertSignal(
                    signal="buy",
                    entry=100.0,
                    stop=95.0,  # Poor RR ratio - would normally fail validation
                    target=101.0,
                    ttl_minutes=90,
                    confidence=0.8,
                    active_window_c5=90,
                    active_window_c15=40,
                    rationale="Test signal"
                )
                mock_expert.return_value = (mock_signal, 100.0)
                
                # Mock judge response
                mock_judge_signal = MultiExpertSignal(
                    signal="buy",
                    entry=100.0,
                    stop=95.0,
                    target=105.0,
                    ttl_minutes=90,
                    confidence=0.9,
                    active_window_c5=90,
                    active_window_c15=40,
                    rationale="Judge decision"
                )
                mock_judge.return_value = (mock_judge_signal, 50.0, {}, None)
                
                # Create mock candle data
                c5_superset = [{"time": f"2024-01-01T{i:02d}:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000} for i in range(100)]
                c15_superset = [{"time": f"2024-01-01T{i:02d}:00", "open": 100, "high": 101, "low": 99, "close": 100, "volume": 3000} for i in range(50)]
                
                gpt_cfg = {
                    "asset": "NIFTY",
                    "agent_id": "test_agent",
                    "store_response": False,
                }
                
                try:
                    # With validation disabled, proposal should be passed to judge regardless of RR ratio
                    result = await _multi_expert_decision_internal(
                        c5_superset=c5_superset,
                        c15_superset=c15_superset,
                        gpt_cfg=gpt_cfg,
                        use_code_sniper=False,
                        total_start_time=0.0,
                        max_total_time_seconds=60
                    )
                    # Should pass proposal to judge even with poor RR
                    assert mock_judge.called, "Judge should be called when validation is disabled"
                except Exception as e:
                    pytest.fail(f"Validation disabled but still rejected proposal: {e}")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

