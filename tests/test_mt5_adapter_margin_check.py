import pytest
from unittest.mock import MagicMock

from packages.adapters.mt5_adapter import MT5Adapter
from packages.core.models import Decision, Side, PriceType


class DummyMT5:
    TRADE_ACTION_DEAL = 1
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    TRADE_ACTION_PENDING = 5
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ORDER_TIME_GTC = 0
    ORDER_FILLING_IOC = 0

    def __init__(self):
        self.order_send = MagicMock()


def test_place_raises_when_margin_insufficient():
    adapter = MT5Adapter()
    adapter._mt5 = DummyMT5()
    adapter.balances = lambda: {"cash": 50.0}

    decision = Decision(
        asset="FOREX",
        symbol="XAUUSD",
        side=Side.BUY,
        price_type=PriceType.MARKET,
        entry=100.0,
        quantity=1.0,
    )

    with pytest.raises(ValueError):
        adapter.place(decision)

    assert adapter._mt5.order_send.called is False
