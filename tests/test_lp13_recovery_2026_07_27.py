"""Tests for the 2026-07-27 LP-13 fix (restart-orphaned positions).

Covers the recovery-side accounting: a monitor_state row whose position closed
while the process was down must produce a synthetic exit through the NORMAL
bookkeeping (update_comprehensive_trade_exit → Mongo close + trade_exit event +
RECENT_TRADES) using the REAL broker deal (96h lookback), never a theoretical
P&L — and must retain the row when no deal can be found. Also covers the
retry allowance: a broker that never becomes ready must un-mark the asset so a
later monitor init can retry recovery.

Evidence: PATTERN_LIBRARY LP-13 (07-14 USTEC −$6.82, 07-24 EURUSD −$6.42).
The eager-init half of the fix (runner constructs the monitor on seeing an
open position with no monitor) is covered by the flow smoke, not here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import packages.core.services.trade_monitor as tm
import packages.db.repositories.comprehensive_logging as cl
from packages.adapters.mt5_adapter import MT5Adapter


def _bare_monitor(broker) -> tm.TradeMonitor:
    """TradeMonitor without __init__ (no loops/tasks) — recovery only."""
    mon = tm.TradeMonitor.__new__(tm.TradeMonitor)
    mon.broker = broker
    mon._owner_asset = "EURUSD"
    return mon


def _bare_mt5_broker():
    broker = MT5Adapter.__new__(MT5Adapter)
    broker._mt5 = SimpleNamespace(positions_get=lambda symbol=None: [])
    broker._normalize_symbol = lambda s: s
    return broker


_DOC = {
    "asset": "EURUSD", "symbol": "EURUSD", "position_ticket": "111",
    "trade_id": "abc123",
    "trade_state": {"entry": 1.13828, "sl": 1.1393, "direction": "short",
                    "quantity": 0.06},
}


@pytest.mark.asyncio
async def test_recovered_gone_records_synthetic_exit(monkeypatch):
    broker = _bare_mt5_broker()
    seen = {}

    def fake_deal(sym, ticket=None, lookback_hours=24.0):
        seen["lookback"] = lookback_hours
        return {"price": 1.1393, "profit": -6.12, "reason": "SL",
                "time": datetime(2026, 7, 24, 7, 9, tzinfo=timezone.utc),
                "position_id": 111}

    broker.get_last_exit_deal = fake_deal
    mon = _bare_monitor(broker)

    async def fake_states():
        return [dict(_DOC)]

    cleared = []

    async def fake_clear(ticket, reason):
        cleared.append((ticket, reason))

    calls = {}

    async def fake_update(**kw):
        calls.update(kw)
        return True

    monkeypatch.setattr(tm, "get_open_monitor_states", fake_states)
    monkeypatch.setattr(tm, "clear_monitor_state", fake_clear)
    monkeypatch.setattr(cl, "update_comprehensive_trade_exit", fake_update)
    tm._RECOVERED_ASSETS.discard("EURUSD")

    await mon._recover_open_trades()

    assert calls["exit_reason"] == "RECOVERED_GONE"
    assert calls["pnl"] == -6.12
    assert calls["exit_price"] == 1.1393
    assert calls["position_ticket"] == "111"
    assert calls["trade_id"] == "abc123"
    assert seen["lookback"] == 96.0          # weekend-orphan window
    assert ("111", "RECOVERED_GONE") in cleared
    tm._RECOVERED_ASSETS.discard("EURUSD")


@pytest.mark.asyncio
async def test_recovered_gone_without_deal_retains_state_and_records_nothing(monkeypatch):
    broker = _bare_mt5_broker()
    broker.get_last_exit_deal = lambda *a, **k: None
    mon = _bare_monitor(broker)

    async def fake_states():
        return [dict(_DOC)]

    cleared = []

    async def fake_clear(ticket, reason):
        cleared.append((ticket, reason))

    calls = {}

    async def fake_update(**kw):
        calls.update(kw)
        return True

    monkeypatch.setattr(tm, "get_open_monitor_states", fake_states)
    monkeypatch.setattr(tm, "clear_monitor_state", fake_clear)
    monkeypatch.setattr(cl, "update_comprehensive_trade_exit", fake_update)
    tm._RECOVERED_ASSETS.discard("EURUSD")

    await mon._recover_open_trades()

    assert not calls                          # nothing invented
    assert not cleared
    assert "EURUSD" not in tm._RECOVERED_ASSETS
    tm._RECOVERED_ASSETS.discard("EURUSD")


@pytest.mark.asyncio
async def test_broker_never_ready_unmarks_asset_for_retry(monkeypatch):
    broker = MT5Adapter.__new__(MT5Adapter)
    broker._mt5 = None                        # terminal never comes up
    mon = _bare_monitor(broker)

    monkeypatch.setattr(tm, "MONITOR_RECOVERY_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(tm, "MONITOR_RECOVERY_RETRY_SECONDS", 0)
    tm._RECOVERED_ASSETS.discard("EURUSD")

    await mon._recover_open_trades()

    # un-marked ⇒ a later monitor construction retries recovery
    assert "EURUSD" not in tm._RECOVERED_ASSETS
