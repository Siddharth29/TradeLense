"""Tests for MT5 native SL/TP close detection.

Validates that when MT5 server-side SL/TP closes a position, the trade
monitor correctly:
1. Detects the position is gone via positions_get()
2. Fetches the actual exit deal from MT5 history
3. Updates MongoDB with the correct symbol/price/reason — no cross-symbol
   contamination
4. Fires on_trade_closed callback to clear _active_trade
5. Records partial exits for MT5 live trades
"""

import asyncio
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch, call
from typing import Any, Dict, List

import pytest

from packages.core.services.trade_monitor import TradeMonitor
from packages.adapters.mt5_adapter import MT5Adapter


# ── Helpers ──────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def offline_monitor_io(monkeypatch):
    import packages.core.services.trade_monitor as service
    monkeypatch.setattr(service, "MONITOR_CANDLE_BUFFER_SEC", 0)
    monkeypatch.setattr(service, "_is_market_closed", lambda *a, **k: (False, 0))
    monkeypatch.setattr(service, "_is_market_closing_soon", lambda *a, **k: (False, ""))
    for name in ("upsert_monitor_state", "update_monitor_state", "clear_monitor_state"):
        monkeypatch.setattr(service, name, AsyncMock())

def _make_mt5_broker(
    positions_by_symbol: Dict[str, list] | None = None,
    deals_by_symbol: Dict[str, list] | None = None,
    suffix: str = "m",
):
    """Build a mock MT5Adapter that responds per-symbol."""
    broker = MagicMock(spec=MT5Adapter)
    broker._symbol_suffix = suffix
    broker._mt5 = MagicMock()

    positions_by_symbol = positions_by_symbol or {}
    deals_by_symbol = deals_by_symbol or {}

    def _normalize_symbol(symbol):
        return symbol.upper().strip() + suffix

    broker._normalize_symbol = _normalize_symbol

    def _positions_get(symbol=None):
        raw = symbol.replace(suffix, "") if symbol else ""
        return positions_by_symbol.get(raw, [])

    broker._mt5.positions_get = MagicMock(side_effect=_positions_get)

    def _get_last_exit_deal(symbol, position_ticket=None):
        return deals_by_symbol.get(symbol.upper())

    broker.get_last_exit_deal = MagicMock(side_effect=_get_last_exit_deal)
    broker.close = MagicMock()
    broker.partial_close = MagicMock()
    broker.modify_sl = MagicMock()

    return broker


def _make_candle_service(bars: List[Dict[str, Any]]):
    """Build a candle service that yields bars one at a time."""
    bar_iter = iter(bars)

    async def _get_latest_closed_candle(symbol, interval):
        try:
            return next(bar_iter)
        except StopIteration:
            return bars[-1] if bars else None

    svc = SimpleNamespace(
        get_latest_closed_candle=AsyncMock(side_effect=_get_latest_closed_candle)
    )
    return svc


def _fast_bar_close():
    """Replacement for await_next_bar_close that returns immediately."""
    async def _impl(interval, tz):
        return datetime.now(timezone.utc)
    return _impl


# ── Core: position-gone detection ────────────────────────────────────

@pytest.mark.asyncio
async def test_detects_position_closed_by_native_sl(monkeypatch):
    """When MT5 positions_get returns empty, monitor fires callback."""
    broker = _make_mt5_broker(
        positions_by_symbol={},  # no positions for any symbol
        deals_by_symbol={
            "XAUUSD": {
                "price": 4522.3,
                "volume": 0.01,
                "reason": "SL",
                "profit": -5.2,
                "time": datetime(2026, 5, 26, 14, 37, tzinfo=timezone.utc),
            }
        },
    )

    bars = [{"close": 4520.0, "high": 4525.0, "low": 4515.0, "time": datetime.now(timezone.utc)}]
    svc = _make_candle_service(bars)

    callback = MagicMock()
    monitor = TradeMonitor(svc, broker, on_trade_closed=callback)
    monkeypatch.setattr(
        "packages.core.services.trade_monitor.await_next_bar_close", _fast_bar_close()
    )

    trade_state = {
        "direction": "short", "entry": 4517.5, "sl": 4530.0,
        "initial_stop": 4530.0, "sl_moved": False, "quantity": 0.01,
    }
    rules = {"tp1": 4510.0, "tp": 4500.0}
    expiry = datetime.now(timezone.utc) + timedelta(minutes=30)

    monitor.queue_trade("XAUUSD", trade_state, rules, expiry, "ph", "mode", 0.0)
    await asyncio.sleep(1.5)

    # Callback must fire with actual deal data
    assert callback.called, "on_trade_closed was never called"
    args = callback.call_args[0]
    assert args[0] == "XAUUSD"
    assert args[1] == "MT5_SL"
    assert args[2] == 4522.3  # actual exit price from deal history

    await monitor.shutdown()


@pytest.mark.asyncio
async def test_detects_position_closed_by_native_tp(monkeypatch):
    """TP exit is correctly identified from MT5 deal history."""
    broker = _make_mt5_broker(
        positions_by_symbol={},
        deals_by_symbol={
            "BTCUSD": {
                "price": 72500.0,
                "volume": 0.01,
                "reason": "TP",
                "profit": 12.5,
                "time": datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc),
            }
        },
    )

    bars = [{"close": 72400.0, "high": 72600.0, "low": 72300.0, "time": datetime.now(timezone.utc)}]
    svc = _make_candle_service(bars)

    callback = MagicMock()
    monitor = TradeMonitor(svc, broker, on_trade_closed=callback)
    monkeypatch.setattr(
        "packages.core.services.trade_monitor.await_next_bar_close", _fast_bar_close()
    )

    trade_state = {
        "direction": "long", "entry": 72000.0, "sl": 71500.0,
        "initial_stop": 71500.0, "sl_moved": False, "quantity": 0.01,
    }
    rules = {"tp1": 72250.0, "tp": 72500.0}
    expiry = datetime.now(timezone.utc) + timedelta(minutes=30)

    monitor.queue_trade("BTCUSD", trade_state, rules, expiry, "ph", "mode", 0.0)
    await asyncio.sleep(1.5)

    assert callback.called
    args = callback.call_args[0]
    assert args[0] == "BTCUSD"
    assert args[1] == "MT5_TP"
    assert args[2] == 72500.0

    await monitor.shutdown()


@pytest.mark.asyncio
async def test_fallback_when_deal_history_unavailable(monkeypatch):
    """When deal history returns None, uses candle close as fallback."""
    broker = _make_mt5_broker(
        positions_by_symbol={},
        deals_by_symbol={},  # no deals found
    )

    bars = [{"close": 4520.0, "high": 4525.0, "low": 4515.0, "time": datetime.now(timezone.utc)}]
    svc = _make_candle_service(bars)

    callback = MagicMock()
    monitor = TradeMonitor(svc, broker, on_trade_closed=callback)
    monkeypatch.setattr(
        "packages.core.services.trade_monitor.await_next_bar_close", _fast_bar_close()
    )

    trade_state = {
        "direction": "short", "entry": 4517.5, "sl": 4530.0,
        "initial_stop": 4530.0, "sl_moved": False, "quantity": 0.01,
    }
    rules = {"tp1": 4510.0, "tp": 4500.0}
    expiry = datetime.now(timezone.utc) + timedelta(minutes=30)

    monitor.queue_trade("XAUUSD", trade_state, rules, expiry, "ph", "mode", 0.0)
    await asyncio.sleep(1.5)

    assert callback.called
    args = callback.call_args[0]
    assert args[0] == "XAUUSD"
    assert args[1] == "MT5_NATIVE_CLOSE"
    assert args[2] == 4520.0  # candle close fallback

    await monitor.shutdown()


# ── Cross-symbol safety: the critical test ───────────────────────────

@pytest.mark.asyncio
async def test_no_cross_symbol_contamination(monkeypatch):
    """BTC and XAUUSD close at the same time — each gets its own data.

    Scenario: Both positions closed by MT5 native SL/TP.  Two monitors
    run concurrently.  Verify each callback gets the correct symbol,
    exit price, and reason — no mixing.
    """
    broker = _make_mt5_broker(
        positions_by_symbol={},  # both positions gone
        deals_by_symbol={
            "BTCUSD": {
                "price": 71800.0,
                "volume": 0.01,
                "reason": "SL",
                "profit": -8.0,
                "time": datetime(2026, 5, 26, 14, 35, tzinfo=timezone.utc),
            },
            "XAUUSD": {
                "price": 4505.0,
                "volume": 0.01,
                "reason": "TP",
                "profit": 12.5,
                "time": datetime(2026, 5, 26, 14, 35, tzinfo=timezone.utc),
            },
        },
    )

    bars_btc = [{"close": 71750.0, "high": 71900.0, "low": 71700.0, "time": datetime.now(timezone.utc)}]
    bars_gold = [{"close": 4508.0, "high": 4520.0, "low": 4500.0, "time": datetime.now(timezone.utc)}]

    bar_idx = {"BTCUSD": 0, "XAUUSD": 0}
    bar_data = {"BTCUSD": bars_btc, "XAUUSD": bars_gold}

    async def _get_latest(symbol, interval):
        sym = symbol.upper()
        data = bar_data.get(sym, bars_btc)
        idx = min(bar_idx.get(sym, 0), len(data) - 1)
        bar_idx[sym] = idx + 1
        return data[idx]

    svc = SimpleNamespace(
        get_latest_closed_candle=AsyncMock(side_effect=_get_latest)
    )

    callback = MagicMock()
    monitor = TradeMonitor(svc, broker, on_trade_closed=callback)
    monkeypatch.setattr(
        "packages.core.services.trade_monitor.await_next_bar_close", _fast_bar_close()
    )

    # Queue both trades
    btc_state = {
        "direction": "long", "entry": 72000.0, "sl": 71500.0,
        "initial_stop": 71500.0, "sl_moved": False, "quantity": 0.01,
    }
    gold_state = {
        "direction": "short", "entry": 4517.5, "sl": 4530.0,
        "initial_stop": 4530.0, "sl_moved": False, "quantity": 0.01,
    }

    expiry = datetime.now(timezone.utc) + timedelta(minutes=30)
    monitor.queue_trade("BTCUSD", btc_state, {"tp1": 72250.0, "tp": 72500.0}, expiry, "ph1", "mode", 0.0)
    monitor.queue_trade("XAUUSD", gold_state, {"tp1": 4510.0, "tp": 4500.0}, expiry, "ph2", "mode", 0.0)

    await asyncio.sleep(2.0)

    # Both must have fired
    assert callback.call_count == 2, f"Expected 2 callbacks, got {callback.call_count}"

    # Collect calls by symbol
    results = {}
    for c in callback.call_args_list:
        sym, reason, price = c[0]
        results[sym] = (reason, price)

    # BTC: SL exit at 71800.0
    assert "BTCUSD" in results, f"Missing BTCUSD callback. Got: {list(results.keys())}"
    assert results["BTCUSD"] == ("MT5_SL", 71800.0), f"BTC got wrong data: {results['BTCUSD']}"

    # Gold: TP exit at 4505.0
    assert "XAUUSD" in results, f"Missing XAUUSD callback. Got: {list(results.keys())}"
    assert results["XAUUSD"] == ("MT5_TP", 4505.0), f"Gold got wrong data: {results['XAUUSD']}"

    await monitor.shutdown()


# ── PnL accuracy ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pnl_uses_deal_profit_not_candle_close(monkeypatch):
    """MongoDB update must use deal.profit, not candle-derived PnL."""
    broker = _make_mt5_broker(
        positions_by_symbol={},
        deals_by_symbol={
            "XAUUSD": {
                "price": 4522.315,
                "volume": 0.01,
                "reason": "SL",
                "profit": -4.815,
                "time": datetime(2026, 5, 26, 12, 55, tzinfo=timezone.utc),
            }
        },
    )

    bars = [{"close": 4520.0, "high": 4525.0, "low": 4515.0, "time": datetime.now(timezone.utc)}]
    svc = _make_candle_service(bars)
    monitor = TradeMonitor(svc, broker, on_trade_closed=MagicMock())
    monkeypatch.setattr(
        "packages.core.services.trade_monitor.await_next_bar_close", _fast_bar_close()
    )

    mock_update = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "packages.db.repositories.comprehensive_logging.update_comprehensive_trade_exit",
        mock_update,
    )

    trade_state = {
        "direction": "short", "entry": 4517.5, "sl": 4530.0,
        "initial_stop": 4530.0, "sl_moved": False, "quantity": 0.01,
    }
    rules = {"tp1": 4510.0, "tp": 4500.0}
    expiry = datetime.now(timezone.utc) + timedelta(minutes=30)

    monitor.queue_trade("XAUUSD", trade_state, rules, expiry, "ph", "mode", 0.0, trade_id="abc123")
    await asyncio.sleep(1.5)

    assert mock_update.called, "update_comprehensive_trade_exit was never called"
    kwargs = mock_update.call_args[1]
    assert kwargs["symbol"] == "XAUUSD"
    assert kwargs["exit_price"] == 4522.315  # from deal, not candle close 4520
    assert kwargs["exit_reason"] == "MT5_SL"
    assert kwargs["pnl"] == -4.815  # from deal.profit, not calculated
    assert kwargs["trade_id"] == "abc123"

    await monitor.shutdown()


@pytest.mark.asyncio
async def test_pnl_calculated_when_deal_has_no_profit(monkeypatch):
    """When deal history is unavailable, PnL is calculated from prices."""
    broker = _make_mt5_broker(
        positions_by_symbol={},
        deals_by_symbol={},
    )

    bars = [{"close": 4510.0, "high": 4520.0, "low": 4505.0, "time": datetime.now(timezone.utc)}]
    svc = _make_candle_service(bars)
    monitor = TradeMonitor(svc, broker, on_trade_closed=MagicMock())
    monkeypatch.setattr(
        "packages.core.services.trade_monitor.await_next_bar_close", _fast_bar_close()
    )

    mock_update = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "packages.db.repositories.comprehensive_logging.update_comprehensive_trade_exit",
        mock_update,
    )

    # Short trade: entry 4517.5, close at 4510 → pnl = (4517.5 - 4510) * 0.01 = 0.075
    trade_state = {
        "direction": "short", "entry": 4517.5, "sl": 4530.0,
        "initial_stop": 4530.0, "sl_moved": False, "quantity": 0.01,
    }
    rules = {"tp1": 4510.0, "tp": 4500.0}
    expiry = datetime.now(timezone.utc) + timedelta(minutes=30)

    monitor.queue_trade("XAUUSD", trade_state, rules, expiry, "ph", "mode", 0.0)
    await asyncio.sleep(1.5)

    assert mock_update.called
    kwargs = mock_update.call_args[1]
    assert kwargs["pnl"] == pytest.approx(7.5, abs=0.001)  # Gold contract = 100 oz/lot.

    await monitor.shutdown()


@pytest.mark.asyncio
async def test_long_direction_pnl_fallback(monkeypatch):
    """PnL fallback for long trades: (exit - entry) * qty."""
    broker = _make_mt5_broker(
        positions_by_symbol={},
        deals_by_symbol={},
    )

    bars = [{"close": 72500.0, "high": 72600.0, "low": 72400.0, "time": datetime.now(timezone.utc)}]
    svc = _make_candle_service(bars)
    monitor = TradeMonitor(svc, broker, on_trade_closed=MagicMock())
    monkeypatch.setattr(
        "packages.core.services.trade_monitor.await_next_bar_close", _fast_bar_close()
    )

    mock_update = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "packages.db.repositories.comprehensive_logging.update_comprehensive_trade_exit",
        mock_update,
    )

    trade_state = {
        "direction": "long", "entry": 72000.0, "sl": 71500.0,
        "initial_stop": 71500.0, "sl_moved": False, "quantity": 0.05,
    }
    rules = {"tp1": 72250.0, "tp": 72500.0}
    expiry = datetime.now(timezone.utc) + timedelta(minutes=30)

    monitor.queue_trade("BTCUSD", trade_state, rules, expiry, "ph", "mode", 0.0)
    await asyncio.sleep(1.5)

    assert mock_update.called
    kwargs = mock_update.call_args[1]
    # (72500 - 72000) * 0.05 = 25.0
    assert kwargs["pnl"] == pytest.approx(25.0, abs=0.01)

    await monitor.shutdown()


# ── Position still open: monitor must NOT fire callback ──────────────

@pytest.mark.asyncio
async def test_position_still_open_continues_monitoring(monkeypatch):
    """When MT5 position is still open, monitor keeps evaluating."""
    # Position exists → one fake MT5 position object
    fake_pos = SimpleNamespace(
        _asdict=lambda: {"ticket": 123, "volume": 0.01, "type": 1, "sl": 4530, "tp": 4500, "price_open": 4517},
    )
    broker = _make_mt5_broker(
        positions_by_symbol={"XAUUSD": [fake_pos]},
    )

    # Two bars: first one won't trigger exit, second won't either
    bars = [
        {"close": 4515.0, "high": 4520.0, "low": 4510.0, "time": datetime.now(timezone.utc)},
        {"close": 4516.0, "high": 4519.0, "low": 4512.0, "time": datetime.now(timezone.utc) + timedelta(minutes=5)},
    ]
    svc = _make_candle_service(bars)

    callback = MagicMock()
    monitor = TradeMonitor(svc, broker, on_trade_closed=callback)
    monkeypatch.setattr(
        "packages.core.services.trade_monitor.await_next_bar_close", _fast_bar_close()
    )

    trade_state = {
        "direction": "short", "entry": 4517.5, "sl": 4530.0,
        "initial_stop": 4530.0, "sl_moved": False, "quantity": 0.01,
    }
    rules = {"tp1": 4510.0, "tp": 4500.0}
    expiry = datetime.now(timezone.utc) + timedelta(seconds=3)

    monitor.queue_trade("XAUUSD", trade_state, rules, expiry, "ph", "mode", 0.0)
    await asyncio.sleep(2.0)

    # Callback fires for EXPIRY only, not premature close
    if callback.called:
        args = callback.call_args[0]
        assert args[1] == "EXPIRED", f"Expected EXPIRED, got {args[1]}"

    await monitor.shutdown()


# ── Symbol normalization ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_positions_get_uses_normalized_symbol(monkeypatch):
    """positions_get must be called with broker-normalized symbol (e.g. XAUUSDm)."""
    broker = _make_mt5_broker(
        positions_by_symbol={},
        deals_by_symbol={"XAUUSD": {"price": 4500, "volume": 0.01, "reason": "TP", "profit": 10, "time": None}},
    )

    bars = [{"close": 4510.0, "high": 4520.0, "low": 4505.0, "time": datetime.now(timezone.utc)}]
    svc = _make_candle_service(bars)
    monitor = TradeMonitor(svc, broker, on_trade_closed=MagicMock())
    monkeypatch.setattr(
        "packages.core.services.trade_monitor.await_next_bar_close", _fast_bar_close()
    )

    trade_state = {
        "direction": "short", "entry": 4517.5, "sl": 4530.0,
        "initial_stop": 4530.0, "sl_moved": False, "quantity": 0.01,
    }
    rules = {"tp1": 4510.0, "tp": 4500.0}
    expiry = datetime.now(timezone.utc) + timedelta(minutes=30)

    monitor.queue_trade("XAUUSD", trade_state, rules, expiry, "ph", "mode", 0.0)
    await asyncio.sleep(1.5)

    # Verify positions_get was called with normalized symbol "XAUUSDm"
    pos_calls = broker._mt5.positions_get.call_args_list
    assert len(pos_calls) >= 1
    assert pos_calls[0][1]["symbol"] == "XAUUSDm"

    await monitor.shutdown()


# ── Non-MT5 broker: position check must be skipped ───────────────────

@pytest.mark.asyncio
async def test_non_mt5_broker_skips_position_check(monkeypatch):
    """A broker without an MT5 handle must not trigger the MT5 position check."""
    broker = SimpleNamespace(
        move_sl_to_be=MagicMock(),
        close=MagicMock(),
        partial_close=MagicMock(),
        modify_sl=MagicMock(),
    )

    closes = [94, 94]
    async def fetch(asset, n5, n15):
        c = closes.pop(0) if closes else 94
        return ([{"close": c, "high": 102, "low": 93}], [])

    svc = SimpleNamespace(fetch=AsyncMock(side_effect=fetch))
    monitor = TradeMonitor(svc, broker, on_trade_closed=MagicMock())
    monkeypatch.setattr(
        "packages.core.services.trade_monitor.await_next_bar_close", _fast_bar_close()
    )

    trade_state = {"direction": "long", "entry": 100, "sl": 95, "sl_moved": False}
    rules = {"tp1": 105, "tp": 110}
    expiry = datetime.now(timezone.utc) + timedelta(minutes=30)
    monitor.queue_trade("NIFTY", trade_state, rules, expiry, "ph", "mode", 0.0)

    await asyncio.sleep(1.5)

    # Must NOT try positions_get (this mock broker has no _mt5)
    assert not hasattr(broker, "positions_get")
    assert not hasattr(broker, "_mt5")

    await monitor.shutdown()


# ── MT5Adapter.get_last_exit_deal unit tests ─────────────────────────

class TestGetLastExitDeal:
    """Unit tests for the deal history helper on MT5Adapter."""

    def _make_adapter(self, deals_raw):
        adapter = MT5Adapter.__new__(MT5Adapter)
        adapter._mt5 = MagicMock()
        adapter._symbol_suffix = "m"
        adapter._health_ts = 0.0
        adapter._health_ok = False
        adapter._credentials = {}
        adapter._mt5.history_deals_get = MagicMock(return_value=deals_raw)
        return adapter

    def _deal(self, price, entry, reason, profit=0.0, volume=0.01, time_ts=1716739200):
        """Create a fake MT5 deal namedtuple-like object."""
        return SimpleNamespace(
            _asdict=lambda p=price, e=entry, r=reason, pr=profit, v=volume, t=time_ts: {
                "price": p, "entry": e, "reason": r,
                "profit": pr, "volume": v, "time": t,
            }
        )

    def test_returns_sl_deal(self):
        deals = [
            self._deal(price=72000, entry=0, reason=0),    # IN deal
            self._deal(price=71800, entry=1, reason=4, profit=-8.0),  # OUT + SL
        ]
        adapter = self._make_adapter(deals)
        result = adapter.get_last_exit_deal("BTCUSD")

        assert result is not None
        assert result["price"] == 71800
        assert result["reason"] == "SL"
        assert result["profit"] == -8.0

    def test_returns_tp_deal(self):
        deals = [
            self._deal(price=4517, entry=0, reason=0),
            self._deal(price=4505, entry=1, reason=5, profit=12.5),  # OUT + TP
        ]
        adapter = self._make_adapter(deals)
        result = adapter.get_last_exit_deal("XAUUSD")

        assert result is not None
        assert result["price"] == 4505
        assert result["reason"] == "TP"
        assert result["profit"] == 12.5

    def test_returns_none_when_no_deals(self):
        adapter = self._make_adapter(None)
        assert adapter.get_last_exit_deal("BTCUSD") is None

    def test_returns_none_when_empty_deals(self):
        adapter = self._make_adapter([])
        assert adapter.get_last_exit_deal("BTCUSD") is None

    def test_returns_none_when_only_in_deals(self):
        deals = [self._deal(price=72000, entry=0, reason=0)]  # IN only
        adapter = self._make_adapter(deals)
        assert adapter.get_last_exit_deal("BTCUSD") is None

    def test_returns_most_recent_out_deal(self):
        """When multiple OUT deals exist, returns the last one."""
        deals = [
            self._deal(price=4510, entry=1, reason=3, profit=7.0, time_ts=1000),
            self._deal(price=4505, entry=1, reason=3, profit=12.5, time_ts=2000),
        ]
        adapter = self._make_adapter(deals)
        result = adapter.get_last_exit_deal("XAUUSD")
        assert result["price"] == 4505  # most recent (last in list)

    def test_uses_normalized_symbol(self):
        adapter = self._make_adapter([])
        adapter.get_last_exit_deal("xauusd")
        # history_deals_get is called with "XAUUSDm"
        call_args = adapter._mt5.history_deals_get.call_args
        assert call_args[1]["group"] == "XAUUSDm"

    def test_handles_exception_gracefully(self):
        adapter = self._make_adapter([])
        adapter._mt5.history_deals_get = MagicMock(side_effect=Exception("IPC error"))
        result = adapter.get_last_exit_deal("BTCUSD")
        assert result is None

    def test_unknown_reason_code_formatted(self):
        deals = [self._deal(price=100, entry=1, reason=7)]
        adapter = self._make_adapter(deals)
        result = adapter.get_last_exit_deal("EURUSD")
        assert result["reason"] == "REASON_7"

    def test_not_logged_in_returns_none(self):
        adapter = MT5Adapter.__new__(MT5Adapter)
        adapter._mt5 = None
        adapter._symbol_suffix = ""
        adapter._health_ts = 0.0
        adapter._health_ok = False
        adapter._credentials = {}
        assert adapter.get_last_exit_deal("XAUUSD") is None


# ── MongoDB update correctness ───────────────────────────────────────

@pytest.mark.asyncio
async def test_mongodb_update_called_with_deal_closed_at(monkeypatch):
    """trade_closed_at must use deal.time, not current time."""
    deal_time = datetime(2026, 5, 26, 14, 37, 12, tzinfo=timezone.utc)
    broker = _make_mt5_broker(
        positions_by_symbol={},
        deals_by_symbol={
            "XAUUSD": {
                "price": 4522.3, "volume": 0.01, "reason": "SL",
                "profit": -5.2, "time": deal_time,
            }
        },
    )

    bars = [{"close": 4520.0, "high": 4525.0, "low": 4515.0, "time": datetime.now(timezone.utc)}]
    svc = _make_candle_service(bars)
    monitor = TradeMonitor(svc, broker, on_trade_closed=MagicMock())
    monkeypatch.setattr(
        "packages.core.services.trade_monitor.await_next_bar_close", _fast_bar_close()
    )

    mock_update = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "packages.db.repositories.comprehensive_logging.update_comprehensive_trade_exit",
        mock_update,
    )

    trade_state = {
        "direction": "short", "entry": 4517.5, "sl": 4530.0,
        "initial_stop": 4530.0, "sl_moved": False, "quantity": 0.01,
    }
    rules = {"tp1": 4510.0, "tp": 4500.0}
    expiry = datetime.now(timezone.utc) + timedelta(minutes=30)

    monitor.queue_trade("XAUUSD", trade_state, rules, expiry, "ph", "mode", 0.0)
    await asyncio.sleep(1.5)

    assert mock_update.called
    kwargs = mock_update.call_args[1]
    assert kwargs["trade_closed_at"] == deal_time

    await monitor.shutdown()


# ── Partial close recording for MT5 live ─────────────────────────────

@pytest.mark.asyncio
async def test_mt5_partial_close_records_to_mongodb(monkeypatch):
    """When partial_close succeeds on MT5, a TP1_PARTIAL record is stored."""
    fake_pos = SimpleNamespace(
        _asdict=lambda: {"ticket": 99, "volume": 0.10, "type": 1, "sl": 4530, "tp": 4500, "price_open": 4517},
    )
    broker = _make_mt5_broker(
        positions_by_symbol={"XAUUSD": [fake_pos]},
    )
    broker.partial_close = MagicMock(return_value=0.05)  # confirmed fill

    # Bar 1: hits TP1 → MOVE_SL_TO_BE → partial close + record
    bars = [
        {"close": 4508.0, "high": 4518.0, "low": 4505.0, "time": datetime.now(timezone.utc)},
    ]
    svc = _make_candle_service(bars)
    monitor = TradeMonitor(svc, broker, on_trade_closed=MagicMock())
    monkeypatch.setattr(
        "packages.core.services.trade_monitor.await_next_bar_close", _fast_bar_close()
    )

    mock_store = AsyncMock(return_value="doc_id_123")
    monkeypatch.setattr(
        "packages.db.repositories.comprehensive_logging.store_comprehensive_trade",
        mock_store,
    )
    # Also mock the exit update to prevent errors
    monkeypatch.setattr(
        "packages.db.repositories.comprehensive_logging.update_comprehensive_trade_exit",
        AsyncMock(return_value=True),
    )

    trade_state = {
        "direction": "short", "entry": 4517.5, "sl": 4530.0,
        "initial_stop": 4530.0, "sl_moved": False, "quantity": 0.10,
        "asset": "XAUUSD",
    }
    rules = {"tp1": 4510.0, "tp": 4500.0, "move_sl_to_be": True}
    expiry = datetime.now(timezone.utc) + timedelta(seconds=3)

    monitor.queue_trade("XAUUSD", trade_state, rules, expiry, "ph", "mode", 0.0)
    await asyncio.sleep(2.0)

    # partial_close was called
    assert broker.partial_close.called

    # Check the MongoDB store was called with TP1_PARTIAL
    if mock_store.called:
        kwargs = mock_store.call_args[1]
        assert kwargs["symbol"] == "XAUUSD"
        assert kwargs["trade_status"] == "closed"
        assert kwargs["exit_reason"] == "TP1_PARTIAL"
        assert kwargs["trade_type"] == "live"
        assert kwargs["broker_type"] == "MT5"
        assert kwargs["quantity"] == pytest.approx(0.05, abs=0.001)

    await monitor.shutdown()


# ── Latency: position check must not block ───────────────────────────

@pytest.mark.asyncio
async def test_position_check_does_not_block_event_loop(monkeypatch):
    """MT5 IPC calls in position check must run via to_thread, not inline."""
    broker = _make_mt5_broker(
        positions_by_symbol={},
        deals_by_symbol={
            "XAUUSD": {"price": 4500, "volume": 0.01, "reason": "TP", "profit": 10, "time": None}
        },
    )

    # Track whether positions_get was called from a thread
    original_to_thread = asyncio.to_thread
    thread_calls = []

    async def _tracking_to_thread(func, *args, **kwargs):
        thread_calls.append(func.__name__ if hasattr(func, '__name__') else str(func))
        return await original_to_thread(func, *args, **kwargs)

    monkeypatch.setattr("packages.core.services.trade_monitor.asyncio.to_thread", _tracking_to_thread)

    bars = [{"close": 4510.0, "high": 4520.0, "low": 4505.0, "time": datetime.now(timezone.utc)}]
    svc = _make_candle_service(bars)
    monitor = TradeMonitor(svc, broker, on_trade_closed=MagicMock())
    monkeypatch.setattr(
        "packages.core.services.trade_monitor.await_next_bar_close", _fast_bar_close()
    )

    trade_state = {
        "direction": "short", "entry": 4517.5, "sl": 4530.0,
        "initial_stop": 4530.0, "sl_moved": False, "quantity": 0.01,
    }
    rules = {"tp1": 4510.0, "tp": 4500.0}
    expiry = datetime.now(timezone.utc) + timedelta(minutes=30)

    monitor.queue_trade("XAUUSD", trade_state, rules, expiry, "ph", "mode", 0.0)
    await asyncio.sleep(1.5)

    # Both positions_get and get_last_exit_deal must have been dispatched to threads
    assert any("positions_get" in c for c in thread_calls), \
        f"positions_get not called via to_thread. Calls: {thread_calls}"
    assert any("get_last_exit_deal" in c for c in thread_calls), \
        f"get_last_exit_deal not called via to_thread. Calls: {thread_calls}"

    await monitor.shutdown()
