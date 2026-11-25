from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from packages.core.services import RiskService
from packages.core.services import risk as risk_module
from packages.core.risk import CONTRACT_SPECS, calc_risk_reward


def test_calc_quantity_uses_supplied_balance():
    broker = SimpleNamespace()
    service = RiskService(broker)
    service.update_balances({"cash": 1000})

    qty = service.calc_quantity(
        risk=0.1,
        entry=100,
        stop=90,
        tick_size=1,
        lot_size=1,
        min_qty=1,
        max_qty=100,
        risk_is_pct=True,
    )

    assert qty == 10


def test_calc_risk_reward():
    ratio = calc_risk_reward(entry=100, stop=90, take_profit=120)
    assert ratio == 2.0


def test_reward_to_risk_enforced():
    broker = SimpleNamespace()
    service = RiskService(broker, min_reward_to_risk=2.0)
    service.update_balances({"cash": 1000})

    # acceptable ratio: (120-100)/(100-90) = 2
    service.calc_quantity(
        risk=0.1,
        entry=100,
        stop=90,
        take_profit=120,
        tick_size=1,
        lot_size=1,
        min_qty=1,
        max_qty=100,
        risk_is_pct=True,
    )

    # insufficient ratio: (105-100)/(100-90) = 0.5
    with pytest.raises(ValueError):
        service.calc_quantity(
            risk=0.1,
            entry=100,
            stop=90,
            take_profit=105,
            tick_size=1,
            lot_size=1,
            min_qty=1,
            max_qty=100,
            risk_is_pct=True,
        )


def test_calc_lot_size_by_risk_uses_supplied_balance():
    broker = SimpleNamespace()
    service = RiskService(broker, currency="USD")
    service.update_balances({"USD": 1000})

    assert "BTCUSD" in CONTRACT_SPECS

    lot = service.calc_lot_size_by_risk(
        risk_pct=0.1,
        sl_points=50,
        symbol="BTCUSD",
        entry_price=1.0,
    )

    assert lot == 2.0


class DummyResponse:
    def __init__(self, data):
        self._data = data

    def json(self):  # pragma: no cover - simple stub
        return self._data

    def raise_for_status(self):  # pragma: no cover - simple stub
        pass


class DummyClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    async def get(self, url, headers=None, **kwargs):
        self.calls += 1
        return DummyResponse(self.payload)


class _BaseBroker:
    def __init__(self):
        self.order_margin = MagicMock(return_value=50)


CashMarginBroker = type("CashMarginBroker", (_BaseBroker,), {})
MT5Broker = type("MT5Broker", (_BaseBroker,), {})


@pytest.mark.parametrize("broker_cls", [CashMarginBroker, MT5Broker])
def test_ensure_margin_compares_balance(broker_cls, monkeypatch):
    decision = object()
    broker = broker_cls()
    service = RiskService(broker)
    service.update_balances({"cash": 100})

    fake_time = 1000.0

    def monotonic():
        return fake_time

    monkeypatch.setattr(risk_module, "time", SimpleNamespace(monotonic=monotonic))

    assert service.ensure_margin(decision) is True
    broker.order_margin.assert_called_once_with(decision)

    broker.order_margin.return_value = 150
    fake_time += 16  # expire cache
    assert service.ensure_margin(decision) is False


def test_ensure_margin_caches_results(monkeypatch):
    decision = object()
    broker = CashMarginBroker()
    service = RiskService(broker)
    service.update_balances({"cash": 100})

    fake_time = 1000.0

    def monotonic():
        return fake_time

    monkeypatch.setattr(risk_module, "time", SimpleNamespace(monotonic=monotonic))

    assert service.ensure_margin(decision) is True
    assert service.ensure_margin(decision) is True
    broker.order_margin.assert_called_once()

    fake_time += 16  # expire cache
    assert service.ensure_margin(decision) is True
    assert broker.order_margin.call_count == 2
