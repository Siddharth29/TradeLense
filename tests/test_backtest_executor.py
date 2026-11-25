"""Unit tests for backtest_executor.py."""

import pytest
import pandas as pd
from datetime import datetime, timezone
from typing import Dict, Any, List

from packages.core.backtest_executor import BacktestExecutor
from tests.fixtures.backtest_fixtures import generate_mock_candles


@pytest.fixture
def executor():
    """Create a BacktestExecutor instance."""
    return BacktestExecutor()


@pytest.fixture
def mock_candles():
    """Generate mock 5m candles."""
    start_time = datetime(2024, 1, 1, 9, 15, tzinfo=timezone.utc)
    return generate_mock_candles(start_time, count=100, interval_minutes=5)


@pytest.fixture
def mock_signal():
    """Generate mock trading signal."""
    return {
        "direction": "long",
        "level": 19500.0,
        "entry_price": 19500.0,
        "stop": 19450.0,
        "stoploss": 19450.0,
        "target": 19600.0,
        "pattern": "breakout"
    }


class TestBacktestExecutor:
    """Test BacktestExecutor class."""
    
    def test_initialization(self, executor):
        """Test executor initialization."""
        assert executor is not None
    
    def test_simulate_code_execution_no_code(self, executor, mock_candles):
        """Test code execution with no code provided."""
        result = executor.simulate_code_execution(
            code=None,
            candles_5m=mock_candles,
            start_index=0
        )
        
        assert result["code_executed"] is False
        assert "error" in result
    
    def test_simulate_code_execution_no_candles(self, executor):
        """Test code execution with no candles."""
        result = executor.simulate_code_execution(
            code="print('test')",
            candles_5m=[],
            start_index=0
        )
        
        assert result["code_executed"] is False
        assert "error" in result
    
    def test_simulate_code_execution_insufficient_candles(self, executor):
        """Test code execution with insufficient candles."""
        candles = generate_mock_candles(
            datetime(2024, 1, 1, 9, 15, tzinfo=timezone.utc),
            count=5,
            interval_minutes=5
        )
        
        result = executor.simulate_code_execution(
            code="print('test')",
            candles_5m=candles,
            start_index=10  # Beyond available candles
        )
        
        assert result["code_executed"] is False
        assert "error" in result
        assert "Insufficient candles" in result["error"]
    
    def test_simulate_code_execution_with_valid_code(self, executor, mock_candles):
        """Test code execution with valid monitoring code."""
        code = """
def run(candles_5, now):
    '''Simple trigger check.'''
    # candles_5 is a list of arrays: [time, open, high, low, close, volume]
    # candles_5[-1] is the last candle (current), index 4 is close price
    if len(candles_5) > 0:
        # Handle both array format [time, o, h, l, c, v] and dict format
        last_candle = candles_5[-1]
        if isinstance(last_candle, list):
            current_close = last_candle[4]  # close is at index 4
        else:
            current_close = last_candle.get('close', 0)
        return {
            'direction': 'buy',
            'entry': current_close,
            'stop_loss': current_close - 50,
            'take_profit_1': current_close + 100,
            'take_profit_2': current_close + 150,
            'confidence': 0.8,
            'rationale': 'Test trigger'
        }
    return {
        'direction': 'hold',
        'entry': None,
        'stop_loss': None,
        'take_profit_1': None,
        'confidence': 0.5,
        'rationale': 'No candles'
    }
"""
        
        result = executor.simulate_code_execution(
            code=code,
            candles_5m=mock_candles,
            start_index=50  # Mid-way through candles
        )
        
        assert result["code_executed"] is True
        assert "start_index" in result
        assert result["start_index"] == 50
        assert "historical_candles_count" in result
    
    def test_simulate_code_execution_with_error(self, executor, mock_candles):
        """Test code execution with code that raises an error."""
        code = """
def run(candles_5, now):
    raise ValueError("Test error")
"""
        
        result = executor.simulate_code_execution(
            code=code,
            candles_5m=mock_candles,
            start_index=50
        )
        
        assert result["code_executed"] is True
        assert "error" in result
    
    def test_simulate_trade_outcome_with_signal(self, executor, mock_signal):
        """Test trade outcome simulation with valid signal."""
        # Create future candles DataFrame
        candles = generate_mock_candles(
            datetime(2024, 1, 1, 9, 15, tzinfo=timezone.utc),
            count=12,
            interval_minutes=5,
            base_price=19500.0
        )
        
        future_df = pd.DataFrame(candles)
        if 'time' in future_df.columns:
            future_df['time'] = pd.to_datetime(future_df['time'])
            future_df = future_df.set_index('time')
        
        outcome = executor.simulate_trade_outcome(mock_signal, future_df)
        
        assert outcome is not None
        assert "result" in outcome
        assert "signal_data" in outcome
    
    def test_simulate_trade_outcome_with_short_signal(self, executor):
        """Test trade outcome simulation with short signal."""
        short_signal = {
            "direction": "short",
            "level": 19500.0,
            "stop": 19550.0,
            "target": 19400.0,
            "pattern": "reversal"
        }
        
        candles = generate_mock_candles(
            datetime(2024, 1, 1, 9, 15, tzinfo=timezone.utc),
            count=12,
            interval_minutes=5,
            base_price=19500.0
        )
        
        future_df = pd.DataFrame(candles)
        if 'time' in future_df.columns:
            future_df['time'] = pd.to_datetime(future_df['time'])
            future_df = future_df.set_index('time')
        
        outcome = executor.simulate_trade_outcome(short_signal, future_df)
        
        assert outcome is not None
        assert "result" in outcome
        assert "direction" in outcome
    
    def test_simulate_trade_outcome_empty_future_candles(self, executor, mock_signal):
        """Test trade outcome with empty future candles."""
        future_df = pd.DataFrame()
        
        outcome = executor.simulate_trade_outcome(mock_signal, future_df)
        
        assert outcome is not None
        # Should handle gracefully even with empty data
    
    def test_simulate_trade_outcome_with_invalid_signal(self, executor):
        """Test trade outcome with invalid signal."""
        invalid_signal = {
            "direction": None,
            "level": None,
            "stop": None,
            "target": None
        }
        
        candles = generate_mock_candles(
            datetime(2024, 1, 1, 9, 15, tzinfo=timezone.utc),
            count=12,
            interval_minutes=5
        )
        
        future_df = pd.DataFrame(candles)
        if 'time' in future_df.columns:
            future_df['time'] = pd.to_datetime(future_df['time'])
            future_df = future_df.set_index('time')
        
        outcome = executor.simulate_trade_outcome(invalid_signal, future_df)
        
        assert outcome is not None
        # Should handle gracefully

