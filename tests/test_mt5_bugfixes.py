"""Tests for MT5 adapter bug fixes:
- BUG-2: XAUUSD should NOT be classified as crypto
- BUG-8: modify_sl should skip when new_sl == current_sl
"""
import pytest
from unittest.mock import MagicMock


from packages.adapters import mt5_adapter as mt5_mod


# ── BUG-2: XAUUSD is_crypto classification ─────────────────────────

class TestIsCryptoClassification:
    """Verify the is_crypto list in _fetch_candles_internal excludes XAUUSD."""

    def _get_is_crypto_from_source(self):
        """Extract the is_crypto check from the source to verify it's correct."""
        import inspect
        source = inspect.getsource(mt5_mod.MT5Adapter._fetch_candles_internal)
        return source

    def test_xauusd_not_in_crypto_list(self):
        source = self._get_is_crypto_from_source()
        assert "XAUUSD" not in source or "is_crypto" not in source.split("XAUUSD")[0].split("\n")[-1], \
            "XAUUSD should not appear in the is_crypto check"

    def test_btcusd_in_crypto_list(self):
        assert mt5_mod.is_crypto_symbol("BTCUSDm")

    def test_ethusdt_in_crypto_list(self):
        assert mt5_mod.is_crypto_symbol("ETHUSDT")

    @pytest.mark.parametrize("symbol,expected", [
        ("BTCUSD", True),
        ("BTCUSDT", True),
        ("ETHUSD", True),
        ("ETHUSDT", True),
        ("XAUUSD", False),
        ("GBPUSD", False),
        ("EURUSD", False),
        ("XAGUSD", False),
    ])
    def test_crypto_classification(self, symbol, expected):
        is_crypto = mt5_mod.is_crypto_symbol(symbol)
        assert is_crypto == expected


# ── BUG-8: modify_sl no-change guard ────────────────────────────────

class TestModifySLNoChangeGuard:
    def _make_adapter_with_position(
        self, current_sl=100.0, pos_type=0, digits=None,
        stops_level=None, point=None, bid=None, ask=None,
    ):
        adapter = mt5_mod.MT5Adapter()
        mt5_mock = MagicMock()
        adapter._mt5 = mt5_mock
        adapter._normalize_symbol = lambda s: s

        pos_mock = MagicMock()
        pos_mock._asdict.return_value = {
            "ticket": 12345,
            "tp": 110.0,
            "sl": current_sl,
            "volume": 0.01,
            "type": pos_type,
        }
        mt5_mock.positions_get.return_value = [pos_mock]
        # When a precision is supplied, expose it via symbol_info so the
        # tick-resolution skip-guard can engage (mirrors a real broker).
        if digits is not None or stops_level is not None:
            sym_data = {}
            if digits is not None:
                sym_data["digits"] = digits
            if stops_level is not None:
                sym_data["trade_stops_level"] = stops_level
            if point is not None:
                sym_data["point"] = point
            sym_mock = MagicMock()
            sym_mock._asdict.return_value = sym_data
            mt5_mock.symbol_info.return_value = sym_mock
        if bid is not None or ask is not None:
            tick_mock = MagicMock()
            tick_mock.bid = bid if bid is not None else 0.0
            tick_mock.ask = ask if ask is not None else 0.0
            mt5_mock.symbol_info_tick.return_value = tick_mock
        return adapter, mt5_mock

    def test_skip_when_sl_unchanged(self):
        adapter, mt5_mock = self._make_adapter_with_position(current_sl=100.0)
        adapter.modify_sl("BTCUSD", 100.0)
        mt5_mock.order_send.assert_not_called()

    def test_skip_when_sl_within_epsilon(self):
        adapter, mt5_mock = self._make_adapter_with_position(current_sl=100.0)
        adapter.modify_sl("BTCUSD", 100.0 + 1e-10)
        mt5_mock.order_send.assert_not_called()

    def test_proceeds_when_sl_different(self):
        adapter, mt5_mock = self._make_adapter_with_position(current_sl=100.0)
        result_mock = MagicMock()
        result_mock._asdict.return_value = {"retcode": 10009, "comment": "OK"}
        mt5_mock.order_send.return_value = result_mock

        adapter.modify_sl("BTCUSD", 101.0)
        mt5_mock.order_send.assert_called_once()

    def test_refuses_backward_sl_buy(self):
        adapter, mt5_mock = self._make_adapter_with_position(
            current_sl=100.0, pos_type=0
        )
        adapter.modify_sl("BTCUSD", 99.0)
        mt5_mock.order_send.assert_not_called()

    def test_refuses_backward_sl_sell(self):
        adapter, mt5_mock = self._make_adapter_with_position(
            current_sl=100.0, pos_type=1
        )
        adapter.modify_sl("BTCUSD", 101.0)
        mt5_mock.order_send.assert_not_called()

    def test_no_position_raises(self):
        adapter = mt5_mod.MT5Adapter()
        mt5_mock = MagicMock()
        adapter._mt5 = mt5_mock
        adapter._normalize_symbol = lambda s: s
        mt5_mock.positions_get.return_value = []

        with pytest.raises(RuntimeError, match="No open position"):
            adapter.modify_sl("BTCUSD", 100.0)

    def test_forward_sl_buy_proceeds(self):
        adapter, mt5_mock = self._make_adapter_with_position(
            current_sl=100.0, pos_type=0
        )
        result_mock = MagicMock()
        result_mock._asdict.return_value = {"retcode": 10009, "comment": "OK"}
        mt5_mock.order_send.return_value = result_mock

        adapter.modify_sl("BTCUSD", 101.0)
        mt5_mock.order_send.assert_called_once()
        call_args = mt5_mock.order_send.call_args[0][0]
        assert call_args["sl"] == 101.0

    def test_forward_sl_sell_proceeds(self):
        adapter, mt5_mock = self._make_adapter_with_position(
            current_sl=100.0, pos_type=1
        )
        result_mock = MagicMock()
        result_mock._asdict.return_value = {"retcode": 10009, "comment": "OK"}
        mt5_mock.order_send.return_value = result_mock

        adapter.modify_sl("BTCUSD", 99.0)
        mt5_mock.order_send.assert_called_once()
        call_args = mt5_mock.order_send.call_args[0][0]
        assert call_args["sl"] == 99.0

    def test_skip_when_subtick_equal_after_rounding(self):
        """A sub-tick recompute that rounds to the current SL must be skipped.

        Reproduces the production case where the trailing step recomputes the
        same level the BE move already set (broker rounded), so no broker
        round-trip should be issued.
        """
        adapter, mt5_mock = self._make_adapter_with_position(
            current_sl=100.0, pos_type=0, digits=2
        )
        adapter.modify_sl("BTCUSD", 100.001)  # rounds to 100.00 == current
        mt5_mock.order_send.assert_not_called()

    def test_request_sl_rounded_to_symbol_digits(self):
        """The SL sent to the broker is rounded to the symbol's precision."""
        adapter, mt5_mock = self._make_adapter_with_position(
            current_sl=100.0, pos_type=0, digits=2
        )
        result_mock = MagicMock()
        result_mock._asdict.return_value = {"retcode": 10009, "comment": "OK"}
        mt5_mock.order_send.return_value = result_mock

        adapter.modify_sl("BTCUSD", 100.567123)
        mt5_mock.order_send.assert_called_once()
        call_args = mt5_mock.order_send.call_args[0][0]
        assert call_args["sl"] == 100.57

    def test_no_changes_retcode_is_benign(self):
        """Broker retcode 10025 ('No changes') must not raise — the SL is
        already at the requested level, so it is a benign no-op."""
        adapter, mt5_mock = self._make_adapter_with_position(
            current_sl=100.0, pos_type=0, digits=2
        )
        result_mock = MagicMock()
        result_mock._asdict.return_value = {"retcode": 10025, "comment": "No changes"}
        mt5_mock.order_send.return_value = result_mock

        # A genuine forward move that the broker nonetheless reports as
        # unchanged must return cleanly rather than raising RuntimeError.
        adapter.modify_sl("BTCUSD", 100.05)
        mt5_mock.order_send.assert_called_once()


# ── modify_sl stops-level clamp (2026-06-12) ─────────────────────────
#
# Exness rejects an SL closer to the current price than
# trade_stops_level × point with retcode 10016 ("Invalid stops") — seen
# live on GBPUSDm 2026-06-12. The adapter now clamps the requested SL to
# the closest legal level and returns the value the broker holds.

class TestModifySLStopsLevelClamp:
    # Borrow the position/symbol-info fixture builder (plain function in
    # Py3, so this binds as a normal method here) without inheriting the
    # parent's tests, which pytest would otherwise run twice.
    _make_adapter_with_position = TestModifySLNoChangeGuard._make_adapter_with_position

    def _ok_result(self, mt5_mock):
        result_mock = MagicMock()
        result_mock._asdict.return_value = {"retcode": 10009, "comment": "OK"}
        mt5_mock.order_send.return_value = result_mock

    def test_clamp_buy_sl_to_stops_level(self):
        # min_dist = 20 × 0.01 = 0.2; bid 101.0 → max legal SL = 100.8
        adapter, mt5_mock = self._make_adapter_with_position(
            current_sl=100.0, pos_type=0, digits=2,
            stops_level=20, point=0.01, bid=101.0, ask=101.02,
        )
        self._ok_result(mt5_mock)
        applied = adapter.modify_sl("BTCUSD", 100.95)
        call_args = mt5_mock.order_send.call_args[0][0]
        assert call_args["sl"] == 100.80
        assert applied == 100.80

    def test_clamp_sell_sl_to_stops_level(self):
        # min_dist = 0.2; ask 101.0 → min legal SL = 101.2
        adapter, mt5_mock = self._make_adapter_with_position(
            current_sl=101.5, pos_type=1, digits=2,
            stops_level=20, point=0.01, bid=100.98, ask=101.0,
        )
        self._ok_result(mt5_mock)
        applied = adapter.modify_sl("BTCUSD", 101.1)
        call_args = mt5_mock.order_send.call_args[0][0]
        assert call_args["sl"] == 101.20
        assert applied == 101.20

    def test_clamp_noop_when_sl_already_at_legal_limit(self):
        # Current SL (100.9) is already beyond the legal max (100.8): the
        # clamped value would move the SL backward → refuse, no round-trip.
        adapter, mt5_mock = self._make_adapter_with_position(
            current_sl=100.9, pos_type=0, digits=2,
            stops_level=20, point=0.01, bid=101.0, ask=101.02,
        )
        applied = adapter.modify_sl("BTCUSD", 100.95)
        mt5_mock.order_send.assert_not_called()
        assert applied == 100.9

    def test_no_clamp_when_stops_level_zero(self):
        adapter, mt5_mock = self._make_adapter_with_position(
            current_sl=100.0, pos_type=0, digits=2,
            stops_level=0, point=0.01, bid=101.0, ask=101.02,
        )
        self._ok_result(mt5_mock)
        applied = adapter.modify_sl("BTCUSD", 100.95)
        call_args = mt5_mock.order_send.call_args[0][0]
        assert call_args["sl"] == 100.95
        assert applied == 100.95

    def test_no_clamp_when_sl_far_enough(self):
        adapter, mt5_mock = self._make_adapter_with_position(
            current_sl=100.0, pos_type=0, digits=2,
            stops_level=20, point=0.01, bid=101.0, ask=101.02,
        )
        self._ok_result(mt5_mock)
        applied = adapter.modify_sl("BTCUSD", 100.5)  # below 100.8 limit
        call_args = mt5_mock.order_send.call_args[0][0]
        assert call_args["sl"] == 100.5
        assert applied == 100.5

    def test_skip_path_returns_current_sl(self):
        adapter, mt5_mock = self._make_adapter_with_position(current_sl=100.0)
        applied = adapter.modify_sl("BTCUSD", 100.0)
        mt5_mock.order_send.assert_not_called()
        assert applied == 100.0
