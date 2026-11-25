"""C3 — partials must be booked at the volume the broker FILLED.

`partial_close` floors the request to `lot_step` and clamps it up to `min_lot`,
then used to return a bare bool. The monitor assumed `quantity * fraction` for both
the P&L leg and the remaining size, so on any position that is not an even multiple
of twice the lot step, the trade record and the engine's own idea of its exposure
both went wrong. Confirmed in production: `comprehensive_trades` holds TP1/TP2 rows
at 0.035 / 0.045 / 0.055 lots — volumes MT5 cannot hold.

The parity test at the bottom is the one that keeps this fixed: the live adapter and
the backtest's `PositionSimulator` must quantise identically, or the two engines
book different money for the same fill.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from packages.adapters.mt5_adapter import MT5Adapter
from packages.core.backtest_account import PositionSimulator


class _FakeMT5:
    """Minimum MetaTrader5 surface for partial_close."""

    ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
    TRADE_ACTION_DEAL, ORDER_TIME_GTC, ORDER_FILLING_IOC = 1, 0, 0

    def __init__(self, volume: float, step: float = 0.01, vmin: float = 0.01):
        self._volume = volume
        self._step = step
        self._vmin = vmin
        self.sent_volume = None

    def positions_get(self, symbol=None):
        return [SimpleNamespace(_asdict=lambda: {
            "ticket": 111, "volume": self._volume, "type": 0})]

    def symbol_info(self, symbol):
        return SimpleNamespace(_asdict=lambda: {
            "volume_step": self._step, "volume_min": self._vmin})

    def symbol_info_tick(self, symbol):
        return SimpleNamespace(_asdict=lambda: {"bid": 100.0, "ask": 100.1})

    def order_send(self, request):
        self.sent_volume = request["volume"]
        return SimpleNamespace(_asdict=lambda: {"retcode": 10009, "volume": request["volume"]})


def _adapter(volume: float) -> tuple[MT5Adapter, _FakeMT5]:
    a = MT5Adapter.__new__(MT5Adapter)
    fake = _FakeMT5(volume)
    a._mt5 = fake
    a._reconnect_if_needed = lambda: None
    a._normalize_symbol = lambda s: s
    return a, fake


class TestAdapterReturnsWhatItFilled:
    @pytest.mark.parametrize("volume,fraction,expected", [
        (0.07, 0.5, 0.03),   # 0.035 requested -> floors to 0.03
        (0.09, 0.5, 0.04),   # 0.045 requested -> 0.04
        (0.11, 0.5, 0.05),   # 0.055 requested -> 0.05
        (0.04, 0.5, 0.02),   # even multiple: request == fill
        (0.02, 0.34, 0.01),  # TP2 fraction, floors up to min_lot
    ])
    def test_returns_the_filled_volume_not_the_request(self, volume, fraction, expected):
        adapter, fake = _adapter(volume)
        filled = adapter.partial_close("XAUUSD", fraction)
        assert filled == pytest.approx(expected), (
            f"{volume} lots x {fraction} should FILL {expected}, got {filled}"
        )
        assert fake.sent_volume == pytest.approx(expected), "order volume disagrees with the return"
        if abs(volume * fraction - expected) > 1e-9:
            assert filled != pytest.approx(volume * fraction), (
                "returned the requested volume — this is the C3 bug"
            )

    def test_refusal_still_returns_False(self):
        """The truthiness contract every caller relies on must not change."""
        adapter, _ = _adapter(0.01)          # at min lot, cannot split
        assert adapter.partial_close("XAUUSD", 0.5) is False

    def test_return_annotation_is_not_bare_bool(self):
        assert "bool" != str(inspect.signature(MT5Adapter.partial_close).return_annotation)


class TestMonitorBooksTheFill:
    """The monitor must decrement by, and price, the filled volume."""

    def _monitor_source(self) -> str:
        return Path("packages/core/services/trade_monitor.py").read_text(encoding="utf-8")

    def test_tp1_uses_the_filled_volume(self):
        src = self._monitor_source()
        assert "filled_qty = float(closed) if partial_ok else 0.0" in src
        assert "partial_qty = filled_qty" in src
        assert 'trade_state.get("quantity", 0) * 0.5' not in src, (
            "TP1 is halving the in-memory quantity again instead of subtracting the fill"
        )

    def test_tp2_uses_the_filled_volume(self):
        src = self._monitor_source()
        assert "tp2_filled_qty = float(closed) if tp2_partial_done else 0.0" in src
        assert "closed_qty = tp2_filled_qty" in src
        assert 'trade_state.get("quantity", 0) * TP2_PARTIAL_FRACTION' not in src

    def test_remaining_quantity_never_goes_negative(self):
        src = self._monitor_source()
        assert src.count("max(\n                                0.0, trade_state.get") \
            + src.count("max(\n                            0.0, trade_state.get") >= 2


class TestLiveBacktestParity:
    """The adapter and PositionSimulator must quantise identically.

    If they drift, the same fill books different money in the two engines and no
    backtest/live comparison is meaningful.
    """

    @pytest.mark.parametrize("volume,fraction", [
        (0.07, 0.5), (0.09, 0.5), (0.11, 0.5), (0.04, 0.5),
        (0.03, 0.5), (0.02, 0.34), (0.06, 0.34), (0.25, 0.34),
    ])
    def test_adapter_and_simulator_agree(self, volume, fraction):
        adapter, _ = _adapter(volume)
        try:
            live = adapter.partial_close("XAUUSD", fraction)
        except Exception:
            live = False

        sim = PositionSimulator(
            "XAUUSD",
            {"direction": "long", "entry": 100.0, "sl": 99.0,
             "initial_stop": 99.0, "quantity": volume},
            {"tp1": 101.0, "tp": 102.0},
            volume,
        )
        backtest = sim._splittable_volume(fraction)

        if live is False:
            assert backtest is None, (
                f"{volume}x{fraction}: live refuses the split, backtest books "
                f"{backtest} — the backtest would credit a fill live never gets"
            )
        else:
            assert backtest is not None, (
                f"{volume}x{fraction}: live fills {live}, backtest refuses"
            )
            assert live == pytest.approx(backtest), (
                f"{volume}x{fraction}: live fills {live}, backtest books {backtest}"
            )
