import pandas as pd

from packages.core.services.risk import RiskService
from packages.core.models import Decision, Side, PriceType, Order
from packages.core.order_executor import place_with_retries
from core.trigger_check import entry_trigger_met


class DummyBroker:
    """Broker with fixed margin requirement and order placement tracking."""

    def __init__(self, required_margin: float):
        self.required_margin = required_margin
        self.place_calls = 0

    def order_margin(self, decision):
        return self.required_margin

    def place(self, decision):
        self.place_calls += 1
        return Order(id="1", decision=decision, status="filled")


def _make_df(vals):
    base = pd.Timestamp("2024-01-01 09:20")
    rows = [
        {"open": o, "high": h, "low": l, "close": c}
        for o, h, l, c in vals
    ]
    index = [base + pd.Timedelta(minutes=5 * i) for i in range(len(rows))]
    return pd.DataFrame(rows, index=index)


def maybe_execute(balance: float):
    broker = DummyBroker(required_margin=50)
    service = RiskService(broker)
    service.update_balances({"cash": balance})

    fut = _make_df([(0, 5, -1, 3)])
    prediction = {"direction": "long", "condition": "breakout", "level": 1}
    decision = Decision(
        asset="equity",
        symbol="AAPL",
        side=Side.BUY,
        price_type=PriceType.MARKET,
        entry=100.0,
        quantity=1.0,
    )

    order = None
    if service.ensure_margin(decision) and entry_trigger_met(prediction, fut):
        order = place_with_retries(broker, decision)
    return broker.place_calls, order


def test_order_only_when_margin_and_trigger_met():
    calls, order = maybe_execute(balance=100)
    assert calls == 1
    assert order is not None

    calls, order = maybe_execute(balance=10)
    assert calls == 0
    assert order is None
