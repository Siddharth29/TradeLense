import pytest

from packages.core import Decision, DecisionAction, run_strategy_code
from packages.core import strategy_loader


def test_compile_only_once():
    source = """
from math import sqrt

def decide(candles_5, now):
    return {"action": "BUY", "size": 1}
"""
    strategy_loader.compile_counter = 0
    run_strategy_code(source, [], "now")
    run_strategy_code(source, [], "now")
    assert strategy_loader.compile_counter == 1


def test_run_strategy_code_happy_path():
    source = """
def decide(candles_5, now):
    return {"action": "BUY", "size": 1}
"""
    decision = run_strategy_code(source, [], "now")
    assert isinstance(decision, Decision)
    assert decision.action is DecisionAction.BUY
    assert decision.size == 1


def test_run_strategy_code_missing_decide():
    source = """
# no decide here
"""
    with pytest.raises(AttributeError):
        run_strategy_code(source, [], "now")
