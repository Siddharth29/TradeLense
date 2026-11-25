from packages.adapters.mt5_adapter import MT5Adapter
from packages.core.models import Decision, Side, PriceType


def test_order_margin_returns_premium():
    adapter = MT5Adapter()
    decision = Decision(
        asset="FOREX",
        symbol="XAUUSD",
        side=Side.BUY,
        price_type=PriceType.MARKET,
        entry=100.0,
        quantity=2.0,
    )
    assert adapter.order_margin(decision) == 200.0
