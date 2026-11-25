from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import pytest

from packages.core.services.code_sniper_monitor import CodeSniperMonitor
from packages.core.schemas_trade import CodeSniperSignal


class DummyCandleService:
    def __init__(self):
        self._now = datetime.now(timezone.utc)

    async def fetch_candles(self, tf: str, limit: int):
        assert tf == "5m"
        base = self._now

        class C:
            def __init__(self, t):
                self.time = t
                self.open = 100.0
                self.high = 101.0
                self.low = 99.5
                self.close = 100.5
                self.volume = 100

        return [C(base - timedelta(minutes=5 * (limit - i - 1))) for i in range(limit)]


class DummyRisk:
    def available_funds(self) -> float:
        return 100000.0


class DummyBroker:
    def __init__(self):
        self.placed = []

    def place(self, decision):
        self.placed.append(decision)

        class Order:
            id = "1"
            status = "filled"
            average_price = decision.entry

        return Order()


@pytest.mark.asyncio
async def test_monitor_add_and_expire_strategy():
    candle_service = DummyCandleService()
    broker = DummyBroker()
    risk = DummyRisk()

    monitor = CodeSniperMonitor(candle_service, broker, risk)

    code = """
def run(candles_5, now):
    return {
        "direction": "hold",
        "entry": 0,
        "stop_loss": 0,
        "take_profit_1": 0,
        "take_profit_2": 0,
        "confidence": 0.1,
        "rationale": "waiting"
    }
"""
    signal = CodeSniperSignal(
        code=code,
        input_schema={},
        output_schema={},
        metadata={},
        ttl_minutes=90,
        confidence=0.1,
    )

    strategy_id = "s1"
    monitor.add_strategy(strategy_id, signal, asset="TEST", symbol="TEST", context={})

    # Strategy should be in active list
    assert strategy_id in monitor.get_active_strategies()

    # Force expire by manipulating the Strategy object's expiry
    monitor._strategies[strategy_id].expiry = datetime.now(timezone.utc) - timedelta(seconds=1)

    # After expiry, should_execute returns False and is_expired returns True
    assert monitor._strategies[strategy_id].is_expired

    # Remove expired strategy (simulates what _run_all does)
    monitor.remove_strategy(strategy_id)
    assert strategy_id not in monitor.get_active_strategies()


@pytest.mark.asyncio
async def test_monitor_no_trade_on_hold():
    """Verify that hold signals don't trigger broker placement."""
    candle_service = DummyCandleService()
    broker = DummyBroker()
    risk = DummyRisk()

    monitor = CodeSniperMonitor(candle_service, broker, risk)

    signal = CodeSniperSignal(
        code='def run(candles_5, now):\n    return {"direction": "hold", "entry": 0, "stop_loss": 0, "take_profit_1": 0, "take_profit_2": 0, "confidence": 0.1, "rationale": "waiting"}',
        input_schema={},
        output_schema={},
        metadata={},
        ttl_minutes=90,
        confidence=0.1,
    )

    monitor.add_strategy("s2", signal, asset="TEST", symbol="TEST", context={})
    assert monitor.active_count == 1
    assert len(broker.placed) == 0


class BrokerWithPositions:
    """Broker exposing positions() so the single-position guard can run."""

    def __init__(self, open_positions):
        self._positions = open_positions
        self.placed = []

    def positions(self):
        return list(self._positions)

    def place(self, decision):
        self.placed.append(decision)

        class Order:
            id = "1"
            status = "filled"
            average_price = decision.entry

        return Order(), []


def _hold_signal():
    return CodeSniperSignal(
        code='def run(candles_5, now):\n    return {"direction": "hold", "entry": 0, "stop_loss": 0, "take_profit_1": 0, "take_profit_2": 0, "confidence": 0.1, "rationale": "x"}',
        input_schema={},
        output_schema={},
        metadata={},
        ttl_minutes=90,
        confidence=0.1,
    )


def _btc_decision(side):
    from packages.core.models import Decision, PriceType

    return Decision(
        asset="BTCUSD",
        symbol="BTCUSDm",
        side=side,
        price_type=PriceType.MARKET,
        entry=66985.0,
        quantity=0.01,
        sl=67200.0,
        tp1=66800.0,
        tp=66500.0,
    )


@pytest.mark.asyncio
async def test_execute_trade_skips_when_broker_position_already_open():
    """Regression: a fresh strategy/runner must NOT open an opposite position
    while one is already live at the broker (suffix-safe BTCUSDm -> BTCUSD)."""
    from packages.core.models import Side, Position

    # Broker already holds a long BTCUSDm position.
    broker = BrokerWithPositions([Position(symbol="BTCUSDm", quantity=0.01, average_price=66930.0)])
    monitor = CodeSniperMonitor(DummyCandleService(), broker, DummyRisk())

    monitor.add_strategy("s_guard", _hold_signal(), asset="BTCUSD", symbol="BTCUSDm", context={})
    strat = monitor._strategies["s_guard"]

    # Strategy flips to the opposite (sell) side — must be blocked.
    await monitor._execute_trade(strat, _btc_decision(Side.SELL))

    assert len(broker.placed) == 0, "guard must block a second/opposite entry"
    # Strategy stays registered so it can act once the position closes.
    assert "s_guard" in monitor._strategies


@pytest.mark.asyncio
async def test_execute_trade_places_when_no_broker_position(monkeypatch):
    """Counterpart: with no open position the entry goes through as normal."""
    from packages.core.models import Side
    from unittest.mock import AsyncMock
    monkeypatch.setattr("packages.core.services.code_sniper_monitor.account_trading_halted", AsyncMock(return_value=(False, "")))
    monkeypatch.setattr("packages.core.services.code_sniper_monitor.trading_throttled", AsyncMock(return_value=(False, "")))
    monkeypatch.setattr("packages.core.services.code_sniper_monitor.news_blackout_active", lambda *_: (False, ""))

    broker = BrokerWithPositions([])  # flat
    monitor = CodeSniperMonitor(DummyCandleService(), broker, DummyRisk())

    monitor.add_strategy("s_ok", _hold_signal(), asset="BTCUSD", symbol="BTCUSDm", context={})
    strat = monitor._strategies["s_ok"]

    await monitor._execute_trade(strat, _btc_decision(Side.BUY))

    assert len(broker.placed) == 1, "entry must proceed when broker is flat"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, RuntimeError("positions unavailable")])
async def test_unknown_pretrade_positions_defer_entry(monkeypatch, failure):
    from unittest.mock import Mock
    from packages.core.models import Side
    broker = BrokerWithPositions([])
    broker.positions = Mock(side_effect=[failure])
    monitor = CodeSniperMonitor(DummyCandleService(), broker, DummyRisk())
    monitor.add_strategy("pending", _hold_signal(), asset="BTCUSD", symbol="BTCUSDm", context={})
    await monitor._execute_trade(monitor._strategies["pending"], _btc_decision(Side.SELL))
    assert broker.placed == []
    assert "pending" in monitor._strategies


@pytest.mark.asyncio
async def test_uncertain_send_disarms_strategy_instead_of_repeating_next_bar(monkeypatch):
    from unittest.mock import Mock, AsyncMock
    from packages.core.models import Side
    from packages.adapters.mt5_adapter import OrderReconciliationRequired
    monkeypatch.setattr("packages.core.services.code_sniper_monitor.account_trading_halted", AsyncMock(return_value=(False, "")))
    monkeypatch.setattr("packages.core.services.code_sniper_monitor.trading_throttled", AsyncMock(return_value=(False, "")))
    monkeypatch.setattr("packages.core.services.code_sniper_monitor.news_blackout_active", lambda *_: (False, ""))
    broker = BrokerWithPositions([])
    broker.place = Mock(side_effect=OrderReconciliationRequired("unknown outcome"))
    monitor = CodeSniperMonitor(DummyCandleService(), broker, DummyRisk())
    monitor.add_strategy("uncertain", _hold_signal(), asset="BTCUSD", symbol="BTCUSDm", context={})
    with pytest.raises(OrderReconciliationRequired):
        await monitor._execute_trade(monitor._strategies["uncertain"], _btc_decision(Side.SELL))
    assert "uncertain" not in monitor._strategies
    broker.place.assert_called_once()
