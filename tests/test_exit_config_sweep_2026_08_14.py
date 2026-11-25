"""Exit-config sweep simulator.

The sweep is only worth running if its replay reproduces reality, so these pin
the mechanics the calibration gate depends on: stop-outs land at -1R, the TP1
partial is booked at half, the min-lot throttle suppresses that partial, TTL
closes at market, and an unsimulable entry returns ``None`` rather than a
silent zero (which would drag every config toward break-even).
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from packages.core.candles import Candle

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "audit" / "exit_config_sweep.py"
_spec = importlib.util.spec_from_file_location("exit_config_sweep", _PATH)
sweep = importlib.util.module_from_spec(_spec)
sys.modules["exit_config_sweep"] = sweep
_spec.loader.exec_module(sweep)

T0 = datetime(2026, 7, 13, tzinfo=timezone.utc)
BASE = sweep.ExitConfig("baseline", 120, False, False)


def bar(minute: int, o: float, h: float, lo: float, c: float) -> Candle:
    return Candle(
        time=pd.Timestamp(T0 + timedelta(minutes=minute)),
        open=o, high=h, low=lo, close=c, volume=1.0,
    )


def entry(qty: float = 0.06, side: str = "buy") -> "sweep.Entry":
    if side == "buy":
        return sweep.Entry(T0, "XAUUSD", "buy", 100.0, 99.0, 102.0, qty, 0.0, 1.0)
    return sweep.Entry(T0, "XAUUSD", "sell", 100.0, 101.0, 98.0, qty, 0.0, 1.0)


FLAT = [bar(i, 100, 100.1, 99.9, 100.0) for i in range(0, 60, 5)]
DOWN = [bar(0, 100, 100.2, 99.9, 100.0)] + [bar(i, 99.5, 99.6, 98.5, 98.8) for i in range(5, 30, 5)]
UP = [bar(0, 100, 100.2, 99.9, 100.1)] + [bar(i, 101, 102.5, 100.9, 102.2) for i in range(5, 30, 5)]


def test_stop_out_lands_at_minus_one_r():
    assert sweep.simulate(entry(), DOWN, BASE) == pytest.approx(-1.0, abs=1e-6)


def test_tp1_books_half_and_the_rest_rides():
    # half at TP1 (+2.0R), half marked to the last close (+2.2R) -> 2.1R
    assert sweep.simulate(entry(qty=0.06), UP, BASE) == pytest.approx(2.1, abs=1e-6)


def test_min_lot_throttles_the_partial():
    # At 0.01 the position cannot be split, so nothing is booked at TP1 and the
    # whole position rides. This is the SP-03 limiter, and the difference from
    # the test above is exactly what the sweep exists to price.
    assert sweep.simulate(entry(qty=0.01), UP, BASE) == pytest.approx(2.2, abs=1e-6)
    assert entry(qty=0.01).can_split is False
    assert entry(qty=0.06).can_split is True


def test_ttl_closes_at_market_on_the_deadline_bar():
    ttl10 = sweep.ExitConfig("ttl10", 10, False, False)
    assert sweep.simulate(entry(), FLAT, ttl10) == pytest.approx(0.0, abs=1e-6)


def test_unlimited_ttl_marks_to_the_last_close():
    unlimited = sweep.ExitConfig("none", None, False, False)
    assert sweep.simulate(entry(), FLAT, unlimited) == pytest.approx(0.0, abs=1e-6)


def test_longer_ttl_can_change_the_outcome():
    """A trade flat at 10 minutes but resolved by 60 — the whole TTL thesis."""
    late = FLAT[:3] + [bar(i, 101, 102.5, 100.9, 102.2) for i in range(15, 60, 5)]
    short_ttl = sweep.simulate(entry(), late, sweep.ExitConfig("t10", 10, False, False))
    long_ttl = sweep.simulate(entry(), late, sweep.ExitConfig("t240", 240, False, False))
    assert short_ttl < long_ttl


def test_short_side_is_mirrored():
    down_hard = [bar(0, 100, 100.1, 99.9, 100.0)] + [
        bar(i, 99, 99.1, 97.5, 97.8) for i in range(5, 30, 5)
    ]
    assert sweep.simulate(entry(side="sell"), down_hard, BASE) > 1.5


def test_no_bars_returns_none_not_zero():
    assert sweep.simulate(entry(), [], BASE) is None


def test_zero_risk_returns_none():
    bad = sweep.Entry(T0, "XAUUSD", "buy", 100.0, 100.0, 102.0, 0.06, 0.0, None)
    assert sweep.simulate(bad, UP, BASE) is None


def test_calibration_rejects_a_decorrelated_simulator():
    entries = [sweep.Entry(T0, "X", "buy", 100.0, 99.0, 102.0, 0.06, float(i % 5) - 2, None)
               for i in range(30)]
    rows = [(e, -e.real_r) for e in entries]      # perfectly anti-correlated
    ok, detail = sweep.calibrate(rows)
    assert ok is False
    assert "corr=" in detail


def test_calibration_accepts_a_faithful_simulator():
    entries = [sweep.Entry(T0, "X", "buy", 100.0, 99.0, 102.0, 0.06, float(i % 7) - 3, None)
               for i in range(40)]
    rows = [(e, e.real_r) for e in entries]       # exact reproduction
    ok, _ = sweep.calibrate(rows)
    assert ok is True


def test_calibration_refuses_a_thin_sample():
    entries = [sweep.Entry(T0, "X", "buy", 100.0, 99.0, 102.0, 0.06, 1.0, None) for _ in range(5)]
    ok, detail = sweep.calibrate([(e, e.real_r) for e in entries])
    assert ok is False
    assert "too few" in detail
