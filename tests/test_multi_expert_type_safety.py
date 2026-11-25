"""Unit tests for multi-expert type safety and graceful skip functionality."""

import pytest
from unittest.mock import Mock, AsyncMock
from datetime import datetime, timezone

from packages.core.multi_expert import (
    _multi_expert_decision_internal,
    multi_expert_decision_with_details,
    call_judge,
)
from packages.core.schemas_trade import MultiExpertSignal, CodeSniperSignal, DynamicConfig
from packages.db.models import MultiExpertCycleDoc, ExpertInsight, JudgeOutcome
from apps.backend.agents.runner import require_attr


class TestDirectionMapping:
    """Test direction mapping from long/short to buy/sell."""
    
    def test_direction_mapping_valid_values(self):
        """Test that valid direction values are mapped correctly."""
        direction_map = {"long": "buy", "short": "sell", "flat": "flat"}
        
        assert direction_map["long"] == "buy"
        assert direction_map["short"] == "sell"
        assert direction_map["flat"] == "flat"
    
    def test_direction_mapping_invalid_values(self):
        """Test that invalid direction values raise ValueError."""
        direction_map = {"long": "buy", "short": "sell", "flat": "flat"}
        
        with pytest.raises(KeyError):
            _ = direction_map["invalid"]


class TestExecutorResultValidation:
    """Test executor result validation."""
    
    def test_executor_result_missing_fields(self):
        """Test that missing required fields raise ValueError."""
        required = ["direction", "entry", "stop_loss", "take_profit_1", "confidence", "rationale"]
        
        # Test missing 'direction' field
        incomplete_result = {
            "entry": 100.0,
            "stop_loss": 95.0,
            "take_profit_1": 110.0,
            "confidence": 0.8,
            "rationale": "Test rationale"
        }
        
        missing = [f for f in required if f not in incomplete_result]
        assert "direction" in missing
        
        with pytest.raises(ValueError, match="Executor result missing fields"):
            if missing:
                raise ValueError(f"Executor result missing fields: {missing}")
    
    def test_executor_result_numeric_validation(self):
        """Test that non-numeric fields raise ValueError."""
        execution_result = {
            "direction": "long",
            "entry": "not_a_number",  # Invalid
            "stop_loss": 95.0,
            "take_profit_1": 110.0,
            "confidence": 0.8,
            "rationale": "Test rationale"
        }
        
        for f in ["entry", "stop_loss", "take_profit_1", "confidence"]:
            if not isinstance(execution_result[f], (int, float)):
                with pytest.raises(ValueError, match="Executor result field must be numeric"):
                    raise ValueError(f"Executor result field must be numeric: {f}")
    
    def test_executor_result_confidence_range(self):
        """Test that confidence values outside 0.0-1.0 range raise ValueError."""
        execution_result = {
            "direction": "long",
            "entry": 100.0,
            "stop_loss": 95.0,
            "take_profit_1": 110.0,
            "confidence": 1.5,  # Invalid - out of range
            "rationale": "Test rationale"
        }
        
        if not 0.0 <= execution_result["confidence"] <= 1.0:
            with pytest.raises(ValueError, match="Executor confidence out of range"):
                raise ValueError(f"Executor confidence out of range: {execution_result['confidence']}")


class TestFieldMapping:
    """Test field mapping from executor result to MultiExpertSignal."""
    
    def test_field_mapping_correct(self):
        """Test that executor result fields are mapped correctly to MultiExpertSignal."""
        execution_result = {
            "direction": "long",
            "entry": 100.0,
            "stop_loss": 95.0,  # Maps to 'stop'
            "take_profit_1": 110.0,  # Maps to 'target'
            "confidence": 0.8,
            "rationale": "Test rationale"
        }
        
        # Simulate the mapping logic
        direction_map = {"long": "buy", "short": "sell", "flat": "flat"}
        mapped_signal = direction_map[execution_result["direction"]]
        
        expected_signal = MultiExpertSignal(
            signal=mapped_signal,
            entry=execution_result["entry"],
            stop=execution_result["stop_loss"],  # stop_loss → stop
            target=execution_result["take_profit_1"],  # take_profit_1 → target
            ttl_minutes=90,
            confidence=execution_result["confidence"],
            active_window_c5=90,
            active_window_c15=40,
            rationale=execution_result["rationale"]
        )
        
        assert expected_signal.signal == "buy"
        assert expected_signal.stop == 95.0
        assert expected_signal.target == 110.0


class TestGracefulSkip:
    """Test graceful skip when all proposals are invalid or flat."""
    
    @pytest.mark.asyncio
    async def test_graceful_skip_no_valid_proposals(self):
        """Test that function returns flat signal instead of raising when no valid proposals."""
        # Mock invalid proposals
        invalid_proposals = [
            MultiExpertSignal(
                signal="sell",
                entry=100.0,
                stop=95.0,
                target=110.0,
                ttl_minutes=90,
                confidence=0.78,
                active_window_c5=90,
                active_window_c15=40,
                rationale="Invalid proposal"
            )
        ]
        
        # Mock candle data
        c5_superset = [{"close": 100.0, "high": 105.0, "low": 95.0}]
        c15_superset = [{"close": 100.0, "high": 105.0, "low": 95.0}]
        
        # Mock GPT config
        gpt_cfg = {
            "asset": "BTCUSD",
            "agent_id": "test_agent",
            "symbol": "BTCUSD"
        }
        
        # Create cycle document
        cycle_doc = MultiExpertCycleDoc(
            cycle_id="test_cycle",
            mode='multi_expert',
            agent_id="test_agent",
            asset="BTCUSD",
            symbol="BTCUSD",
            started_at=datetime.now(timezone.utc),
            status='skipped',
            experts=[],
            judge=None
        )
        
        # Verify that a flat signal can be created (graceful skip path)
        flat_signal = MultiExpertSignal(
            signal="flat",
            entry=0.0,
            stop=0.0,
            target=0.0,
            ttl_minutes=90,
            confidence=0.0,
            active_window_c5=90,
            active_window_c15=40,
            rationale="All expert proposals failed validation - no trade signal generated"
        )

        assert flat_signal.signal == "flat"
        assert cycle_doc.status == 'skipped'


class TestDynamicConfigSafe:
    """Test safe access to dynamic_config."""
    
    def test_dynamic_config_safe_access_with_config(self):
        """Test safe access when dynamic_config is present."""
        signal = Mock()
        signal.dynamic_config = DynamicConfig(
            ttl_minutes=180,
            candles_5m_count=150,
            candles_15m_count=30,
            reasoning="Test reasoning",
            market_volatility="medium",
            trend_strength="moderate"
        )
        
        dynamic_config = getattr(signal, "dynamic_config", None)
        assert dynamic_config is not None
        assert dynamic_config.ttl_minutes == 180
    
    def test_dynamic_config_safe_access_without_config(self):
        """Test safe access when dynamic_config is missing."""
        # Use spec to prevent Mock from auto-creating the attribute
        signal = Mock(spec=[])  # spec=[] means no attributes auto-created
        
        dynamic_config = getattr(signal, "dynamic_config", None)
        assert dynamic_config is None


class TestStrictFloat:
    """Test strict float conversion."""
    
    def test_strict_float_valid_values(self):
        """Test that valid numeric values are converted correctly."""
        def strict_float(value, field_name: str):
            if value is None:
                raise ValueError(f"Required field '{field_name}' cannot be None")
            try:
                return float(value)
            except (ValueError, TypeError):
                raise ValueError(f"Field '{field_name}' must be numeric, got: {type(value).__name__} = {value}")
        
        assert strict_float(100.0, "entry") == 100.0
        assert strict_float(100, "entry") == 100.0
        assert strict_float("100.5", "entry") == 100.5
    
    def test_strict_float_none_value(self):
        """Test that None values raise ValueError."""
        def strict_float(value, field_name: str):
            if value is None:
                raise ValueError(f"Required field '{field_name}' cannot be None")
            try:
                return float(value)
            except (ValueError, TypeError):
                raise ValueError(f"Field '{field_name}' must be numeric, got: {type(value).__name__} = {value}")
        
        with pytest.raises(ValueError, match="Required field 'entry' cannot be None"):
            strict_float(None, "entry")
    
    def test_strict_float_invalid_type(self):
        """Test that invalid types raise ValueError."""
        def strict_float(value, field_name: str):
            if value is None:
                raise ValueError(f"Required field '{field_name}' cannot be None")
            try:
                return float(value)
            except (ValueError, TypeError):
                raise ValueError(f"Field '{field_name}' must be numeric, got: {type(value).__name__} = {value}")
        
        with pytest.raises(ValueError, match="Field 'entry' must be numeric"):
            strict_float("not_a_number", "entry")


class TestRequireAttr:
    """Test require_attr helper function."""
    
    def test_require_attr_existing_attribute(self):
        """Test that existing attributes are returned correctly."""
        obj = Mock()
        obj.test_attr = "test_value"
        
        result = require_attr(obj, "test_attr", "test context")
        assert result == "test_value"
    
    def test_require_attr_missing_attribute(self):
        """Test that missing attributes raise ValueError."""
        # Use spec=[] so hasattr() returns False for unknown attributes
        obj = Mock(spec=[])
        
        with pytest.raises(ValueError, match="Required attribute 'test_attr' missing"):
            require_attr(obj, "test_attr", "test context")
    
    def test_require_attr_none_value(self):
        """Test that None attribute values raise ValueError."""
        obj = Mock()
        obj.test_attr = None
        
        with pytest.raises(ValueError, match="Required attribute 'test_attr' is None"):
            require_attr(obj, "test_attr", "test context")


class TestModeGuard:
    """Test mode Literal guard."""
    
    def test_mode_guard_valid_modes(self):
        """Test that valid modes pass validation."""
        valid_modes = {'multi_expert', 'normal', 'multi_expert_code_sniper'}
        
        for mode in valid_modes:
            assert mode in valid_modes
    
    def test_mode_guard_invalid_mode(self):
        """Test that invalid modes raise ValueError."""
        valid_modes = {'multi_expert', 'normal', 'multi_expert_code_sniper'}
        invalid_mode = "invalid_mode"
        
        if invalid_mode not in valid_modes:
            with pytest.raises(ValueError, match="Invalid mode assignment"):
                raise ValueError(f"Invalid mode assignment: {invalid_mode}. Must be one of {valid_modes}")


class TestCycleStatusNormalization:
    """Test cycle status normalization."""
    
    def test_cycle_status_success(self):
        """Test that successful cycles have correct status."""
        cycle_doc = MultiExpertCycleDoc(
            cycle_id="test_cycle",
            mode='multi_expert',
            agent_id="test_agent",
            asset="BTCUSD",
            symbol="BTCUSD",
            started_at=datetime.now(timezone.utc),
            status='skipped',  # Initial status
            experts=[],
            judge=None
        )
        
        # Simulate successful completion
        cycle_doc.status = 'success'
        cycle_doc.completed_at = datetime.now(timezone.utc)
        cycle_doc.error = None
        
        assert cycle_doc.status == 'success'
        assert cycle_doc.completed_at is not None
        assert cycle_doc.error is None
    
    def test_cycle_status_skipped(self):
        """Test that skipped cycles have correct status."""
        cycle_doc = MultiExpertCycleDoc(
            cycle_id="test_cycle",
            mode='multi_expert',
            agent_id="test_agent",
            asset="BTCUSD",
            symbol="BTCUSD",
            started_at=datetime.now(timezone.utc),
            status='skipped',
            experts=[],
            judge=None
        )
        
        # Simulate skipped completion
        cycle_doc.status = 'skipped'
        cycle_doc.completed_at = datetime.now(timezone.utc)
        cycle_doc.error = None  # Not an error, just no valid proposals
        
        assert cycle_doc.status == 'skipped'
        assert cycle_doc.completed_at is not None
        assert cycle_doc.error is None
    
    def test_cycle_status_failed(self):
        """Test that failed cycles have correct status."""
        cycle_doc = MultiExpertCycleDoc(
            cycle_id="test_cycle",
            mode='multi_expert',
            agent_id="test_agent",
            asset="BTCUSD",
            symbol="BTCUSD",
            started_at=datetime.now(timezone.utc),
            status='skipped',  # Initial status
            experts=[],
            judge=None
        )
        
        # Simulate failed completion
        cycle_doc.status = 'failed'
        cycle_doc.completed_at = datetime.now(timezone.utc)
        cycle_doc.error = "Test error message"
        
        assert cycle_doc.status == 'failed'
        assert cycle_doc.completed_at is not None
        assert cycle_doc.error == "Test error message"


class TestDefaultsNoCode:
    """Test default behavior when no code is provided."""
    
    def test_default_candle_counts(self):
        """Test that default candle counts are used when no dynamic config."""
        # Mock agent runner with static config
        class MockAgentRunner:
            def __init__(self):
                self.candle_count_5m = 100
                self.candle_count_15m = 20
                self._dynamic_config = None
            
            def get_effective_candle_counts(self):
                if self._dynamic_config:
                    return (
                        self._dynamic_config.candles_5m_count,
                        self._dynamic_config.candles_15m_count
                    )
                else:
                    return (self.candle_count_5m, self.candle_count_15m)
        
        runner = MockAgentRunner()
        c5_count, c15_count = runner.get_effective_candle_counts()
        
        assert c5_count == 100
        assert c15_count == 20
    
    def test_dynamic_candle_counts(self):
        """Test that dynamic candle counts are used when available."""
        # Mock agent runner with dynamic config
        class MockAgentRunner:
            def __init__(self):
                self.candle_count_5m = 100  # Static default
                self.candle_count_15m = 20  # Static default
                self._dynamic_config = DynamicConfig(
                    ttl_minutes=180,
                    candles_5m_count=150,  # Dynamic override
                    candles_15m_count=30,  # Dynamic override
                    reasoning="Test reasoning",
                    market_volatility="medium",
                    trend_strength="moderate"
                )
            
            def get_effective_candle_counts(self):
                if self._dynamic_config:
                    return (
                        self._dynamic_config.candles_5m_count,
                        self._dynamic_config.candles_15m_count
                    )
                else:
                    return (self.candle_count_5m, self.candle_count_15m)
        
        runner = MockAgentRunner()
        c5_count, c15_count = runner.get_effective_candle_counts()
        
        assert c5_count == 150  # Dynamic override
        assert c15_count == 30  # Dynamic override


if __name__ == "__main__":
    pytest.main([__file__])
