"""Backtest ⇄ live parity pins (2026-08-07).

Each test here fails if the backtest engine drifts back to a DIFFERENT
definition of the same thing than live uses. They are deliberately written
against the live constant / live function rather than a hardcoded expectation,
so a future change to live drags the backtest with it instead of silently
splitting the two apart again.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from packages.core import config as cfg
from packages.core.backtest_account import (
    TP1_PARTIAL_FRACTION,
    PositionSimulator,
    build_rules,
    commission_for,
    contract_size_for,
    size_position,
    usd_pnl,
)
from packages.core.services.trade_monitor import _usd_pnl as live_usd_pnl
from packages.core.trade_sim import simulate_trade


# ══════════════════════════════════════════════════════════════════════════
# P&L definition
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("asset", ["XAUUSD", "XAGUSD", "GBPUSD", "EURUSD", "BTCUSD", "USTEC"])
def test_backtest_pnl_matches_live_pnl(asset):
    """The backtest's money formula IS the live one, for every instrument.

    The bug this pins: the backtest used pip_value_per_lot, which is 1.0 for the
    metals while a lot is 100 oz of gold / 5,000 oz of silver — so gold P&L came
    out 100x and silver 5,000x too small.
    """
    entry, exit_price, qty = 100.0, 101.0, 0.03
    assert usd_pnl(asset, "long", entry, exit_price, qty) == pytest.approx(
        live_usd_pnl(asset, "long", entry, exit_price, qty)
    )
    assert usd_pnl(asset, "short", entry, exit_price, qty) == pytest.approx(
        live_usd_pnl(asset, "short", entry, exit_price, qty)
    )


def test_metal_contract_sizes_are_not_one():
    """Guards the specific regression: a metal silently priced like BTC."""
    assert contract_size_for("XAUUSD") == 100.0
    assert contract_size_for("XAGUSD") == 5000.0


# ══════════════════════════════════════════════════════════════════════════
# Position sizing — live formula + LP-01 risk-budget gate
# ══════════════════════════════════════════════════════════════════════════

def test_sizing_respects_the_risk_budget():
    """A normal setup sizes to (approximately) the intended risk, not a fixed lot."""
    r = size_position("XAUUSD", 1000.0, 4137.0, 4127.0, 0.025)
    assert r.tradeable
    # Floored to lot_step, so realised risk is <= intended plus min_lot effects.
    assert r.implied_risk <= r.intended_risk * cfg.RISK_BUDGET_MAX_OVERSHOOT_MULT
    assert r.lot > 0


def test_risk_budget_gate_skips_min_lot_overshoot():
    """LP-01: a min-lot position risking far more than the budget is REFUSED.

    Reproduces the measured live case — silver at a ~$390 balance, where min_lot
    implies several times the intended risk.
    """
    r = size_position("XAGUSD", 390.0, 59.7, 59.0, 0.015)
    assert not r.tradeable
    assert r.reason == "risk_budget_overshoot"
    assert r.overshoot > cfg.RISK_BUDGET_MAX_OVERSHOOT_MULT


def test_risk_budget_gate_is_account_size_adaptive():
    """The same instrument+stop is admitted once the account can afford it.

    This is the deliberate design of the live gate: it never names an asset, it
    re-admits automatically as the balance grows.
    """
    small = size_position("XAGUSD", 390.0, 59.7, 59.0, 0.015)
    large = size_position("XAGUSD", 20000.0, 59.7, 59.0, 0.015)
    assert not small.tradeable
    assert large.tradeable


def test_sizing_uses_the_live_risk_default():
    assert cfg.BT_RISK_PCT == cfg.CS_DEFAULT_RISK_PCT


# ══════════════════════════════════════════════════════════════════════════
# Exit ladder — TP1 partial, TP2 partial, runner
# ══════════════════════════════════════════════════════════════════════════

def _bar(o, h, l, c):
    return dict(open=o, high=h, low=l, close=c)


def test_tp1_books_half_and_moves_stop_to_be_plus_half_r():
    state = {
        "direction": "long", "entry": 100.0, "sl": 90.0, "initial_stop": 90.0,
        "sl_moved": False, "tp1_hit": False, "tp2_hit": False,
    }
    rules = build_rules(110.0, 130.0, entry=100.0, stop_loss=90.0, atr_1h=5.0)
    sim = PositionSimulator("BTCUSD", state, rules, quantity=1.0)

    sim.step(close=110.0, high=111.0, low=105.0, when=None)

    assert state["tp1_hit"] is True
    # Half booked at TP1 -> 10 points on 0.5 lots on a contract size of 1.
    assert sim.open_qty == pytest.approx(1.0 - TP1_PARTIAL_FRACTION)
    assert sim.result.tp1_pnl == pytest.approx(10.0 * TP1_PARTIAL_FRACTION)
    # BE + 0.5R on a 10-point risk.
    assert state["sl"] == pytest.approx(105.0)


def test_tp2_books_the_configured_fraction_and_keeps_a_runner():
    state = {
        "direction": "long", "entry": 100.0, "sl": 90.0, "initial_stop": 90.0,
        "sl_moved": False, "tp1_hit": False, "tp2_hit": False,
    }
    rules = build_rules(110.0, 130.0, entry=100.0, stop_loss=90.0, atr_1h=5.0)
    sim = PositionSimulator("BTCUSD", state, rules, quantity=1.0)

    sim.step(close=110.0, high=111.0, low=105.0, when=None)   # TP1
    remaining_after_tp1 = sim.open_qty
    sim.step(close=130.0, high=131.0, low=125.0, when=None)   # TP2

    assert state["tp2_hit"] is True
    assert not sim.result.closed, "TP2 must leave a runner, not close the position"
    expected_booked = remaining_after_tp1 * cfg.TP2_PARTIAL_FRACTION
    assert sim.open_qty == pytest.approx(remaining_after_tp1 - expected_booked)


def test_runner_survives_to_a_trailed_exit_and_all_legs_are_counted():
    state = {
        "direction": "long", "entry": 100.0, "sl": 90.0, "initial_stop": 90.0,
        "sl_moved": False, "tp1_hit": False, "tp2_hit": False,
    }
    rules = build_rules(110.0, 130.0, entry=100.0, stop_loss=90.0, atr_1h=5.0)
    sim = PositionSimulator("BTCUSD", state, rules, quantity=1.0)

    sim.step(close=110.0, high=111.0, low=105.0, when=None)   # TP1 partial
    sim.step(close=130.0, high=131.0, low=125.0, when=None)   # TP2 partial
    for _ in range(6):                                        # runner ratchets up
        sim.step(close=140.0, high=141.0, low=135.0, when=None)
    # Collapse into the trailed stop.
    for _ in range(6):
        sim.step(close=100.0, high=141.0, low=100.0, when=None)
        if sim.result.closed:
            break

    assert sim.result.closed
    reasons = [leg.reason for leg in sim.result.legs]
    assert "TP1_PARTIAL" in reasons and "TP2_PARTIAL" in reasons
    # Total P&L is the sum of every leg, not just the last one.
    assert sim.result.pnl == pytest.approx(sum(leg.pnl for leg in sim.result.legs))
    assert sim.result.pnl > 0


def test_simulate_trade_uses_the_judges_tp1_not_a_midpoint():
    """A pred carrying tp1/tp2 must book at TP1, not at (entry+target)/2."""
    idx = [datetime(2024, 1, 1, 9, 0, tzinfo=timezone.utc) + timedelta(minutes=5 * i)
           for i in range(4)]
    fut = pd.DataFrame(
        [_bar(100, 106, 99, 105), _bar(105, 106, 104, 105),
         _bar(105, 106, 104, 105), _bar(105, 106, 104, 105)],
        index=idx,
    )
    pred = {
        "direction": "long", "pattern": "code_sniper",
        "entry": 100.0, "stop": 95.0,
        "tp1": 106.0, "tp2": 130.0, "target": 130.0,
    }
    out = simulate_trade(pred, fut, asset="BTCUSD", lot_size=1.0)
    # TP1 at 106 is inside the first bar's range; the midpoint of 100->130 (115)
    # is not. A booked TP1 leg therefore proves the judge's own level was used.
    assert out["tp1_points"] > 0


# ══════════════════════════════════════════════════════════════════════════
# Config parity: no second copy of a live number
# ══════════════════════════════════════════════════════════════════════════

def test_backtest_ttl_default_is_a_legal_live_ttl():
    assert cfg.BT_TTL_DEFAULT_MINUTES == cfg.TTL_DEFAULT_MINUTES
    assert cfg.BT_TTL_DEFAULT_MINUTES in cfg.TTL_MINUTES_ALLOWED


def test_backtest_timeout_tracks_the_live_monitor_expiry():
    """Pins against CSM_TRADE_EXPIRY_HOURS — the value a FRESH live trade is

    actually given (services/code_sniper_monitor.py:1085, MonitorConfig
    .trade_expiry_hours). TRADE_MONITOR_EXPIRY_HOURS is only the crash-recovery
    fallback used when a persisted monitor doc has no expiry of its own
    (services/trade_monitor.py:435-439,489) — pinning against it previously let
    the backtest run trades 2h longer than live ever would.
    """
    assert cfg.BT_TRADE_TIMEOUT_MINUTES == cfg.CSM_TRADE_EXPIRY_HOURS * 60
    assert cfg.TRADE_SIM_MAX_DURATION_HOURS == cfg.CSM_TRADE_EXPIRY_HOURS
    # The longest TTL must fit inside the monitor window, or the enum is cosmetic.
    assert max(cfg.TTL_MINUTES_ALLOWED) <= cfg.BT_TRADE_TIMEOUT_MINUTES


def test_default_backtest_capital_is_one_thousand():
    assert cfg.BT_INITIAL_CAPITAL == 1000.0


# ══════════════════════════════════════════════════════════════════════════
# Commission — the backtest must not be frictionless (2026-08-15)
# ══════════════════════════════════════════════════════════════════════════

def test_commission_uses_configured_per_asset_rates(monkeypatch):
    """Use synthetic rates so the test runs without a broker calibration table."""
    monkeypatch.setitem(cfg.COMMISSION_USD_PER_0_01_LOT, "XAGUSD", 0.80)
    monkeypatch.setitem(cfg.COMMISSION_USD_PER_0_01_LOT, "USTEC", 0.01)
    assert commission_for("XAGUSD", 0.01) == pytest.approx(cfg.COMMISSION_USD_PER_0_01_LOT["XAGUSD"])
    assert commission_for("USTEC", 0.01) == pytest.approx(cfg.COMMISSION_USD_PER_0_01_LOT["USTEC"])
    # XAG must stay far more expensive than USTEC — collapsing to a blended
    # average would erase this gap.
    assert commission_for("XAGUSD", 0.01) > 10 * commission_for("USTEC", 0.01)


def test_commission_scales_linearly_with_lot_size():
    assert commission_for("XAUUSD", 0.02) == pytest.approx(2 * commission_for("XAUUSD", 0.01))


def test_unresolvable_asset_is_charged_nothing():
    """No profile -> no guessed real-dollar fee (abstract-unit callers stay free)."""
    assert commission_for(None, 1.0) == 0.0
    assert commission_for("NOT_A_REAL_SYMBOL", 1.0) == 0.0


def test_position_open_books_exactly_one_commission_leg():
    """Charged once, on entry — never again on a TP1/TP2/SL/TTL leg.

    Matches the measured live behaviour: every one of 355 real entry deals
    (magic 234000) carried nonzero commission; every one of 405 exit deals,
    including every partial, carried exactly zero.
    """
    state = {"direction": "long", "entry": 100.0, "sl": 90.0, "initial_stop": 90.0,
             "sl_moved": False, "tp1_hit": False, "tp2_hit": False}
    rules = build_rules(110.0, 130.0, entry=100.0, stop_loss=90.0, atr_1h=5.0)
    sim = PositionSimulator("XAUUSD", state, rules, quantity=0.05)

    commission_legs = [leg for leg in sim.result.legs if leg.reason == "COMMISSION"]
    assert len(commission_legs) == 1
    assert commission_legs[0].pnl == pytest.approx(-commission_for("XAUUSD", 0.05))

    sim.step(close=110.0, high=111.0, low=105.0, when=None)   # TP1
    sim.step(close=130.0, high=131.0, low=125.0, when=None)   # TP2
    for _ in range(6):
        sim.step(close=140.0, high=141.0, low=135.0, when=None)
    for _ in range(6):
        sim.step(close=100.0, high=141.0, low=100.0, when=None)
        if sim.result.closed:
            break

    assert [leg.reason for leg in sim.result.legs].count("COMMISSION") == 1


# ══════════════════════════════════════════════════════════════════════════
# Result vocabulary — every label a producer emits must be classifiable
# ══════════════════════════════════════════════════════════════════════════

def test_every_producible_result_label_is_classifiable():
    """No producer may emit a label reporting bins as "other".

    The regression: simulate_trade emitted "Time Exit (Win)"/"(Loss)", which the
    summary endpoint's substring classifier matched nowhere — those trades were
    dropped from win rate, profit factor, expectancy and the go-live scorecard
    while their P&L still counted in total_pnl.
    """
    from packages.core.backtest_types import (
        CLOSED_RESULTS, RESULT_BE, RESULT_SL, RESULT_TIMEOUT, RESULT_TP,
        RESULT_TRAIL, result_type,
    )

    for label in CLOSED_RESULTS:
        assert result_type(label) != "other", f"{label!r} is unclassifiable"
    assert result_type(RESULT_TRAIL) == "TP"
    assert result_type(RESULT_TIMEOUT) == "Timeout"
    assert result_type(RESULT_SL) == "SL"
    assert result_type(RESULT_BE) == "BE"
    assert result_type(RESULT_TP) == "TP"


def test_timeout_branch_is_reachable():
    """The monitor-expiry force close must actually be able to fire.

    It was unreachable: the future series was truncated to exactly
    `max_duration_hours*60/5` bars, which spans one bar LESS than the limit, so
    the duration test never passed and every long trade returned "Open" — its
    P&L silently excluded from the run.
    """
    from packages.core.backtest_types import RESULT_TIMEOUT

    hours = 2
    bars = (hours * 60) // 5 + 5
    idx = [datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=5 * i)
           for i in range(bars)]
    fut = pd.DataFrame([_bar(100, 100.4, 99.6, 100)] * bars, index=idx)
    pred = {
        "direction": "long", "pattern": "code_sniper",
        "entry": 100.0, "stop": 90.0, "tp1": 150.0, "tp2": 200.0, "target": 200.0,
    }
    out = simulate_trade(pred, fut, asset="BTCUSD", lot_size=1.0,
                         max_trade_duration_hours=hours)
    assert out["result"] == RESULT_TIMEOUT


def test_pattern_thresholds_fall_back_instead_of_raising():
    """get_pattern_thresholds raised for 5 of the 6 traded assets."""
    from packages.core.trigger_check import PATTERN_THRESHOLDS, get_pattern_thresholds

    for asset in ("GBPUSD", "EURUSD", "XAGUSD", "USTEC", "ETHUSD"):
        assert get_pattern_thresholds(asset) == PATTERN_THRESHOLDS["default"]
    # Explicit rows still win, and broker suffixes resolve to them.
    assert get_pattern_thresholds("XAUUSD") == PATTERN_THRESHOLDS["XAUUSD"]
    assert get_pattern_thresholds("XAUUSDm") == PATTERN_THRESHOLDS["XAUUSD"]


# ══════════════════════════════════════════════════════════════════════════
# Replay correctness: session helpers must not read the wall clock
# ══════════════════════════════════════════════════════════════════════════

def test_session_noise_floor_is_as_of_aware(tmp_path, monkeypatch):
    """The same ATR/price must be able to produce different floors by session.

    Without an as-of time, a replayed 2024 bar is scored against whatever
    session the machine running the replay happens to be in.
    """
    import json

    from packages.core.market_structure import recommended_stop_distance

    stats = tmp_path / "session_stats.json"
    stats.write_text(json.dumps({"assets": {"XAUUSD": {
        "asia": {"tr_pct": {"p60": 0.05}},
        "ny": {"tr_pct": {"p60": 0.30}},
    }}}), encoding="utf-8")
    monkeypatch.setattr(cfg, "SESSION_STATS_PATH", str(stats))
    monkeypatch.setitem(cfg.SESSION_NOISE_FLOOR_ENABLED_BY_ASSET, "XAUUSD", True)

    asia = datetime(2024, 3, 1, 2, 0, tzinfo=timezone.utc)
    ny = datetime(2024, 3, 1, 15, 0, tzinfo=timezone.utc)
    d_asia = recommended_stop_distance(1.0, 4137.0, "XAUUSD", asia)
    d_ny = recommended_stop_distance(1.0, 4137.0, "XAUUSD", ny)
    assert d_asia is not None and d_ny is not None
    # XAUUSD's profiled NY session is materially noisier than its Asia session,
    # so an as-of-aware floor must differ between the two.
    assert d_ny > d_asia


def test_htf_context_percentile_uses_the_candle_clock():
    """compute_htf_context defaults its as-of to the newest supplied bar."""
    from packages.core.market_structure import compute_htf_context

    base = datetime(2024, 3, 1, 2, 0, tzinfo=timezone.utc)
    candles = [
        {"time": base + timedelta(hours=i), "open": 100 + i, "high": 101 + i,
         "low": 99 + i, "close": 100.5 + i, "volume": 1.0}
        for i in range(60)
    ]
    out = compute_htf_context({"1h": candles}, "XAUUSD")
    # The series ends in the EU session (02:00 + 59h), never "now".
    if out.get("session"):
        from packages.core.session_stats import session_of
        assert out["session"] == session_of(candles[-1]["time"])
