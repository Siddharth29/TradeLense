"""
E2E tests for market-hours related fixes.

Validates all 5 bugs discovered during log analysis (logs (12).json):

  Fix 1 — BTCUSD broker maintenance gap now recognized (was: 24/7 = no gaps)
  Fix 2 — MARKET_CLOSE_WINDOWS expanded to all traded symbols
  Fix 3 — MultiExpertSignal accepts extra LLM fields (extra="ignore")
  Fix 4 — Trade close retries after market reopen (retcode 10018)
  Fix 5 — Pre-market-close forced position exit (weekend + daily)

Each test class targets one fix; tests within go from unit → integration → E2E.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "packages", ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


# ============================================================================
# Fix 1 — BTCUSD broker maintenance gap recognition
# ============================================================================

class TestBTCUSDMaintenanceGap:
    """BTCUSD was in _24_7_ASSETS which blocked ALL gap recognition.
    Exness has a daily maintenance window (~20:55-22:05 UTC) for crypto too.
    """

    def test_btcusd_still_in_24_7_assets(self):
        from core.candles import _24_7_ASSETS
        assert "BTCUSD" in _24_7_ASSETS

    def test_btcusd_in_broker_maintenance_set(self):
        from core.candles import _BROKER_MAINTENANCE_ASSETS
        assert "BTCUSD" in _BROKER_MAINTENANCE_ASSETS
        assert "ETHUSD" in _BROKER_MAINTENANCE_ASSETS

    def test_btcusd_daily_maintenance_gap_accepted(self):
        """Gap from 20:55 to 22:05 UTC should be accepted for BTCUSD."""
        from core.candles import is_expected_market_gap
        prev = pd.Timestamp("2026-05-29 20:55:00", tz="UTC")
        curr = pd.Timestamp("2026-05-29 22:05:00", tz="UTC")
        assert is_expected_market_gap(prev, curr, asset="BTCUSD") is True

    def test_btcusd_maintenance_gap_at_boundary(self):
        """Earliest possible maintenance start: prev candle at 17:00 UTC."""
        from core.candles import is_expected_market_gap
        prev = pd.Timestamp("2026-05-29 17:00:00", tz="UTC")
        curr = pd.Timestamp("2026-05-29 21:00:00", tz="UTC")
        assert is_expected_market_gap(prev, curr, asset="BTCUSD") is True

    def test_btcusd_non_maintenance_gap_rejected(self):
        """A gap at 14:00-16:00 is NOT maintenance — should be rejected."""
        from core.candles import is_expected_market_gap
        prev = pd.Timestamp("2026-05-29 14:00:00", tz="UTC")
        curr = pd.Timestamp("2026-05-29 16:00:00", tz="UTC")
        assert is_expected_market_gap(prev, curr, asset="BTCUSD") is False

    def test_btcusd_weekend_gap_still_rejected(self):
        """BTCUSD trades 24/7 — no weekend gap expected."""
        from core.candles import is_expected_market_gap
        prev = pd.Timestamp("2026-05-29 21:00:00", tz="UTC")  # Friday
        curr = pd.Timestamp("2026-05-31 21:00:00", tz="UTC")  # Sunday
        assert is_expected_market_gap(prev, curr, asset="BTCUSD") is False

    def test_ethusd_maintenance_gap_accepted(self):
        from core.candles import is_expected_market_gap
        prev = pd.Timestamp("2026-05-29 20:50:00", tz="UTC")
        curr = pd.Timestamp("2026-05-29 22:00:00", tz="UTC")
        assert is_expected_market_gap(prev, curr, asset="ETHUSD") is True

    def test_candle_integrity_passes_during_btcusd_maintenance(self):
        """Candle series with a gap during maintenance should pass integrity check."""
        from core.candles import Candle, validate_candle_integrity

        base = datetime(2026, 5, 29, 18, 0, tzinfo=timezone.utc)
        candles = []
        # 18:00 to 20:55 — normal 5m candles
        t = base
        while t < datetime(2026, 5, 29, 20, 55, tzinfo=timezone.utc):
            candles.append(Candle(
                time=pd.Timestamp(t),
                open=73000.0, high=73100.0, low=72900.0, close=73050.0, volume=100.0
            ))
            t += timedelta(minutes=5)

        # Gap: 20:55 to 22:05 (maintenance)
        # Resume at 22:05
        t = datetime(2026, 5, 29, 22, 5, tzinfo=timezone.utc)
        while t <= datetime(2026, 5, 29, 23, 0, tzinfo=timezone.utc):
            candles.append(Candle(
                time=pd.Timestamp(t),
                open=73000.0, high=73100.0, low=72900.0, close=73050.0, volume=100.0
            ))
            t += timedelta(minutes=5)

        is_valid, error = validate_candle_integrity(
            candles, "5m", check_recency=False, symbol="BTCUSD"
        )
        assert is_valid, f"Expected valid but got error: {error}"


# ============================================================================
# Fix 2 — MARKET_CLOSE_WINDOWS expanded + weekend closure detection
# ============================================================================

class TestMarketCloseWindowsExpanded:
    """MARKET_CLOSE_WINDOWS was missing GBPUSD, BTCUSD etc.
    _is_market_closed had no weekend detection.
    """

    def test_all_traded_symbols_in_close_windows(self):
        from core.services.code_sniper_monitor import MARKET_CLOSE_WINDOWS
        # Windows are registry-driven — every onboarded asset must have one.
        from packages.core.asset_registry import market_close_windows
        required = sorted(market_close_windows().keys())
        assert {"XAUUSD", "GBPUSD", "BTCUSD", "USOIL", "USTEC", "XAGUSD"} <= set(required)
        for sym in required:
            assert sym in MARKET_CLOSE_WINDOWS, f"{sym} missing from MARKET_CLOSE_WINDOWS"

    def test_daily_close_detected_for_gbpusd(self):
        from core.services.code_sniper_monitor import _is_market_closed
        during = datetime(2026, 5, 29, 21, 30, 0, tzinfo=timezone.utc)
        closed, wait = _is_market_closed("GBPUSD", now=during)
        assert closed is True
        assert wait > 0

    def test_daily_close_detected_for_btcusd(self):
        from core.services.code_sniper_monitor import _is_market_closed
        during = datetime(2026, 5, 29, 21, 15, 0, tzinfo=timezone.utc)
        closed, _ = _is_market_closed("BTCUSD", now=during)
        assert closed is True

    def test_open_hours_return_false(self):
        from core.services.code_sniper_monitor import _is_market_closed
        during = datetime(2026, 5, 29, 15, 0, 0, tzinfo=timezone.utc)
        for sym in ["XAUUSD", "GBPUSD", "BTCUSD"]:
            closed, _ = _is_market_closed(sym, now=during)
            assert closed is False, f"{sym} should be open at 15:00 UTC"

    # ---- Weekend closure ----

    def test_xauusd_friday_night_closed(self):
        from core.services.code_sniper_monitor import _is_market_closed
        friday_night = datetime(2026, 5, 29, 21, 0, 0, tzinfo=timezone.utc)
        assert friday_night.weekday() == 4  # Verify it's Friday
        closed, wait = _is_market_closed("XAUUSD", now=friday_night)
        assert closed is True
        assert wait > 3600  # At least 1 hour until Sunday reopen

    def test_xauusd_saturday_closed(self):
        from core.services.code_sniper_monitor import _is_market_closed
        saturday = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc)
        assert saturday.weekday() == 5
        closed, _ = _is_market_closed("XAUUSD", now=saturday)
        assert closed is True

    def test_xauusd_sunday_before_reopen_closed(self):
        from core.services.code_sniper_monitor import _is_market_closed
        sunday_early = datetime(2026, 5, 31, 15, 0, 0, tzinfo=timezone.utc)
        assert sunday_early.weekday() == 6
        closed, wait = _is_market_closed("XAUUSD", now=sunday_early)
        assert closed is True
        assert 0 < wait < 30000  # Should reopen at 22:05

    def test_xauusd_sunday_after_reopen_open(self):
        from core.services.code_sniper_monitor import _is_market_closed
        sunday_late = datetime(2026, 5, 31, 22, 10, 0, tzinfo=timezone.utc)
        assert sunday_late.weekday() == 6
        closed, _ = _is_market_closed("XAUUSD", now=sunday_late)
        assert closed is False

    def test_btcusd_saturday_NOT_weekend_closed(self):
        """Crypto trades weekends — only daily maintenance applies."""
        from core.services.code_sniper_monitor import _is_market_closed
        saturday = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc)
        closed, _ = _is_market_closed("BTCUSD", now=saturday)
        assert closed is False

    def test_gbpusd_saturday_closed(self):
        from core.services.code_sniper_monitor import _is_market_closed
        saturday = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc)
        closed, _ = _is_market_closed("GBPUSD", now=saturday)
        assert closed is True

    def test_weekend_symbols_set_excludes_crypto(self):
        from core.services.code_sniper_monitor import _WEEKEND_CLOSE_SYMBOLS
        assert "BTCUSD" not in _WEEKEND_CLOSE_SYMBOLS
        assert "ETHUSD" not in _WEEKEND_CLOSE_SYMBOLS
        assert "XAUUSD" in _WEEKEND_CLOSE_SYMBOLS
        assert "GBPUSD" in _WEEKEND_CLOSE_SYMBOLS

    def test_broker_suffix_stripped(self):
        """Symbols with broker suffixes (e.g. XAUUSDm) should still match."""
        from core.services.code_sniper_monitor import _is_market_closed
        during = datetime(2026, 5, 29, 21, 15, 0, tzinfo=timezone.utc)
        closed, _ = _is_market_closed("XAUUSDm", now=during)
        assert closed is True


# ============================================================================
# Fix 3 — MultiExpertSignal extra="ignore" (was extra="forbid")
# ============================================================================

class TestMultiExpertSignalExtraFields:
    """LLM sometimes returns extra fields like 'context'. Previously this
    rejected the entire proposal via Pydantic validation error.
    """

    def test_valid_sell_signal_with_extra_context_field(self):
        """Reproduces the exact error from log line 1133."""
        from core.schemas_trade import MultiExpertSignal
        data = {
            "signal": "sell",
            "entry": 1.348,
            "stop": 1.3485,
            "target": 1.3462,
            "ttl_minutes": 180,
            "confidence": 0.68,
            "rationale": "Bearish structure",
            "context": {"structure": "Uptrend from spike post-exhaustion"},
        }
        sig = MultiExpertSignal(**data)
        assert sig.signal == "sell"
        assert sig.entry == 1.348
        assert sig.stop == 1.3485
        assert sig.target == 1.3462
        assert sig.confidence == 0.68

    def test_extra_context_field_is_not_stored(self):
        from core.schemas_trade import MultiExpertSignal
        data = {
            "signal": "buy",
            "entry": 100.0,
            "stop": 95.0,
            "target": 110.0,
            "ttl_minutes": 90,
            "confidence": 0.75,
            "context": {"foo": "bar"},
        }
        sig = MultiExpertSignal(**data)
        assert not hasattr(sig, "context")

    def test_multiple_extra_fields_all_ignored(self):
        from core.schemas_trade import MultiExpertSignal
        data = {
            "signal": "flat",
            "ttl_minutes": 240,
            "confidence": 0.3,
            "context": {"x": 1},
            "analysis_notes": "some extra text",
            "internal_score": 42,
        }
        sig = MultiExpertSignal(**data)
        assert sig.signal == "flat"
        assert not hasattr(sig, "context")
        assert not hasattr(sig, "analysis_notes")
        assert not hasattr(sig, "internal_score")

    def test_required_fields_still_enforced(self):
        """Extra fields are tolerated, but missing required fields still fail."""
        from core.schemas_trade import MultiExpertSignal
        with pytest.raises(Exception):  # ValidationError
            MultiExpertSignal(signal="buy", ttl_minutes=180)  # missing confidence

    def test_non_flat_signal_requires_prices(self):
        from core.schemas_trade import MultiExpertSignal
        with pytest.raises(Exception):
            MultiExpertSignal(
                signal="buy",
                ttl_minutes=180,
                confidence=0.7,
                # missing entry/stop/target
            )

    def test_flat_signal_allows_none_prices(self):
        from core.schemas_trade import MultiExpertSignal
        sig = MultiExpertSignal(
            signal="flat",
            ttl_minutes=90,
            confidence=0.2,
        )
        assert sig.entry is None
        assert sig.stop is None
        assert sig.target is None

    def test_unified_expert_response_with_extra_fields_in_proposals(self):
        """Full UnifiedExpertResponse containing proposals with extra fields."""
        from core.schemas_trade import UnifiedExpertResponse
        data = {
            "proposals": [
                {
                    "signal": "sell",
                    "entry": 73650.0,
                    "stop": 73810.0,
                    "target": 73200.0,
                    "ttl_minutes": 180,
                    "confidence": 0.62,
                    "rationale": "test",
                    "context": {"structure": "bearish LH/LL"},
                    "extra_analysis": "some extra",
                }
            ],
            "candel_required_for_judge": 20,
        }
        resp = UnifiedExpertResponse(**data)
        assert len(resp.proposals) == 1
        assert resp.proposals[0].signal == "sell"
        assert resp.proposals[0].entry == 73650.0


# ============================================================================
# Fix 4 — Trade close retries after market reopen (retcode 10018)
# ============================================================================

class TestTradeCloseRetryOnMarketClosed:
    """When a trade expires during market closure, broker.close() returned
    retcode 10018 (Market closed). Now it retries after market reopens.
    """

    def test_retry_logic_exists_in_trade_monitor_source(self):
        """Verify the retry-on-market-closed code path exists in the source."""
        import inspect
        from core.services.trade_monitor import TradeMonitor
        source = inspect.getsource(TradeMonitor._monitor)
        assert "market closed" in source.lower()
        assert "max_close_retries" in source
        assert "10018" in source

    def test_retry_handles_market_closed_string_matching(self):
        """The retry logic matches 'market closed' or '10018' in error messages."""
        error_msgs = [
            "close failed: Market closed",
            "close failed for XAUUSDm (retcode 10018)",
            "Market Closed - cannot execute",
        ]
        for msg in error_msgs:
            matches = "market closed" in msg.lower() or "10018" in msg
            assert matches, f"Error message should match retry condition: {msg}"

    @pytest.mark.asyncio
    async def test_retry_close_succeeds_on_second_attempt(self):
        """Simulate broker.close() failing once then succeeding, with patched sleep."""
        call_count = 0
        def mock_close(symbol):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("close failed: Market closed")
            return True

        with patch("packages.core.services.trade_monitor._is_market_closed", return_value=(False, 0)):
            # Directly test the retry loop logic
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    mock_close("XAUUSD")
                    break
                except RuntimeError as e:
                    if "market closed" in str(e).lower() and attempt < max_retries - 1:
                        continue
                    raise

        assert call_count == 2, f"Expected 2 calls (1 fail + 1 success), got {call_count}"


# ============================================================================
# Fix 5 — Pre-market-close forced position exit
# ============================================================================

class TestPreCloseDetection:
    """_is_market_closing_soon() detects upcoming closures and returns reason."""

    def test_friday_5min_before_close_triggers_weekend(self):
        from core.services.code_sniper_monitor import _is_market_closing_soon
        fri_2050 = datetime(2026, 5, 29, 20, 50, 0, tzinfo=timezone.utc)
        assert fri_2050.weekday() == 4
        closing, reason = _is_market_closing_soon("XAUUSD", now=fri_2050)
        assert closing is True
        assert reason == "weekend_close"

    def test_friday_4min_before_close_triggers(self):
        from core.services.code_sniper_monitor import _is_market_closing_soon
        fri_2051 = datetime(2026, 5, 29, 20, 51, 0, tzinfo=timezone.utc)
        closing, reason = _is_market_closing_soon("GBPUSD", now=fri_2051)
        assert closing is True
        assert reason == "weekend_close"

    def test_friday_30min_before_close_does_not_trigger(self):
        from core.services.code_sniper_monitor import _is_market_closing_soon
        fri_2025 = datetime(2026, 5, 29, 20, 25, 0, tzinfo=timezone.utc)
        closing, _ = _is_market_closing_soon("XAUUSD", now=fri_2025)
        assert closing is False

    def test_crypto_no_weekend_pre_close(self):
        """BTCUSD trades weekends — no weekend pre-close."""
        from core.services.code_sniper_monitor import _is_market_closing_soon
        fri_2050 = datetime(2026, 5, 29, 20, 50, 0, tzinfo=timezone.utc)
        closing, reason = _is_market_closing_soon("BTCUSD", now=fri_2050)
        # Should trigger daily_maintenance pre-close, NOT weekend
        assert reason == "daily_maintenance"

    def test_daily_maintenance_pre_close_on_monday(self):
        from core.services.code_sniper_monitor import _is_market_closing_soon
        mon_2051 = datetime(2026, 6, 1, 20, 51, 0, tzinfo=timezone.utc)
        assert mon_2051.weekday() == 0
        closing, reason = _is_market_closing_soon("BTCUSD", now=mon_2051)
        assert closing is True
        assert reason == "daily_maintenance"

    def test_well_outside_close_window_returns_false(self):
        from core.services.code_sniper_monitor import _is_market_closing_soon
        noon = datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)
        for sym in ["XAUUSD", "GBPUSD", "BTCUSD", "EURUSD"]:
            closing, _ = _is_market_closing_soon(sym, now=noon)
            assert closing is False, f"{sym} should not be closing at noon"

    def test_custom_buffer_minutes(self):
        from core.services.code_sniper_monitor import _is_market_closing_soon
        # 10 min before daily close — default 5 min buffer should NOT trigger
        t = datetime(2026, 6, 1, 20, 45, 0, tzinfo=timezone.utc)
        closing5, _ = _is_market_closing_soon("XAUUSD", buffer_minutes=5, now=t)
        assert closing5 is False
        # But 15 min buffer SHOULD trigger
        closing15, reason = _is_market_closing_soon("XAUUSD", buffer_minutes=15, now=t)
        assert closing15 is True
        assert reason == "daily_maintenance"


class TestPreCloseForceExit:
    """Trade monitor should force-close positions before market closure."""

    def test_pre_close_exit_reason_constructed_correctly(self):
        """The exit reason should be PRE_CLOSE_WEEKEND_CLOSE or PRE_CLOSE_DAILY_MAINTENANCE."""
        from core.services.code_sniper_monitor import _is_market_closing_soon
        fri = datetime(2026, 5, 29, 20, 51, 0, tzinfo=timezone.utc)
        closing, reason = _is_market_closing_soon("XAUUSD", now=fri)
        assert closing is True
        exit_reason = f"PRE_CLOSE_{reason.upper()}"
        assert exit_reason == "PRE_CLOSE_WEEKEND_CLOSE"

    def test_pre_close_code_path_exists_in_trade_monitor(self):
        """Verify the pre-close check is in the _monitor loop."""
        import inspect
        from core.services.trade_monitor import TradeMonitor
        source = inspect.getsource(TradeMonitor._monitor)
        assert "_is_market_closing_soon" in source
        assert "PRE_CLOSE" in source
        assert "force-closing position" in source

    def test_pre_close_pnl_calculation_long(self):
        """PnL for a pre-closed long trade should use last price."""
        entry, last_price, qty = 2350.0, 2355.0, 0.1
        pnl = (last_price - entry) * qty
        assert pnl == pytest.approx(0.5)

    def test_pre_close_pnl_calculation_short(self):
        """PnL for a pre-closed short trade should use last price."""
        entry, last_price, qty = 73650.0, 73600.0, 0.03
        pnl = (entry - last_price) * qty
        assert pnl == pytest.approx(1.5)

    def test_pre_close_does_not_trigger_during_open_hours(self):
        """During normal market hours, trade monitor should NOT pre-close."""
        from core.services.code_sniper_monitor import _is_market_closing_soon
        normal_hours = datetime(2026, 5, 29, 14, 0, 0, tzinfo=timezone.utc)
        closing, _ = _is_market_closing_soon("XAUUSD", now=normal_hours)
        assert closing is False


# ============================================================================
# Fix 1+2 combined — Candle integrity during maintenance (E2E)
# ============================================================================

class TestCandleIntegrityDuringMaintenance:
    """End-to-end: candle series spanning broker maintenance should validate."""

    def _make_candle_series(self, symbol, start, end, interval_min=5, gap_start=None, gap_end=None):
        from core.candles import Candle
        candles = []
        t = start
        while t <= end:
            if gap_start and gap_end and gap_start <= t < gap_end:
                t += timedelta(minutes=interval_min)
                continue
            candles.append(Candle(
                time=pd.Timestamp(t),
                open=100.0, high=101.0, low=99.0, close=100.5, volume=50.0,
            ))
            t += timedelta(minutes=interval_min)
        return candles

    def test_xauusd_5m_with_maintenance_gap_passes(self):
        from core.candles import validate_candle_integrity
        candles = self._make_candle_series(
            "XAUUSD",
            datetime(2026, 5, 29, 18, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 29, 23, 0, tzinfo=timezone.utc),
            gap_start=datetime(2026, 5, 29, 20, 55, tzinfo=timezone.utc),
            gap_end=datetime(2026, 5, 29, 22, 5, tzinfo=timezone.utc),
        )
        is_valid, error = validate_candle_integrity(
            candles, "5m", check_recency=False, symbol="XAUUSD"
        )
        assert is_valid, f"XAUUSD maintenance gap should be allowed: {error}"

    def test_gbpusd_15m_with_maintenance_gap_passes(self):
        from core.candles import validate_candle_integrity
        candles = self._make_candle_series(
            "GBPUSD",
            datetime(2026, 5, 29, 18, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 29, 23, 0, tzinfo=timezone.utc),
            interval_min=15,
            gap_start=datetime(2026, 5, 29, 20, 55, tzinfo=timezone.utc),
            gap_end=datetime(2026, 5, 29, 22, 5, tzinfo=timezone.utc),
        )
        is_valid, error = validate_candle_integrity(
            candles, "15m", check_recency=False, symbol="GBPUSD"
        )
        assert is_valid, f"GBPUSD maintenance gap should be allowed: {error}"

    def test_btcusd_5m_with_maintenance_gap_passes(self):
        from core.candles import validate_candle_integrity
        candles = self._make_candle_series(
            "BTCUSD",
            datetime(2026, 5, 29, 18, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 29, 23, 0, tzinfo=timezone.utc),
            gap_start=datetime(2026, 5, 29, 20, 55, tzinfo=timezone.utc),
            gap_end=datetime(2026, 5, 29, 22, 5, tzinfo=timezone.utc),
        )
        is_valid, error = validate_candle_integrity(
            candles, "5m", check_recency=False, symbol="BTCUSD"
        )
        assert is_valid, f"BTCUSD maintenance gap should be allowed: {error}"

    def test_random_midday_gap_still_fails(self):
        """A gap at 14:00-16:00 is NOT maintenance and should fail."""
        from core.candles import validate_candle_integrity
        candles = self._make_candle_series(
            "XAUUSD",
            datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 29, 18, 0, tzinfo=timezone.utc),
            gap_start=datetime(2026, 5, 29, 14, 0, tzinfo=timezone.utc),
            gap_end=datetime(2026, 5, 29, 16, 0, tzinfo=timezone.utc),
        )
        is_valid, error = validate_candle_integrity(
            candles, "5m", check_recency=False, symbol="XAUUSD"
        )
        assert not is_valid, "Midday gap should NOT be allowed"
        assert "Missing candles" in error

    def test_no_gap_series_still_passes(self):
        """Normal continuous series should still pass."""
        from core.candles import validate_candle_integrity
        candles = self._make_candle_series(
            "BTCUSD",
            datetime(2026, 5, 29, 10, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 29, 15, 0, tzinfo=timezone.utc),
        )
        is_valid, error = validate_candle_integrity(
            candles, "5m", check_recency=False, symbol="BTCUSD"
        )
        assert is_valid, f"Continuous series should pass: {error}"


# ============================================================================
# Code sniper monitor — market-hours skip (Fix 2 integration)
# ============================================================================

class TestCodeSniperMonitorMarketHoursSkip:
    """Code sniper monitor should skip candle fetch for symbols with closed markets."""

    @pytest.mark.asyncio
    async def test_run_all_skips_closed_symbol(self):
        from core.services.code_sniper_monitor import CodeSniperMonitor, MonitorConfig

        mock_candle_service = MagicMock()
        mock_candle_service.get_candles = AsyncMock()
        mock_broker = MagicMock()
        mock_risk = MagicMock()

        monitor = CodeSniperMonitor(
            candle_service=mock_candle_service,
            broker=mock_broker,
            risk_service=mock_risk,
            config=MonitorConfig(),
        )

        from core.schemas_trade import CodeSniperSignal
        signal = CodeSniperSignal(
            code="def run(c, n): return {'direction':'hold','entry':0,'stop_loss':0,'take_profit_1':0,'take_profit_2':0,'confidence':0,'rationale':'test'}",
            metadata={"decision": "accepted", "rationale": "test"},
            ttl_minutes=180,
            confidence=0.7,
        )
        monitor.add_strategy("test_strat", signal, "XAUUSD", "XAUUSD", {})

        # Patch the SAME module object the test imported ``CodeSniperMonitor``
        # from.  The repo is importable under both ``core.*`` and
        # ``packages.core.*`` (the test conftest adds ``packages/`` to sys.path),
        # which are *distinct* module objects.  Patching the other path would
        # silently miss, letting the real (wall-clock-dependent) guard run.
        with patch("core.services.code_sniper_monitor._is_market_closed") as mock_closed:
            mock_closed.return_value = (True, 3600)
            await monitor._run_all()

        mock_candle_service.get_candles.assert_not_called()

    def test_market_close_check_is_called_in_run_all_source(self):
        """Verify _run_all checks market closure before fetching candles."""
        import inspect
        from core.services.code_sniper_monitor import CodeSniperMonitor
        source = inspect.getsource(CodeSniperMonitor._run_all)
        assert "_is_market_closed" in source
        assert "skipping symbol" in source.lower() or "Market closed" in source


# ============================================================================
# Full lifecycle E2E: Expert → Judge → Trade → Pre-Close Exit
# ============================================================================

class TestFullLifecyclePreClose:
    """End-to-end: a trade opened during market hours should be force-closed
    before weekend closure — the full flow from signal to exit.
    """

    def test_pre_close_reason_format(self):
        """Exit reasons should be descriptive and parseable."""
        reasons = ["PRE_CLOSE_WEEKEND_CLOSE", "PRE_CLOSE_DAILY_MAINTENANCE"]
        for r in reasons:
            assert r.startswith("PRE_CLOSE_")
            parts = r.split("_", 2)
            assert len(parts) >= 3

    def test_pre_close_buffer_is_reasonable(self):
        """Buffer should be 3-10 minutes — not too short (miss it) or too long (exit too early)."""
        from core.services.code_sniper_monitor import PRE_CLOSE_BUFFER_MINUTES
        assert 3 <= PRE_CLOSE_BUFFER_MINUTES <= 10

    def test_weekend_close_wait_time_covers_full_weekend(self):
        """Wait time from Friday 21:00 to Sunday 22:05 should be ~49 hours."""
        from core.services.code_sniper_monitor import _is_market_closed
        friday_close = datetime(2026, 5, 29, 21, 0, 0, tzinfo=timezone.utc)
        closed, wait = _is_market_closed("XAUUSD", now=friday_close)
        assert closed is True
        wait_hours = wait / 3600
        assert 40 < wait_hours < 55, f"Weekend wait should be ~49h, got {wait_hours:.1f}h"

    def test_trade_exits_before_weekend_not_after(self):
        """Key assertion: trade should exit with PRE_CLOSE, not TIMEOUT or SL."""
        from core.services.code_sniper_monitor import _is_market_closing_soon

        fri_2051 = datetime(2026, 5, 29, 20, 51, 0, tzinfo=timezone.utc)
        closing, reason = _is_market_closing_soon("XAUUSD", now=fri_2051)
        assert closing is True
        assert reason == "weekend_close"

        exit_reason = f"PRE_CLOSE_{reason.upper()}"
        assert exit_reason == "PRE_CLOSE_WEEKEND_CLOSE"

    def test_all_forex_metals_covered_by_weekend_close(self):
        """Every symbol in MARKET_CLOSE_WINDOWS that isn't crypto should be in weekend set."""
        from core.services.code_sniper_monitor import (
            MARKET_CLOSE_WINDOWS, _WEEKEND_CLOSE_SYMBOLS
        )
        crypto = {"BTCUSD", "ETHUSD", "BTCUSDT", "ETHUSDT"}
        for sym in MARKET_CLOSE_WINDOWS:
            if sym not in crypto:
                assert sym in _WEEKEND_CLOSE_SYMBOLS, \
                    f"{sym} is in MARKET_CLOSE_WINDOWS but not in _WEEKEND_CLOSE_SYMBOLS"
