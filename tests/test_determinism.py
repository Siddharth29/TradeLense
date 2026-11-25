import pytest

from packages.core.strategy_loader import run_strategy_code


def test_strategy_deterministic_results():
    source = """
import random

def decide(candles_5, now):
    return {'action': 'buy', 'size': random.random()}
"""
    res1 = run_strategy_code(source, [], "now")
    res2 = run_strategy_code(source, [], "now")
    assert res1.size == res2.size


def test_unseeded_random_call_raises_error():
    source = """
import random

def decide(candles_5, now):
    random.seed()
    return {'action': 'buy', 'size': random.random()}
"""
    with pytest.raises(RuntimeError):
        run_strategy_code(source, [], "now")


def test_non_whitelisted_module_rejected():
    # "datetime" is deliberately whitelisted (strategies receive `now` but may
    # format timestamps); modules outside the whitelist must still be blocked.
    source = """
import time

def decide(candles_5, now):
    time.time()
    return {'action': 'buy', 'size': 1}
"""
    with pytest.raises(ImportError):
        run_strategy_code(source, [], "now")
