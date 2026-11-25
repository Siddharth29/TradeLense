"""Executable prices and native-order timing, independent of strategy quality."""

import pytest

from packages.core.backtest_account import PositionSimulator, build_rules, commission_for
from packages.core.trade_monitor import EXIT_SL, EXIT_TP, evaluate_trade


def simulator(direction="long", *, runner=True, qty=0.02, skip_first=False):
    stop = 90.0 if direction == "long" else 110.0
    tp1 = 112.0 if direction == "long" else 88.0
    tp2 = 120.0 if direction == "long" else 80.0
    return PositionSimulator(
        "BTCUSD",
        {"direction": direction, "entry": 100.0, "sl": stop, "initial_stop": stop},
        build_rules(tp1, tp2, entry=100.0, stop_loss=stop, runner_enabled=runner),
        qty, settlement_candles=2, skip_first_bar=skip_first,
    )


@pytest.mark.parametrize("direction, high, low", [("long", 104.0, 89.0), ("short", 111.0, 96.0)])
def test_native_stop_is_active_on_first_post_entry_bar(direction, high, low):
    sim = simulator(direction)
    assert sim.step(100.0, high, low, open_price=100.0) == EXIT_SL
    assert sim.result.closed
    assert sim.result.exit_price == (90.0 if direction == "long" else 110.0)


def test_only_actual_pre_entry_bar_is_excluded():
    sim = simulator(skip_first=True)
    assert sim.step(100.0, 125.0, 85.0, open_price=95.0) is None
    assert not sim.result.closed
    assert sim.step(94.0, 101.0, 89.0, open_price=100.0) == EXIT_SL


@pytest.mark.parametrize("runner", [True, False])
def test_bar_touching_both_levels_cannot_award_tp_before_native_sl(runner):
    sim = simulator(runner=runner)
    assert sim.step(122.0, 125.0, 85.0, open_price=100.0) == EXIT_SL
    assert sim.result.exit_price == 90.0
    assert not sim.result.tp2_hit


@pytest.mark.parametrize(
    "direction, opening, high, low, close",
    [("long", 85.0, 88.0, 82.0, 86.0), ("short", 115.0, 118.0, 112.0, 114.0)],
)
def test_adverse_gap_executes_at_open_even_if_stop_outside_range(direction, opening, high, low, close):
    sim = simulator(direction)
    assert sim.step(close, high, low, open_price=opening) == EXIT_SL
    assert sim.result.exit_price == opening
    assert evaluate_trade(sim.trade_state, close, high, low, sim.rules) == EXIT_SL


def test_native_tp_gap_has_known_precedence_over_later_stop_touch():
    sim = simulator(runner=False)
    assert sim.step(95.0, 124.0, 88.0, open_price=122.0) == EXIT_TP
    assert sim.result.exit_price == 120.0


def test_synthetic_tp2_fallback_uses_market_close_not_touched_level():
    sim = simulator(qty=0.01)
    sim.step(105.0, 121.0, 100.0, open_price=102.0)
    assert sim.result.closed
    assert sim.result.exit_price == 105.0
    assert sim.result.pnl == pytest.approx(0.05 - commission_for("BTCUSD", 0.01))


def test_native_exit_resolves_before_bar_close_timeout_and_is_idempotent():
    sim = simulator()
    assert sim.check_native_exit(103.0, 104.0, 89.0, open_price=100.0) == EXIT_SL
    pnl = sim.result.pnl
    sim.force_close(103.0, None)
    sim.step(103.0, 104.0, 89.0, open_price=100.0)
    assert sim.result.exit_price == 90.0
    assert sim.result.pnl == pnl


@pytest.mark.parametrize("direction, current", [("long", 108.0), ("short", 92.0)])
def test_tp1_be_move_cannot_loosen_a_stronger_native_stop(direction, current):
    sim = simulator(direction)
    sim.trade_state["sl"] = current
    sim._move_stop_to_be()
    assert sim.trade_state["sl"] == current


def test_retraced_touch_cannot_install_stop_above_current_close_and_step_retries():
    sim = simulator(qty=0.10)
    sim.rules["tp1"] = None
    sim.rules["tp"] = None
    sim.rules["trail_config"] = [(1.5, 1, 1.0)]
    sim.rules["trail_arm_on_touch"] = True
    # Touched +1.5R, but at update time only +0.7R is executable.
    sim.step(107.0, 116.0, 99.0, open_price=100.0)
    assert sim.trade_state["sl"] == 90.0
    assert sim.trade_state["trail_step"] == 0
    assert sim.result.rejected_stop_updates == 1
    sim.step(112.0, 116.0, 106.0, open_price=107.0)
    assert sim.trade_state["sl"] == 110.0
    assert sim.trade_state["trail_step"] == 1


def test_tp1_partial_can_fill_but_impossible_be_stop_is_not_invented():
    sim = simulator(qty=0.10)
    sim.step(102.0, 113.0, 99.0, open_price=100.0)
    assert sim.result.tp1_hit
    assert sim.open_qty == pytest.approx(0.05)
    assert sim.trade_state["sl"] == 90.0
    assert sim.result.rejected_stop_updates == 1
