import pytest

from packages.core.strategy_loader import run_strategy_code
from packages.core.decision_normalizer import DecisionAction


def _make_source(import_line: str = "") -> str:
    return f"""{import_line}

def decide(candles_5, now):
    return {{'action': 'BUY', 'size': 1}}
"""


def test_allowed_import_math_passes():
    source = _make_source("import math")
    decision = run_strategy_code(source, [], "now")
    assert decision.action is DecisionAction.BUY


def test_disallowed_import_os_raises_error():
    source = _make_source("import os")
    with pytest.raises(ImportError):
        run_strategy_code(source, [], "now")


def test_no_imports_pass_silently():
    source = _make_source("")
    decision = run_strategy_code(source, [], "now")
    assert decision.action is DecisionAction.BUY
