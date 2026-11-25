"""Tests for the simple_sandbox run_in_sandbox interface.

Uses simple_sandbox (the production sandbox) which returns plain dicts.
"""
import pytest
from packages.core.simple_sandbox import run_in_sandbox


def test_successful_run_returns_dict():
    source = """
def run(candles_5, now):
    return {
        'direction': 'hold',
        'entry': 0.0,
        'stop_loss': 0.0,
        'take_profit_1': 0.0,
        'take_profit_2': 0.0,
        'confidence': 0.5,
        'rationale': 'test'
    }
"""
    result = run_in_sandbox(source, candles_5=[], now='2023-01-01T00:00:00')
    assert isinstance(result, dict)
    assert result['direction'] == 'hold'
    assert result['confidence'] == 0.5


def test_successful_buy_signal():
    source = """
def run(candles_5, now):
    return {
        'direction': 'buy',
        'entry': 100.0,
        'stop_loss': 95.0,
        'take_profit_1': 110.0,
        'take_profit_2': 120.0,
        'confidence': 0.8,
        'rationale': 'test buy'
    }
"""
    result = run_in_sandbox(source, candles_5=[], now='2023-01-01T00:00:00')
    assert result['direction'] == 'buy'
    assert result['entry'] == 100.0
    assert result['stop_loss'] == 95.0


def test_timeout_raises_exception():
    source = """
def run(candles_5, now):
    while True:
        pass
"""
    with pytest.raises((TimeoutError, RuntimeError)):
        run_in_sandbox(source, candles_5=[], now='now')


def test_file_system_access_blocked():
    source = """
def run(candles_5, now):
    open('some_file.txt', 'w')
"""
    with pytest.raises((PermissionError, RuntimeError)):
        run_in_sandbox(source, candles_5=[], now='now')


def test_network_access_blocked():
    source = """
def run(candles_5, now):
    __import__('socket').socket()
"""
    with pytest.raises((PermissionError, RuntimeError)):
        run_in_sandbox(source, candles_5=[], now='now')


def test_candles_15m_is_defined():
    """candles_15m must be available in the sandbox namespace (as empty list)."""
    source = """
def run(candles_5, now):
    _ = len(candles_15m)
    return {
        "direction": "hold",
        "entry": 0.0,
        "stop_loss": 0.0,
        "take_profit_1": 0.0,
        "take_profit_2": 0.0,
        "confidence": 0.0,
        "rationale": "candles_15m available"
    }
"""
    result = run_in_sandbox(source, candles_5=[], now="2025-01-01T09:15:00")
    assert result["direction"] == "hold"


def test_candles_array_format_accessible():
    """Candles in array format [time,o,h,l,c,vol] should be accessible."""
    candles = [['2025-01-01T09:00', 100.0, 105.0, 99.0, 102.0, 1000.0]]
    source = """
def run(candles_5, now):
    c = candles_5[-1]
    close = c[4]
    return {
        'direction': 'hold',
        'entry': close,
        'stop_loss': 0.0,
        'take_profit_1': 0.0,
        'take_profit_2': 0.0,
        'confidence': 0.5,
        'rationale': 'array format test'
    }
"""
    result = run_in_sandbox(source, candles_5=candles, now='2025-01-01T09:05:00')
    assert result['direction'] == 'hold'
    assert result['entry'] == 102.0


def test_now_is_accessible_as_string():
    """now must be accessible and usable as a string."""
    source = """
def run(candles_5, now):
    ts = str(now)
    return {
        'direction': 'hold',
        'entry': 0.0,
        'stop_loss': 0.0,
        'take_profit_1': 0.0,
        'take_profit_2': 0.0,
        'confidence': 0.0,
        'rationale': ts
    }
"""
    result = run_in_sandbox(source, candles_5=[], now='2025-06-01T10:30:00')
    assert '2025' in result['rationale']
