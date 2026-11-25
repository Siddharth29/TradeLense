from datetime import datetime, timezone
import pytest
from pydantic import ValidationError

from packages.core.schemas import DecisionSchemaV1
from packages.core.models import Decision, Side, PriceType


def test_schema_to_decision_roundtrip():
    data = {
        "version": 1,
        "asset": "equity",
        "symbol": "AAPL",
        "side": Side.BUY,
        "price_type": PriceType.MARKET,
        "entry": 100.0,
        "quantity": 10,
        "sl": 90.0,
        "tp1": 110.0,
        "tp": 120.0,
        "valid_until": datetime.now(timezone.utc),
        "notes": "test",
        "client_order_id": "abc",
    }
    schema = DecisionSchemaV1.model_validate(data)
    decision = schema.to_decision()
    assert isinstance(decision, Decision)
    assert decision.symbol == "AAPL"
    assert decision.entry == 100.0


def test_schema_strict_mode():
    with pytest.raises(ValidationError):
        DecisionSchemaV1.model_validate({
            "version": 1,
            "asset": "eq",
            "symbol": "AAPL",
            "side": "buy",
            "price_type": "market",
            "entry": 1.0,
        })
