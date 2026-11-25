"""C4 / H1 / H2 / H3 / L1 — the trade-triggering path defends itself.

Before 2026-08-27 the live path executed judge code against whatever candles it was
handed: no consecutiveness check, no alignment check, missing prices silently
becoming 0.0, stale bars logged and then traded on, and no guard against evaluating
the same bar twice. The backtest had most of that; the path risking real money did
not.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from packages.core.strategy_candle_gate import (
    CandleContractError,
    require_real_prices,
    validate_strategy_window,
)

T0 = datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc)


def bars(n: int, start: datetime = T0, step_min: int = 5):
    return [{
        "time": start + timedelta(minutes=step_min * i),
        "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.2, "volume": 10,
    } for i in range(n)]


class TestPricesMustBeReal:
    @pytest.mark.parametrize("field", ["open", "high", "low", "close"])
    def test_missing_price_raises_instead_of_becoming_zero(self, field):
        candle = bars(1)[0]
        candle[field] = None
        with pytest.raises(CandleContractError, match=field):
            require_real_prices(candle, "candle")

    @pytest.mark.parametrize("bad", [0, 0.0, -1.0])
    def test_non_positive_price_is_not_a_price(self, bad):
        candle = bars(1)[0]
        candle["low"] = bad
        with pytest.raises(CandleContractError):
            require_real_prices(candle, "candle")

    def test_high_below_low_is_rejected(self):
        candle = bars(1)[0]
        candle["high"], candle["low"] = 99.0, 101.0
        with pytest.raises(CandleContractError, match="below low"):
            require_real_prices(candle, "candle")

    def test_a_good_candle_passes(self):
        assert require_real_prices(bars(1)[0], "candle")["close"] == 100.2


class TestWindowMustBeContinuous:
    def test_clean_window_passes(self):
        w = bars(41)
        validate_strategy_window(w[:-1], w[-1], asset="XAUUSD", expected_history=40)

    def test_a_hole_in_the_window_is_rejected(self):
        w = bars(41)
        del w[20]                       # 10-minute step across the hole
        with pytest.raises(CandleContractError, match="non-consecutive"):
            validate_strategy_window(w[:-1], w[-1], asset="XAUUSD")

    def test_a_repeated_bar_is_rejected(self):
        """Zero or negative step: the same bar delivered twice, or time running back."""
        w = bars(10)
        w[6]["time"] = w[5]["time"]
        with pytest.raises(CandleContractError, match="out of order"):
            validate_strategy_window(w[:-1], w[-1], asset="XAUUSD")

    def test_time_running_backwards_is_rejected(self):
        w = bars(10)
        w[6]["time"] = w[3]["time"]
        with pytest.raises(CandleContractError, match="out of order"):
            validate_strategy_window(w[:-1], w[-1], asset="XAUUSD")

    def test_weekend_gap_is_allowed(self):
        """A Friday-to-Sunday break is supposed to be there."""
        friday = datetime(2026, 7, 17, 20, 45, tzinfo=timezone.utc)
        w = bars(3, start=friday) + bars(3, start=datetime(2026, 7, 19, 22, 0, tzinfo=timezone.utc))
        validate_strategy_window(w[:-1], w[-1], asset="XAUUSD")

    def test_wrong_history_length_is_rejected(self):
        w = bars(30)
        with pytest.raises(CandleContractError, match="expected 40"):
            validate_strategy_window(w[:-1], w[-1], asset="XAUUSD", expected_history=40)


class TestStalenessIsRejectedNotLogged:
    def test_stale_window_raises(self):
        w = bars(41)
        now = w[-1]["time"] + timedelta(minutes=30)
        with pytest.raises(CandleContractError, match="old"):
            validate_strategy_window(w[:-1], w[-1], now=now, max_staleness_minutes=10)

    def test_fresh_window_passes(self):
        w = bars(41)
        now = w[-1]["time"] + timedelta(minutes=5)
        validate_strategy_window(w[:-1], w[-1], now=now, max_staleness_minutes=10)

    def test_forming_bar_is_rejected(self):
        w = bars(41)
        now = w[-1]["time"] + timedelta(minutes=3)
        with pytest.raises(CandleContractError, match="has not closed"):
            validate_strategy_window(w[:-1], w[-1], now=now, max_staleness_minutes=10)

    def test_backtest_passes_no_clock_so_age_is_never_checked(self):
        """A replayed bar is old on purpose."""
        w = bars(41, start=datetime(2024, 1, 2, tzinfo=timezone.utc))
        validate_strategy_window(w[:-1], w[-1])         # no now= / no threshold


class TestLivePathIsWired:
    def _src(self, path: str) -> str:
        return Path(path).read_text(encoding="utf-8")

    def test_executor_gates_the_window_before_the_sandbox(self):
        src = self._src("packages/core/code_sniper_executor.py")
        gate = src.index("validate_strategy_window(")
        sandbox = src.index("run_in_sandbox(")
        assert gate < sandbox, "the sandbox runs before the candles are validated"

    def test_executor_no_longer_invents_zero_prices(self):
        src = self._src("packages/core/code_sniper_executor.py")
        assert 'float(candle.get("high") or 0)' not in src
        assert "return float(v) if v is not None else 0.0" not in src

    def test_missing_levels_default_to_none_not_zero(self):
        """L1: a take_profit_2 of 0 used to drag a SELL's TP1 to within ticks of entry."""
        src = self._src("packages/core/code_sniper_executor.py")
        assert 'result.get("take_profit_2", 0)' not in src
        assert 'result.get("entry", 0)' not in src
        assert "_tp2 = _tp1" in src, "TP2 must fall back to TP1, as the backtest does"

    def test_monitor_rejects_stale_candles(self):
        src = self._src("packages/core/services/code_sniper_monitor.py")
        assert "raise CandleContractError(" in src
        assert 'logger.warning("Stale candle data detected"' not in src

    def test_bad_candles_do_not_trip_the_strategy_circuit_breaker(self):
        """Three gappy bars from the feed must not retire a working judge program."""
        src = self._src("packages/core/services/code_sniper_monitor.py")
        i = src.index("except CandleContractError as e:")
        j = src.index("except asyncio.TimeoutError:", i)
        assert "failure_count" not in src[i:j]


class TestTradeMonitorBarHandling:
    def _src(self) -> str:
        return Path("packages/core/services/trade_monitor.py").read_text(encoding="utf-8")

    def test_both_fetch_paths_have_a_duplicate_guard(self):
        """H2: the monitor has two bar sources; both must refuse a repeat."""
        assert self._src().count("last_candle_time and bar_time and bar_time <= last_candle_time:") == 2
        assert "if not is_quote and last_candle_time" in self._src()

    def test_service_fetch_waits_for_the_broker_to_finalise(self):
        src = self._src()
        assert "MONITOR_CANDLE_BUFFER_SEC" in src
        wait = src.index('await_next_bar_close("5m", "UTC")')
        buf = src.index("await asyncio.sleep(MONITOR_CANDLE_BUFFER_SEC)")
        assert wait < buf, "the buffer must follow the bar-close wait"


class TestRunnerDoesNotTradeOnNothing:
    def test_conversion_failure_skips_the_cycle(self):
        """H3: it used to substitute [] and still call the model."""
        src = Path("apps/backend/agents/runner.py").read_text(encoding="utf-8")
        assert 'c5_json, c15_json = "[]", "[]"' not in src

    def test_daily_guards_are_fed_money_not_price_points(self):
        """M3: a full stop is ~12.0 on gold and ~0.0012 on EURUSD."""
        src = Path("apps/backend/agents/runner.py").read_text(encoding="utf-8")
        i = src.index("self._daily_guards.register_trade(pnl)")
        window = src[max(0, i - 1200):i]
        assert "_usd_pnl(" in window
        assert "pnl = exit_price - entry" not in window
