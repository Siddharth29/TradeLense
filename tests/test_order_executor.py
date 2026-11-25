import pytest

from packages.core.models import Decision, Side, PriceType, Order
from packages.core.order_executor import place_with_retries, TransientError


@pytest.fixture(autouse=True)
def patch_sniper():
    """Override global patch_sniper fixture to avoid heavy imports."""
    return


class FlakyBroker:
    """Broker that fails a number of times before succeeding."""

    def __init__(self, fail_times=0):
        self.fail_times = fail_times
        self.calls = 0
        self.orders = {}
        self.id_seq = 0

    def place(self, decision):
        cid = decision.client_order_id
        if cid in self.orders:
            self.calls += 1
            return self.orders[cid]
        self.calls += 1
        if self.calls <= self.fail_times:
            raise TransientError("temporary")
        self.id_seq += 1
        order = Order(id=str(self.id_seq), decision=decision, status="filled", client_order_id=cid)
        self.orders[cid] = order
        return order


@pytest.mark.parametrize("price_type", [PriceType.MARKET, PriceType.LIMIT, "stop"])
def test_place_with_retries_transient_then_success(price_type):
    broker = FlakyBroker(fail_times=1)
    decision = Decision(
        asset="equity",
        symbol="AAPL",
        side=Side.BUY,
        price_type=price_type,
        entry=100.0,
        quantity=1,
        client_order_id=None,
    )

    order = place_with_retries(broker, decision)
    assert order.id == "1"
    assert broker.calls == 2
    assert len(broker.orders) == 1

    order2 = place_with_retries(broker, decision)
    assert order2.id == order.id
    assert len(broker.orders) == 1


def test_place_with_retries_retry_cap():
    broker = FlakyBroker(fail_times=5)
    decision = Decision(
        asset="equity",
        symbol="AAPL",
        side=Side.BUY,
        price_type=PriceType.MARKET,
        entry=100.0,
        quantity=1,
        client_order_id=None,
    )

    with pytest.raises(TransientError):
        place_with_retries(broker, decision, max_retries=2)
    assert broker.calls == 3
    assert len(broker.orders) == 0
