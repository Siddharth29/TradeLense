"""Explicit exit families and quote decisions; structural trails remain bar-based."""
from __future__ import annotations

from .trade_monitor import MOVE_SL_TO_BE, PARTIAL_TP2, EXIT_TP


def family_exit_rules(family, existing, *, max_age_minutes=None):
    rules = dict(existing)
    if family not in ("continuation_retest", "range_reclaim", "sweep_reclaim"):
        return rules  # Legacy strategies retain their existing behavior.
    rules["family"] = family
    if max_age_minutes is not None:
        rules["hard_max_age_minutes"] = int(max_age_minutes)
    if family == "range_reclaim":
        rules["runner_enabled"] = False
        rules["tp"] = rules.get("tp1")  # Accepted range objective; no invented extension.
    return rules


def quote_action(state, rules, price):
    """A fresh executable quote can monetize a target, never a historical high."""
    sign = 1 if state["direction"] == "long" else -1
    tp1, tp2 = rules.get("tp1"), rules.get("tp")
    if tp2 is not None and not state.get("tp2_hit") and sign*(price-tp2) >= 0:
        return PARTIAL_TP2 if rules.get("runner_enabled") else EXIT_TP
    if tp1 is not None and not state.get("tp1_hit") and sign*(price-tp1) >= 0:
        return MOVE_SL_TO_BE
    return None  # No trail_consec increments or OHLC-based stop assumptions.
