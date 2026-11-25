"""Unit tests for runner's final safety gate validation."""

import pytest
from apps.backend.agents.runner import _validate_final_safety_gate


class TestRunnerSafetyGate:
    """Test the runner's final safety gate validation."""
    
    @pytest.fixture
    def sample_candles(self):
        """Sample candle data for testing."""
        c5_window = [
            {"high": 100.5, "low": 99.5},
            {"high": 101.0, "low": 100.0},
            {"high": 100.8, "low": 99.8},
            {"high": 101.2, "low": 100.2},
            {"high": 100.6, "low": 99.6},
        ]
        c15_window = [
            {"high": 101.5, "low": 99.0},
            {"high": 102.0, "low": 99.5},
            {"high": 101.8, "low": 99.8},
        ]
        return c5_window, c15_window
    
    def test_valid_trade_passes_safety_gate(self, sample_candles):
        """Test that a valid trade passes all safety checks."""
        c5_window, c15_window = sample_candles
        
        # Valid long trade: good RR, sufficient distance, inside windows
        # Window range: low=99.0, high=102.0, so all levels must be within this range
        is_valid, reason = _validate_final_safety_gate(
            entry=100.0,
            stop=99.2,    # Risk: 0.8 (within window)
            target=102.0, # Reward: 2.0, RR = 2.5 (meets minimum, at window edge)
            direction="long",
            c5_window=c5_window,
            c15_window=c15_window,
            anti_scalp_k=1.0  # Very low k to ensure anti-scalp passes
        )
        
        assert is_valid is True, f"Expected valid trade but got: {reason}"
        assert reason == "All safety checks passed"
    
    def test_low_risk_reward_fails_safety_gate(self, sample_candles):
        """Test case 1: Trade with insufficient risk-reward ratio gets skipped."""
        c5_window, c15_window = sample_candles
        
        # Low RR trade: RR = 1.5 (below minimum 2.5)
        is_valid, reason = _validate_final_safety_gate(
            entry=100.0,
            stop=99.0,    # Risk: 1.0
            target=101.5, # Reward: 1.5, RR = 1.5 (below minimum 2.5)
            direction="long",
            c5_window=c5_window,
            c15_window=c15_window,
            min_rr=2.5,
            anti_scalp_k=0.1,  # anti-scalp is checked first — keep it passing
        )

        assert is_valid is False
        assert "RR 1.50 below minimum 2.5" in reason
    
    def test_anti_scalp_fails_safety_gate(self, sample_candles):
        """Test case 2: Trade failing anti-scalp check gets skipped."""
        c5_window, c15_window = sample_candles
        
        # Anti-scalp failure: target too close (median range ~0.8, k=6.0, min_distance ~4.8)
        is_valid, reason = _validate_final_safety_gate(
            entry=100.0,
            stop=97.0,    # Risk: 3.0  
            target=108.0, # Reward: 8.0, RR = 2.67 (good)
            direction="long",
            c5_window=c5_window,
            c15_window=c15_window,
            anti_scalp_k=20.0  # Very high k to force failure
        )
        
        assert is_valid is False
        assert "Anti-scalp failed" in reason
        assert "distance" in reason
        assert "required" in reason
    
    def test_outside_windows_fails_safety_gate(self, sample_candles):
        """Test case 3: Trade with levels outside windows gets skipped."""
        c5_window, c15_window = sample_candles
        
        # Outside windows: target way above the highest high in windows  
        is_valid, reason = _validate_final_safety_gate(
            entry=100.0,
            stop=97.0,     # Risk: 3.0
            target=110.0,  # Reward: 10.0, RR = 3.33 (good), but outside windows
            direction="long",
            c5_window=c5_window,
            c15_window=c15_window
        )
        
        assert is_valid is False
        assert "Levels outside chosen windows" in reason
    
    def test_short_trade_validation(self, sample_candles):
        """Test safety gate with short trade direction."""
        c5_window, c15_window = sample_candles
        
        # Valid short trade
        # Window range: low=99.0, high=102.0, so all levels must be within this range
        is_valid, reason = _validate_final_safety_gate(
            entry=101.0,
            stop=101.5,   # Risk: 0.5 (within window)
            target=99.5,  # Reward: 1.5, RR = 3.0 (good ratio)
            direction="short",
            c5_window=c5_window,
            c15_window=c15_window,
            anti_scalp_k=1.0  # Very low k to ensure anti-scalp passes
        )
        
        assert is_valid is True, f"Expected valid trade but got: {reason}"
        assert reason == "All safety checks passed"
    
    def test_invalid_direction_fails_safety_gate(self, sample_candles):
        """Test that invalid direction causes safety gate failure."""
        c5_window, c15_window = sample_candles
        
        is_valid, reason = _validate_final_safety_gate(
            entry=100.0,
            stop=99.0,
            target=102.5,
            direction="invalid",
            c5_window=c5_window,
            c15_window=c15_window
        )
        
        assert is_valid is False
        # The error could be in RR calculation due to invalid side conversion
        assert "Invalid side" in reason or "RR calculation failed" in reason
    
    def test_empty_candle_windows_fails_safety_gate(self):
        """Test that empty candle windows cause safety gate failure."""
        is_valid, reason = _validate_final_safety_gate(
            entry=100.0,
            stop=99.0,
            target=102.5,
            direction="long",
            c5_window=[],
            c15_window=[]
        )
        
        assert is_valid is False
        assert "cannot be empty" in reason or "check failed" in reason
