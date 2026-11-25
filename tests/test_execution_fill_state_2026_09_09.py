"""A live position must be managed from its fill, not its requested quote."""

from datetime import datetime, timezone
import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from packages.core.models import Decision, Fill, Order, PriceType, Side
from packages.core.schemas_trade import CodeSniperSignal
from packages.core.services import code_sniper_monitor as csm


def test_confirmed_fill_fallback_uses_weighted_price_and_filled_volume():
    fills = [
        Fill("1", 0.01, 102.0, datetime.now(timezone.utc)),
        Fill("1", 0.02, 105.0, datetime.now(timezone.utc)),
    ]
    price, volume = csm._confirmed_execution(SimpleNamespace(), fills)
    assert price == pytest.approx(104.0)
    assert volume == pytest.approx(0.03)


def test_requested_volume_is_never_invented_from_missing_confirmation():
    with pytest.raises(ValueError, match="reconciliation required"):
        csm._confirmed_execution(SimpleNamespace(average_price=102.0, filled_quantity=0.0), [])


@pytest.mark.asyncio
@pytest.mark.parametrize("reconcile", [False, True])
async def test_queued_monitor_uses_actual_entry_size_and_initial_risk(monkeypatch, reconcile):
    monkeypatch.setattr(csm, "account_trading_halted", AsyncMock(return_value=(False, "")))
    monkeypatch.setattr(csm, "trading_throttled", AsyncMock(return_value=(False, "")))
    monkeypatch.setattr(csm, "news_blackout_active", lambda *_: (False, ""))

    decision = Decision(
        asset="BTCUSD", symbol="BTCUSDm", side=Side.BUY,
        price_type=PriceType.MARKET, entry=100.0, quantity=0.10,
        sl=90.0, tp1=115.0, tp=130.0,
    )
    order = Order("123", decision, "filled", filled_quantity=0.07, average_price=102.0)
    if reconcile:
        order.filled_quantity = 0.0
        order.average_price = 0.0
    broker = SimpleNamespace(
        place=Mock(return_value=(order, [])),
        # First check is pre-order, second is the exact-ticket recovery read.
        positions_raw=Mock(side_effect=[[], [{"ticket": 123, "price_open": 102.0, "volume": 0.07}]]),
    )
    trade_monitor = SimpleNamespace(queue_trade=Mock())
    monitor = csm.CodeSniperMonitor(None, broker, None, trade_monitor=trade_monitor)
    signal = CodeSniperSignal(
        code='def run(candles_5, now):\n    return {"direction": "hold"}',
        input_schema={}, output_schema={}, metadata={}, ttl_minutes=90, confidence=0.8,
    )
    monitor.add_strategy("fill", signal, asset="BTCUSD", symbol="BTCUSDm", context={})

    await monitor._execute_trade(monitor._strategies["fill"], decision)

    queued = trade_monitor.queue_trade.call_args.kwargs
    assert queued["trade_state"]["entry"] == 102.0
    assert queued["trade_state"]["quantity"] == 0.07
    assert order.average_price == 102.0
    assert order.filled_quantity == 0.07
    assert queued["trade_state"]["initial_stop"] == 90.0
    from packages.core.config import RUNNER_TRAIL_FALLBACK_RISK_MULT
    assert queued["rules"]["runner_trail_dist"] == pytest.approx(12.0 * RUNNER_TRAIL_FALLBACK_RISK_MULT)
    assert decision.entry == 100.0  # Preserve proposal for fill-vs-plan attribution.
    assert decision.quantity == 0.10


@pytest.mark.asyncio
@pytest.mark.parametrize("read_failure", [None, RuntimeError("IPC unavailable")])
async def test_failed_position_query_does_not_discard_live_monitor(monkeypatch, read_failure):
    from unittest.mock import MagicMock
    from packages.adapters.mt5_adapter import MT5Adapter
    from packages.core.services import trade_monitor as tm

    broker = MagicMock(spec=MT5Adapter)
    broker._mt5 = SimpleNamespace(positions_get=Mock(side_effect=[read_failure]))
    broker._normalize_symbol.return_value = "BTCUSDm"
    monitor = tm.TradeMonitor.__new__(tm.TradeMonitor)
    monitor.broker = broker
    monitor.candle_service = broker
    monitor._shutdown = False
    monitor.journal = None
    monitor.on_trade_closed = Mock()

    bar = {"time": datetime.now(timezone.utc), "close": 103.0, "high": 104.0, "low": 101.0}
    wait = AsyncMock(side_effect=[bar, asyncio.CancelledError()])
    monkeypatch.setattr(tm, "wait_for_bar", wait)
    monkeypatch.setattr(tm, "_is_market_closing_soon", lambda *_a, **_k: (False, ""))
    monkeypatch.setattr(tm, "_is_market_closed", lambda *_a, **_k: (False, 0))
    monkeypatch.setattr(tm, "upsert_monitor_state", AsyncMock())
    clear = AsyncMock()
    monkeypatch.setattr(tm, "clear_monitor_state", clear)
    item = {
        "asset": "BTCUSD", "position_ticket": "123",
        "trade_state": {"direction": "long", "entry": 100.0, "sl": 90.0, "quantity": 0.1},
        "rules": {"tp1": 112.0, "tp": 120.0},
        "expiry": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    with pytest.raises(asyncio.CancelledError):
        await monitor._monitor(item)
    assert wait.await_count == 2
    clear.assert_not_awaited()
    monitor.on_trade_closed.assert_not_called()
    broker.get_last_exit_deal.assert_not_called()
    broker._mt5.positions_get.assert_called_once_with(ticket=123)


@pytest.mark.parametrize("filled_quantity, fills", [
    (.1, [Fill("123", .04, 100., datetime.now(timezone.utc))]),
    (0., [Fill("wrong-order", .04, 100., datetime.now(timezone.utc))]),
    (0., [Fill("123", .04, 100., datetime.now(timezone.utc)), Fill("123", .01, 0., datetime.now(timezone.utc))]),
])
def test_incomplete_or_mismatched_receipts_require_reconciliation(filled_quantity, fills):
    with pytest.raises(ValueError, match="reconciliation required"):
        csm._confirmed_execution(SimpleNamespace(id="123", average_price=0., filled_quantity=filled_quantity), fills)
