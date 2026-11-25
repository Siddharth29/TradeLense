import pytest

from core.trade_monitor import (
    evaluate_trade,
    _evaluate_trailing,
    _evaluate_continuous_trailing,
    MOVE_SL_TO_BE,
    TRAIL_SL,
    EXIT_TP,
    EXIT_SL,
    DEFAULT_TRAIL_STEPS,
    CONTINUOUS_TRAIL_STEP_R,
    CONTINUOUS_TRAIL_CONFIRM,
)


# ── Helpers ──────────────────────────────────────────────────────────

def _long_state(entry=100.0, sl=90.0, **kw):
    base = {"direction": "long", "entry": entry, "sl": sl, "initial_stop": sl}
    base.update(kw)
    return base


def _short_state(entry=100.0, sl=110.0, **kw):
    base = {"direction": "short", "entry": entry, "sl": sl, "initial_stop": sl}
    base.update(kw)
    return base


def _rules(**kw):
    base = {"tp1": None, "tp": None}
    base.update(kw)
    return base


# ── Basic TP / SL / BE tests ────────────────────────────────────────

def test_move_sl_to_be_long():
    state = {"direction": "long", "entry": 100, "sl": 95, "sl_moved": False, "tp1_hit": False}
    rules = {"tp1": 105, "tp": 110}
    assert evaluate_trade(state, 104, 106, 99, rules) == MOVE_SL_TO_BE


def test_exit_tp_long():
    state = {"direction": "long", "entry": 100, "sl": 95, "sl_moved": True, "tp1_hit": True}
    rules = {"tp1": 105, "tp": 110}
    assert evaluate_trade(state, 108, 111, 100, rules) == EXIT_TP


def test_exit_sl_short():
    state = {"direction": "short", "entry": 100, "sl": 105}
    rules = {"tp1": 95, "tp": 90}
    assert evaluate_trade(state, 104, 106, 100, rules) == EXIT_SL


def test_no_action():
    state = {"direction": "long", "entry": 100, "sl": 95}
    rules = {"tp1": 105, "tp": 110}
    assert evaluate_trade(state, 102, 104, 98, rules) is None


def test_entry_candle_skips_sl_long():
    state = {"direction": "long", "entry": 100, "sl": 99, "sl_moved": False, "tp1_hit": False}
    rules = {"tp1": 105, "tp": 110}
    assert evaluate_trade(state, 100, 102, 98, rules) == EXIT_SL
    assert evaluate_trade(state, 100, 102, 98, rules, is_entry_candle=True) is None


def test_ambiguous_native_brackets_use_conservative_sl_first():
    state = {"direction": "long", "entry": 100, "sl": 95, "sl_moved": True, "tp1_hit": True}
    rules = {"tp1": None, "tp": 105}
    assert evaluate_trade(state, 102, 106, 94, rules) == EXIT_SL


def test_invalid_direction_returns_none():
    state = {"direction": "sideways", "entry": 100, "sl": 95}
    rules = {"tp1": 105, "tp": 110}
    assert evaluate_trade(state, 102, 104, 98, rules) is None


# ── DEFAULT_TRAIL_STEPS sanity ──────────────────────────────────────

def test_default_trail_steps_has_8_entries():
    assert len(DEFAULT_TRAIL_STEPS) == 8


def test_default_trail_steps_trigger_r_ascending():
    triggers = [s[0] for s in DEFAULT_TRAIL_STEPS]
    assert triggers == sorted(triggers)
    assert triggers[0] == 0.5
    assert triggers[-1] == 5.0


def test_default_trail_steps_trail_to_le_trigger():
    for trigger_r, _, trail_to_r in DEFAULT_TRAIL_STEPS:
        assert trail_to_r <= trigger_r


# ── Fixed trailing SL (long) ────────────────────────────────────────

class TestTrailingLong:
    def _make(self):
        return _long_state(entry=100.0, sl=90.0), _rules()

    def test_step0_needs_3_closes_beyond_half_r(self):
        state, rules = self._make()
        risk = 10.0  # entry - sl
        # close at 105 = entry + 0.5R
        assert evaluate_trade(state, 105.0, 106, 99, rules) is None  # 1st
        assert state["trail_consec"] == 1
        assert evaluate_trade(state, 105.5, 106, 99, rules) is None  # 2nd
        assert state["trail_consec"] == 2
        assert evaluate_trade(state, 106.0, 107, 99, rules) == TRAIL_SL  # 3rd fires
        assert state["trail_step"] == 1
        assert state["_pending_trail_sl"] == 105.0  # BE+0.5R (step0 trails to 0.5R)

    def test_step0_resets_on_close_below_trigger(self):
        state, rules = self._make()
        assert evaluate_trade(state, 105.0, 106, 99, rules) is None  # 1
        assert evaluate_trade(state, 105.0, 106, 99, rules) is None  # 2
        assert evaluate_trade(state, 99.0, 106, 98, rules) is None   # reset
        assert state["trail_consec"] == 0

    def test_step1_fires_on_single_close_beyond_1r(self):
        state, rules = self._make()
        # Fast-forward: manually set step 0 as fired
        state["trail_step"] = 1
        state["trail_consec"] = 0
        # close at 110 = entry + 1.0R
        assert evaluate_trade(state, 110.0, 111, 99, rules) == TRAIL_SL
        assert state["trail_step"] == 2
        assert state["_pending_trail_sl"] == 105.0  # +0.5R

    def test_all_8_fixed_steps_fire_sequentially(self):
        state, rules = self._make()
        risk = 10.0

        # Step 0: 3 closes beyond 0.5R (105)
        for _ in range(3):
            evaluate_trade(state, 105.0, 106, 99, rules)
        assert state["trail_step"] == 1

        # Steps 1-7: each needs 1 close
        expected_triggers = [110, 115, 120, 125, 130, 140, 150]
        for i, trigger in enumerate(expected_triggers, start=1):
            result = evaluate_trade(state, float(trigger), trigger + 1, 99, rules)
            assert result == TRAIL_SL, f"Step {i} failed to fire at {trigger}"
            assert state["trail_step"] == i + 1

        assert state["trail_step"] == 8  # all exhausted

    def test_continuous_trailing_engages_after_fixed_steps(self):
        state, rules = self._make()
        risk = 10.0
        # Fast-forward past all 8 fixed steps
        state["trail_step"] = 8
        state["trail_consec"] = 0

        # Next continuous trigger: last trail_to_r = 4.5, + 0.5 = 5.0R
        # trigger = 100 + 5.0 * 10 = 150
        result = evaluate_trade(state, 150.0, 151, 99, rules)
        assert result == TRAIL_SL
        assert state["cont_trail_r"] == 5.0
        assert state["_pending_trail_sl"] == 150.0  # entry + 5.0R

    def test_continuous_trailing_ratchets_multiple_times(self):
        state, rules = self._make()
        risk = 10.0
        state["trail_step"] = 8
        state["trail_consec"] = 0

        # First continuous: 5.0R trigger -> trail to 5.0R
        evaluate_trade(state, 150.0, 151, 99, rules)
        assert state["cont_trail_r"] == 5.0

        # Second continuous: 5.5R trigger -> trail to 5.5R
        result = evaluate_trade(state, 155.0, 156, 99, rules)
        assert result == TRAIL_SL
        assert state["cont_trail_r"] == 5.5
        assert state["_pending_trail_sl"] == 155.0

        # Third continuous: 6.0R trigger -> trail to 6.0R
        result = evaluate_trade(state, 160.0, 161, 99, rules)
        assert result == TRAIL_SL
        assert state["cont_trail_r"] == 6.0
        assert state["_pending_trail_sl"] == 160.0


# ── Fixed trailing SL (short) ───────────────────────────────────────

class TestTrailingShort:
    def _make(self):
        return _short_state(entry=100.0, sl=110.0), _rules()

    def test_step0_needs_3_closes_below_half_r(self):
        state, rules = self._make()
        # 0.5R below entry = 95
        assert evaluate_trade(state, 95.0, 101, 94, rules) is None
        assert evaluate_trade(state, 94.5, 101, 93, rules) is None
        assert evaluate_trade(state, 94.0, 101, 93, rules) == TRAIL_SL
        assert state["_pending_trail_sl"] == 95.0  # BE+0.5R (step0 trails to 0.5R)

    def test_step1_fires_below_1r(self):
        state, rules = self._make()
        state["trail_step"] = 1
        state["trail_consec"] = 0
        # 1.0R below entry = 90
        assert evaluate_trade(state, 90.0, 101, 89, rules) == TRAIL_SL
        assert state["_pending_trail_sl"] == 95.0  # +0.5R for short = entry - 0.5R

    def test_continuous_trailing_short(self):
        state, rules = self._make()
        risk = 10.0
        state["trail_step"] = 8
        state["trail_consec"] = 0

        # Last trail_to_r = 4.5, next trigger = 4.5 + 0.5 = 5.0R below entry
        # trigger = 100 - 50 = 50
        result = evaluate_trade(state, 50.0, 51, 49, rules)
        assert result == TRAIL_SL
        assert state["_pending_trail_sl"] == 50.0  # entry - 5.0R

        # Next: 5.5R -> 45
        result = evaluate_trade(state, 45.0, 46, 44, rules)
        assert result == TRAIL_SL
        assert state["_pending_trail_sl"] == 45.0


# ── Custom trail config ─────────────────────────────────────────────

def test_custom_trail_config():
    state = _long_state(entry=100.0, sl=90.0)
    custom_steps = [(0.3, 1, 0.0), (0.6, 1, 0.3)]
    rules = _rules(trail_config=custom_steps)
    risk = 10.0

    # Step 0: 1 close beyond 0.3R (103)
    result = evaluate_trade(state, 103.0, 104, 99, rules)
    assert result == TRAIL_SL
    assert state["_pending_trail_sl"] == 100.0

    # Step 1: 1 close beyond 0.6R (106)
    result = evaluate_trade(state, 106.0, 107, 99, rules)
    assert result == TRAIL_SL
    assert state["_pending_trail_sl"] == 103.0  # +0.3R


def test_empty_trail_config_disables_trailing():
    state = _long_state(entry=100.0, sl=90.0)
    rules = _rules(trail_config=[])
    result = evaluate_trade(state, 115.0, 120, 99, rules)
    assert result is None


def test_custom_continuous_step_r():
    state = _long_state(entry=100.0, sl=90.0)
    risk = 10.0
    custom_steps = [(0.5, 1, 0.0)]
    rules = _rules(trail_config=custom_steps, continuous_trail_step_r=1.0)

    # Step 0: close beyond 0.5R (105) -> BE
    evaluate_trade(state, 105.0, 106, 99, rules)
    assert state["trail_step"] == 1

    # Continuous: next trigger = 0.0 + 1.0R = 1.0R (110)
    result = evaluate_trade(state, 110.0, 111, 99, rules)
    assert result == TRAIL_SL
    assert state["cont_trail_r"] == 1.0
    assert state["_pending_trail_sl"] == 110.0


# ── Edge cases ───────────────────────────────────────────────────────

def test_zero_risk_returns_none():
    state = _long_state(entry=100.0, sl=100.0)
    rules = _rules()
    # candle range must not touch the SL (100), otherwise EXIT_SL fires first
    result = evaluate_trade(state, 105.0, 106, 101, rules)
    assert result is None


def test_missing_entry_returns_none():
    state = {"direction": "long", "sl": 90.0, "initial_stop": 90.0}
    rules = _rules()
    result = evaluate_trade(state, 105.0, 106, 99, rules)
    assert result is None


def test_missing_initial_stop_uses_sl():
    state = {"direction": "long", "entry": 100.0, "sl": 90.0}
    rules = _rules()
    # Should still work — _evaluate_trailing falls back to sl
    result = evaluate_trade(state, 105.0, 106, 99, rules)
    assert result is None  # only 1st close, needs 3


def test_trail_state_persistence_across_calls():
    state = _long_state(entry=100.0, sl=90.0)
    rules = _rules()
    evaluate_trade(state, 105.0, 106, 99, rules)
    assert "trail_step" in state
    assert "trail_consec" in state
    assert state["trail_step"] == 0
    assert state["trail_consec"] == 1


# ── Integration: full trade lifecycle ────────────────────────────────

def test_full_long_trade_lifecycle():
    """Simulate a winning long trade going through TP1 -> trail -> exit."""
    state = {
        "direction": "long",
        "entry": 100.0,
        "sl": 90.0,
        "initial_stop": 90.0,
        "sl_moved": False,
        "tp1_hit": False,
    }
    rules = {"tp1": 105.0, "tp": 120.0}

    # Candle 1: price rises, no trigger
    assert evaluate_trade(state, 103.0, 104, 101, rules) is None

    # Candle 2: hits TP1 -> MOVE_SL_TO_BE
    assert evaluate_trade(state, 104.5, 105.5, 103, rules) == MOVE_SL_TO_BE
    state["sl_moved"] = True  # caller does this
    state["tp1_hit"] = True
    state["sl"] = 105.0  # BE+0.5R (entry=100, risk=10, 0.5*10=5)

    # Candle 3-5: close above 0.5R (105), building trail step 0 count
    # SL is at 105, so candle low must stay above 105 to avoid EXIT_SL
    assert evaluate_trade(state, 106.0, 107, 105.5, rules) is None
    assert evaluate_trade(state, 106.5, 107, 105.5, rules) is None
    assert evaluate_trade(state, 107.0, 108, 105.5, rules) == TRAIL_SL
    state["sl"] = state.pop("_pending_trail_sl")  # 105.0 (step0 → 0.5R)

    # Candle 6: close above 1.0R (110) -> trail step 1 (trails to 0.5R = 105.0)
    assert evaluate_trade(state, 110.5, 111, 106, rules) == TRAIL_SL
    state["sl"] = state.pop("_pending_trail_sl")  # 105.0

    # Eventually hits TP
    assert evaluate_trade(state, 119, 121, 118, rules) == EXIT_TP


def test_full_short_trade_with_continuous_trailing():
    """Short trade that runs far enough to engage continuous trailing."""
    state = _short_state(entry=1000.0, sl=1050.0)
    risk = 50.0
    rules = _rules()

    # Fire all 8 fixed steps quickly
    # Step 0: 3 closes below 975 (0.5R)
    for _ in range(3):
        evaluate_trade(state, 974.0, 1001, 973, rules)
    assert state["trail_step"] == 1

    triggers_and_expected = [
        (950.0, 975.0),   # 1.0R -> +0.5R = 975
        (925.0, 950.0),   # 1.5R -> +1.0R = 950
        (900.0, 925.0),   # 2.0R -> +1.5R = 925
        (875.0, 900.0),   # 2.5R -> +2.0R = 900
        (850.0, 875.0),   # 3.0R -> +2.5R = 875
        (800.0, 825.0),   # 4.0R -> +3.5R = 825
        (750.0, 775.0),   # 5.0R -> +4.5R = 775
    ]
    for trigger, expected_sl in triggers_and_expected:
        result = evaluate_trade(state, trigger, trigger + 1, trigger - 1, rules)
        assert result == TRAIL_SL
        assert state["_pending_trail_sl"] == expected_sl

    assert state["trail_step"] == 8

    # Now continuous trailing: 4.5R + 0.5 = 5.0R trigger = 1000 - 250 = 750
    result = evaluate_trade(state, 750.0, 751, 749, rules)
    assert result == TRAIL_SL
    assert state["_pending_trail_sl"] == 750.0

    # 5.5R trigger = 725
    result = evaluate_trade(state, 725.0, 726, 724, rules)
    assert result == TRAIL_SL
    assert state["_pending_trail_sl"] == 725.0


# ── Broker-rejection rollback (2026-06-12) ──────────────────────────
#
# When the broker rejects a modify_sl (MT5 10016 "Invalid stops"), the
# monitor restores the snapshot in ``_pending_trail_rollback`` so the
# step re-fires next candle instead of being silently consumed.

class TestTrailRollback:
    def test_fixed_step_fire_sets_rollback_snapshot(self):
        state = _long_state(entry=100.0, sl=90.0)
        rules = _rules(trail_config=[(0.5, 1, 0.5), (1.0, 1, 1.0)])
        assert evaluate_trade(state, 105.0, 106, 99, rules) == TRAIL_SL
        assert state["trail_step"] == 1
        assert state["_pending_trail_rollback"] == {
            "trail_step": 0,
            "trail_consec": 1,
        }

    def test_fixed_step_rollback_refires_same_level(self):
        state = _long_state(entry=100.0, sl=90.0)
        rules = _rules(trail_config=[(0.5, 1, 0.5), (1.0, 1, 1.0)])
        assert evaluate_trade(state, 105.0, 106, 99, rules) == TRAIL_SL
        first_sl = state.pop("_pending_trail_sl")
        state.pop("_trail_label")
        # Simulate the broker rejecting the move: monitor applies rollback
        state.update(state.pop("_pending_trail_rollback"))
        assert state["trail_step"] == 0
        # Same close next candle → the SAME step fires again at the SAME level
        assert evaluate_trade(state, 105.0, 106, 99, rules) == TRAIL_SL
        assert state["_pending_trail_sl"] == first_sl
        assert state["trail_step"] == 1

    def test_fixed_step_rollback_with_multi_confirm_step(self):
        # DEFAULT step 0 needs 3 confirms; after rollback the saved consec
        # keeps counting so one more beyond-close re-fires immediately.
        state = _long_state(entry=100.0, sl=90.0)
        rules = _rules()
        assert evaluate_trade(state, 105.0, 106, 99, rules) is None
        assert evaluate_trade(state, 105.0, 106, 99, rules) is None
        assert evaluate_trade(state, 105.0, 106, 99, rules) == TRAIL_SL
        state.pop("_pending_trail_sl")
        state.pop("_trail_label")
        state.update(state.pop("_pending_trail_rollback"))
        assert state["trail_consec"] == 3
        assert evaluate_trade(state, 105.0, 106, 99, rules) == TRAIL_SL
        assert state["_pending_trail_sl"] == 105.0

    def test_continuous_fire_sets_rollback_and_refires(self):
        state = _long_state(entry=100.0, sl=90.0)
        state["trail_step"] = 8
        state["trail_consec"] = 0
        rules = _rules()
        # 4.5R baseline + 0.5R step → trigger 5.0R = 150
        assert evaluate_trade(state, 150.0, 151, 149, rules) == TRAIL_SL
        first_sl = state.pop("_pending_trail_sl")
        state.pop("_trail_label")
        assert state["_pending_trail_rollback"] == {
            "cont_trail_r": 4.5,
            "cont_trail_consec": 1,
        }
        state.update(state.pop("_pending_trail_rollback"))
        assert state["cont_trail_r"] == 4.5
        assert evaluate_trade(state, 150.0, 151, 149, rules) == TRAIL_SL
        assert state["_pending_trail_sl"] == first_sl

    def test_chandelier_sets_no_rollback(self):
        # The post-TP2 chandelier recomputes against the unchanged SL every
        # bar (self-healing on rejection) — it must not set a rollback.
        state = _long_state(entry=100.0, sl=102.0, tp1_hit=True, tp2_hit=True)
        rules = _rules(tp=110.0, runner_enabled=True, runner_trail_dist=3.0)
        assert evaluate_trade(state, 114.0, 115.0, 113.0, rules) == TRAIL_SL
        assert state["_pending_trail_sl"] == 112.0
        assert "_pending_trail_rollback" not in state
