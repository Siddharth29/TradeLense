"""Production failure-injection checks: no terminal, orders or external services."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import asyncio

import pytest

from packages.adapters.mt5_adapter import MT5Adapter
from packages.core.config import MT5_MAGIC_NUMBER
from packages.core.operational_ledger import OperationalLedger
import packages.core.services.trade_monitor as tm


@pytest.fixture
def recovery(monkeypatch):
    broker = MT5Adapter.__new__(MT5Adapter)
    broker._normalize_symbol = lambda s: s
    broker._mt5 = SimpleNamespace(positions_get=MagicMock(return_value=()))
    broker.get_last_exit_deal = MagicMock(return_value=None)
    monitor = tm.TradeMonitor.__new__(tm.TradeMonitor)
    monitor.broker, monitor._owner_asset = broker, "EURUSD"
    monitor.queue_trade = MagicMock()
    states, clear = AsyncMock(return_value=[]), AsyncMock()
    monkeypatch.setattr(tm, "get_open_monitor_states", states)
    monkeypatch.setattr(tm, "clear_monitor_state", clear)
    monkeypatch.setattr(tm, "MONITOR_RECOVERY_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(tm, "MONITOR_RECOVERY_RETRY_SECONDS", 0)
    tm._RECOVERED_ASSETS.discard("EURUSD")
    yield monitor, broker, states, clear
    tm._RECOVERED_ASSETS.discard("EURUSD")


def position(ticket=222, identifier=222):
    return dict(ticket=ticket, identifier=identifier, magic=MT5_MAGIC_NUMBER,
                symbol="EURUSD", volume=.04, type=0, price_open=1.10, sl=1.105)


def checkpoint(ticket="111"):
    return {"asset": "EURUSD", "symbol": "EURUSD", "position_ticket": ticket,
            "trade_id": "old-trade", "trade_state": {"direction": "long", "entry": 1.10,
            "sl": 1.09, "initial_stop": 1.09, "quantity": .10},
            "rules": {"tp1": 1.12, "tp": 1.14, "runner_enabled": True}}


@pytest.mark.asyncio
async def test_failed_position_snapshot_never_clears_or_reads_checkpoint(recovery):
    monitor, broker, states, clear = recovery
    broker._mt5.positions_get.return_value = None
    await monitor._recover_open_trades()
    assert broker._mt5.positions_get.call_count == 2
    states.assert_not_awaited()
    clear.assert_not_awaited()
    monitor.queue_trade.assert_not_called()
    assert "EURUSD" not in tm._RECOVERED_ASSETS


@pytest.mark.asyncio
async def test_transient_position_failure_retries_before_recovery(recovery):
    monitor, broker, states, clear = recovery
    broker._mt5.positions_get.side_effect = [None, [position()]]
    await monitor._recover_open_trades()
    assert broker._mt5.positions_get.call_count == 2
    assert monitor.queue_trade.call_args.kwargs["position_ticket"] == "222"
    clear.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_rules_never_attach_to_unrelated_same_symbol_position(recovery):
    monitor, broker, states, clear = recovery
    states.return_value = [checkpoint()]
    broker._mt5.positions_get.return_value = [position()]
    await monitor._recover_open_trades()
    queued = monitor.queue_trade.call_args.kwargs
    assert queued["position_ticket"] == "222" and queued["rules"]["tp1"] is None
    assert queued.get("trade_id") != "old-trade"
    clear.assert_not_awaited()  # Unresolved old parent remains available.


@pytest.mark.asyncio
async def test_stable_identifier_reticket_restores_actual_quantity_and_stop(recovery):
    monitor, broker, states, clear = recovery
    states.return_value = [checkpoint()]
    broker._mt5.positions_get.return_value = [position(identifier=111)]
    await monitor._recover_open_trades()
    queued = monitor.queue_trade.call_args.kwargs
    assert queued["position_ticket"] == "222" and queued["trade_id"] == "old-trade"
    assert queued["trade_state"]["quantity"] == .04
    assert queued["trade_state"]["sl"] == 1.105
    assert queued["trade_state"]["initial_stop"] == 1.09
    assert queued["rules"]["tp1"] == 1.12
    clear.assert_awaited_once_with("111", "RECOVERED_RETICKET")


@pytest.mark.asyncio
async def test_failed_exit_history_keeps_recovery_row(recovery):
    monitor, broker, states, clear = recovery
    states.return_value = [checkpoint()]
    broker.get_last_exit_deal.side_effect = RuntimeError("history offline")
    await monitor._recover_open_trades()
    clear.assert_not_awaited()
    assert "EURUSD" not in tm._RECOVERED_ASSETS


@pytest.mark.asyncio
async def test_invalid_fast_mode_does_not_abandon_monitor(monkeypatch):
    monkeypatch.setenv("FAST_POSITION_MODE", "typo")
    monitor = tm.TradeMonitor.__new__(tm.TradeMonitor)
    monitor.broker, monitor._shutdown = SimpleNamespace(), False
    monitor._monitor_impl = AsyncMock()
    item = {}
    await monitor._monitor(item)
    monitor._monitor_impl.assert_awaited_once_with(item)


@pytest.mark.asyncio
async def test_recovered_position_without_stop_keeps_timeout_exit(monkeypatch):
    for name in ("upsert_monitor_state", "update_monitor_state", "clear_monitor_state"):
        monkeypatch.setattr(tm, name, AsyncMock())
    monkeypatch.setattr(tm, "_is_market_closed", lambda *_: (False, 0))
    monkeypatch.setattr("packages.db.repositories.comprehensive_logging.update_comprehensive_trade_exit", AsyncMock())
    monitor = tm.TradeMonitor.__new__(tm.TradeMonitor)
    monitor.broker = SimpleNamespace(close=MagicMock())
    monitor.candle_service = SimpleNamespace()
    monitor._shutdown, monitor.on_trade_closed = False, None
    item = {"asset": "EURUSD", "trade_state": {"direction": "long", "entry": 1.1,
            "sl": None, "quantity": .04}, "rules": {},
            "expiry": datetime.now(timezone.utc)-timedelta(seconds=1)}
    await monitor._monitor_impl(item)
    monitor.broker.close.assert_called_once_with("EURUSD")
    tm.clear_monitor_state.assert_awaited_once()


def test_reconnect_refreshes_partials_older_than_recent_history_window(tmp_path):
    now = datetime(2026, 9, 14, 10, tzinfo=timezone.utc)
    first = {"ticket": 1, "position_id": 111, "symbol": "EURUSD", "magic": MT5_MAGIC_NUMBER,
             "entry": 0, "type": 0, "time": now-timedelta(days=6), "volume": 1., "price": 1.10}
    partial = {**first, "ticket": 2, "entry": 1, "time": now-timedelta(days=3), "volume": .4, "profit": 20.}
    rows = [first]
    broker = MT5Adapter.__new__(MT5Adapter)
    broker._credentials = {"login": 123}
    calls = []
    def history(*args, **kwargs):
        if kwargs.get("position"):
            calls.append(kwargs["position"])
            return list(rows)
        return []  # Both deals fall outside the two-day window.
    broker._mt5 = SimpleNamespace(account_info=lambda: SimpleNamespace(server="demo", login=123),
                                  positions_get=lambda: [SimpleNamespace(ticket=222, identifier=111)],
                                  history_deals_get=history)
    db = OperationalLedger(tmp_path/"ledger.sqlite3")
    account, initial = broker.operational_snapshot(now=now-timedelta(days=4))
    db.ingest(account, initial, magic=MT5_MAGIC_NUMBER)
    rows.append(partial)
    account, refreshed = broker.operational_snapshot(now=now)
    db.ingest(account, refreshed, magic=MT5_MAGIC_NUMBER)
    assert calls == [111, 111]
    assert db.position(account, 111)["remaining_quantity"] == .6
    assert db.position(account, 111)["pnl"] == 20.
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("identity_failure", [False, True])
async def test_trace_disk_failure_preserves_quote_exit_but_identity_failure_holds(identity_failure):
    from packages.core.management_feed import ManagementEventFeed, ManagementAccountUnavailable
    from packages.core.trade_monitor import EXIT_TP
    pending = asyncio.Event()
    async def bar_source():
        await pending.wait()
    def record(*_):
        if identity_failure:
            raise ManagementAccountUnavailable("account changed")
        raise OSError("disk full")
    broker = SimpleNamespace(executable_quote=lambda *_: {"price": 120., "time": datetime.now(timezone.utc)})
    feed = ManagementEventFeed(broker, "BTCUSD", bar_source, mode="live", interval=.05, record=record)
    try:
        result = await feed.next({"direction": "long", "entry": 100., "sl": 90., "initial_stop": 90., "quantity": 1.},
                                 {"tp": 110., "runner_enabled": False})
        if identity_failure:
            assert result == {"source": "heartbeat"}
        else:
            assert result["quote_action"] == EXIT_TP
        assert not feed.bar_task.done()
    finally:
        await feed.close()
