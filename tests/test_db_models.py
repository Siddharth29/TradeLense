from datetime import datetime, timezone
import pytest
from pydantic import ValidationError

from packages.db.models import (
    TradeDoc,
    OrderDoc,
    FillDoc,
    SignalDoc,
    CandleDoc,
    DailyMetricDoc,
)

MODELS = [
    TradeDoc,
    OrderDoc,
    FillDoc,
    SignalDoc,
    CandleDoc,
    DailyMetricDoc,
]


def _valid_data(model):
    base = {
        "version": 1,
        "timestamp": datetime.now(timezone.utc),
        "client_order_id": "cid",
        "checksum": "chk",
    }
    if model is TradeDoc:
        base.update({
            "symbol": "AAPL",
            "quantity": 1.0,
            "price": 10.0,
            "agent_id": "agent",
            "prompt_hash": "ph",
        })
    elif model is OrderDoc:
        base.update({"symbol": "AAPL", "side": "buy", "quantity": 1.0, "status": "open"})
    elif model is FillDoc:
        base.update({"order_id": "1", "quantity": 1.0, "price": 10.0})
    elif model is SignalDoc:
        base.update({"symbol": "AAPL", "side": "buy"})
    elif model is CandleDoc:
        base.update({
            "symbol": "AAPL",
            "open": 1.0,
            "high": 2.0,
            "low": 0.5,
            "close": 1.5,
            "volume": 100.0,
        })
    elif model is DailyMetricDoc:
        base.update({"metric": "pnl", "value": 1.23})
    return base


@pytest.mark.parametrize("model", MODELS)
def test_model_creation(model):
    data = _valid_data(model)
    doc = model.model_validate(data)
    assert doc.version == 1


@pytest.mark.parametrize("model", MODELS)
def test_unknown_field_rejected(model):
    data = _valid_data(model)
    data["unknown"] = 1
    with pytest.raises(ValidationError):
        model.model_validate(data)


def test_timestamp_must_be_utc():
    data = _valid_data(TradeDoc)
    data["timestamp"] = datetime.now()
    with pytest.raises(ValidationError):
        TradeDoc.model_validate(data)
