import pytest

from packages.core import (
    Decision,
    DecisionAction,
    normalize_decision,
    parse_prediction,
)


def test_missing_fields_defaults():
    decision = normalize_decision({})
    assert isinstance(decision, Decision)
    assert decision.action is DecisionAction.HOLD
    assert decision.size == 0.0
    assert decision.client_order_id is None


def test_size_full_precision_passthrough():
    # Sizes are NOT rounded here — lot precision is broker/asset-specific and
    # applied at the adapter via volume_step (4-dp rounding broke 5-digit FX).
    decision = normalize_decision({"action": "buy", "size": 0.123456})
    assert decision.size == 0.123456


def test_enum_coercion_and_invalid_value():
    decision = normalize_decision({"action": "buy", "size": 1})
    assert decision.action is DecisionAction.BUY
    with pytest.raises(ValueError):
        normalize_decision({"action": "invalid"})


def test_client_order_id_handling():
    # Test with client_order_id provided
    decision = normalize_decision({"action": "sell", "size": 2.5, "client_order_id": "test_123"})
    assert decision.client_order_id == "test_123"
    
    # Test without client_order_id (should be None)
    decision = normalize_decision({"action": "buy", "size": 1.0})
    assert decision.client_order_id is None


def test_parse_prediction_invalid_order():
    data = {"direction": "long", "level": 100, "stop": 105, "target": 110}
    assert parse_prediction(data) is None


def test_parse_prediction_missing_values():
    assert parse_prediction({}) is None
