"""Utilities for monitoring an open trade.

The module exposes :func:`evaluate_trade` which inspects the latest
candle close and determines whether any action should be taken on the
current trade.  The supported actions are:

``MOVE_SL_TO_BE``
    Move the stop loss to the entry (break-even) after the first target
    is hit.
``TRAIL_SL``
    Trail the stop loss based on close-confirmed R-multiple thresholds.
    Uses candle close (not high/low) for confirmation, requiring N
    consecutive closes beyond the trigger level before moving the SL.
    This is the only honest trailing approach with OHLC-only data.
``EXIT_TP``
    Exit the trade at the final target price.
``EXIT_SL``
    Exit the trade at the stop loss.

Trailing SL Logic (close-based, OHLC-safe)
-------------------------------------------
Within a single OHLC candle the tick-by-tick path is unknown, so acting
on high/low alone leads to phantom triggers.  The trailing uses candle
**close** prices with multi-candle confirmation:

Fixed steps fire sequentially from 0.5R through 5.0R.  After all fixed
steps exhaust, continuous trailing engages: each additional 0.5R of
favourable close movement ratchets the SL forward by 0.5R.

Each step is evaluated only after the previous step has fired.  The SL
is only ever moved *forward* (in the trade direction), never backward.
"""

from __future__ import annotations

from datetime import datetime  # noqa: F401  (used in annotations)
from typing import Dict, List, Optional, Tuple

from .config import (
    DEFAULT_TRAIL_STEPS as _CFG_TRAIL_STEPS,
    CONTINUOUS_TRAIL_STEP_R as _CFG_TRAIL_STEP_R,
    CONTINUOUS_TRAIL_CONFIRM as _CFG_TRAIL_CONFIRM,
    TRAIL_ARM_ON_TOUCH as _CFG_TRAIL_ARM_ON_TOUCH,
    TTL_EXTEND_WHEN_GREEN as _CFG_TTL_EXTEND_WHEN_GREEN,
    TTL_EXTEND_MIN_R as _CFG_TTL_EXTEND_MIN_R,
    TTL_EXTEND_HOURS as _CFG_TTL_EXTEND_HOURS,
    TTL_EXTEND_MAX_HOURS as _CFG_TTL_EXTEND_MAX_HOURS,
)

MOVE_SL_TO_BE = "MOVE_SL_TO_BE"
TRAIL_SL = "TRAIL_SL"
EXIT_TP = "EXIT_TP"
EXIT_SL = "EXIT_SL"
# TP2 reached with the runner enabled: close a fraction of the remaining
# position, keep the rest trailed by an ATR chandelier (see config
# TP2_RUNNER_ENABLED / RUNNER_TRAIL_ATR_MULT). Emitted instead of EXIT_TP.
PARTIAL_TP2 = "PARTIAL_TP2"

DEFAULT_TRAIL_STEPS: List[Tuple[float, int, float]] = _CFG_TRAIL_STEPS
CONTINUOUS_TRAIL_STEP_R = _CFG_TRAIL_STEP_R
CONTINUOUS_TRAIL_CONFIRM = _CFG_TRAIL_CONFIRM
# Module-level so tests can monkeypatch them on this module (see the
# dual-import-path note in tests/); rules["trail_arm_on_touch"] additionally
# overrides the arming basis per trade.
TRAIL_ARM_ON_TOUCH = _CFG_TRAIL_ARM_ON_TOUCH
TTL_EXTEND_WHEN_GREEN = _CFG_TTL_EXTEND_WHEN_GREEN
TTL_EXTEND_MIN_R = _CFG_TTL_EXTEND_MIN_R
TTL_EXTEND_HOURS = _CFG_TTL_EXTEND_HOURS
TTL_EXTEND_MAX_HOURS = _CFG_TTL_EXTEND_MAX_HOURS


def evaluate_trade(
    trade_state: Dict,
    candle_close: float,
    candle_high: float,
    candle_low: float,
    rules: Dict,
    *,
    is_entry_candle: bool = False,
) -> Optional[str]:
    """Evaluate the current trade and return an action if needed.

    Parameters
    ----------
    trade_state:
        Dictionary describing the current trade.  The trailing logic
        mutates ``trail_consec`` and ``trail_step`` inside this dict.
    candle_close:
        Closing price of the latest candle.
    candle_high:
        High of the latest candle.
    candle_low:
        Low of the latest candle.
    rules:
        Dictionary containing ``tp1`` and ``tp`` values.
        May also contain ``trail_config`` -- a list of
        ``(trigger_r, candle_confirm, trail_to_r)`` tuples.
    is_entry_candle:
        If True, do not evaluate stop-loss against this bar's high/low range.

    Returns
    -------
    Optional[str]
        One of ``TRAIL_SL``, ``MOVE_SL_TO_BE``, ``EXIT_TP``, ``EXIT_SL``
        or ``None`` if no action is required.
    """

    direction = trade_state.get("direction")
    sl = trade_state.get("sl")
    tp1_hit = trade_state.get("tp1_hit", False)
    tp2_hit = trade_state.get("tp2_hit", False)
    tp1 = rules.get("tp1")
    tp = rules.get("tp")
    runner_enabled = bool(rules.get("runner_enabled", False))

    if direction not in {"long", "short"}:
        return None

    is_long = direction == "long"

    def _target_reached(price: float) -> bool:
        # A gap completely beyond the level is still a crossing. Requiring
        # the level to lie INSIDE the bar silently misses gap exits.
        return candle_high >= price if is_long else candle_low <= price

    # The native stop is active throughout the bar. TP1 and, with a runner,
    # TP2 are market actions at bar close; neither can rescue a position that
    # already stopped out intrabar. When both a native TP and SL are touched
    # and no finer path is available, use the conservative SL-first outcome.
    if not is_entry_candle and sl is not None:
        stop_reached = candle_low <= sl if is_long else candle_high >= sl
        if stop_reached:
            return EXIT_SL

    # -- 2. Target handling, after the native stop.
    # Runner mode: TP2 books a partial and keeps a trailed runner instead of
    # closing everything (it can only fire once — tp2_hit gates it).
    if tp is not None and not tp2_hit and _target_reached(tp):
        return PARTIAL_TP2 if runner_enabled else EXIT_TP

    # -- 3. TP1 -> partial close 50% + move SL to BE+0.5R (check before
    #    trailing so the partial close always fires rather than being
    #    pre-empted by the trailing SL logic on the same candle).
    if not tp1_hit and tp1 is not None and _target_reached(tp1):
        return MOVE_SL_TO_BE

    # -- 4a. Post-TP2 runner: ATR chandelier from the post-TP2 extreme.
    #    (The SL itself is native at the broker; this only ratchets it.)
    if tp2_hit and runner_enabled:
        return _evaluate_chandelier(trade_state, candle_high, candle_low, rules)

    # -- 4b. Trailing SL. Close-based by default; steps whose trail_to_r is
    #    strictly inside their trigger_r may also arm on the bar's favourable
    #    extreme (config.TRAIL_ARM_ON_TOUCH — see the note there).
    trail_action = _evaluate_trailing(
        trade_state, candle_close, rules,
        candle_high=candle_high, candle_low=candle_low,
    )
    if trail_action is not None:
        return trail_action

    return None


def _evaluate_chandelier(
    trade_state: Dict,
    candle_high: float,
    candle_low: float,
    rules: Dict,
) -> Optional[str]:
    """Trail the post-TP2 runner: SL = extreme ∓ runner_trail_dist.

    ``runner_trail_dist`` (price units) is frozen at entry time
    (RUNNER_TRAIL_ATR_MULT × ATR(1h), or RUNNER_TRAIL_FALLBACK_RISK_MULT × R
    when no ATR was available) so no extra data fetch happens per candle.
    Mutates ``trade_state["runner_extreme"]``; only ever tightens the SL.
    """
    dist = rules.get("runner_trail_dist")
    if not dist or dist <= 0:
        return None
    is_long = trade_state.get("direction") == "long"

    extreme = trade_state.get("runner_extreme")
    if extreme is None:
        extreme = candle_high if is_long else candle_low
    else:
        extreme = max(extreme, candle_high) if is_long else min(extreme, candle_low)
    trade_state["runner_extreme"] = extreme

    candidate = extreme - dist if is_long else extreme + dist
    sl = trade_state.get("sl")
    improved = (
        sl is None
        or (is_long and candidate > sl)
        or (not is_long and candidate < sl)
    )
    if improved:
        trade_state["_pending_trail_sl"] = candidate
        trade_state["_trail_label"] = f"runner_chandelier(dist={dist:.6g})"
        return TRAIL_SL
    return None


def unrealised_r(trade_state: Dict, price: float) -> Optional[float]:
    """Current open profit in R multiples, or ``None`` if it can't be computed.

    R is measured against the INITIAL stop distance, not the current (possibly
    trailed) one, so the number means the same thing for the life of the trade.
    """
    entry = trade_state.get("entry")
    initial_stop = trade_state.get("initial_stop") or trade_state.get("sl")
    direction = trade_state.get("direction")
    if entry is None or initial_stop is None or direction not in {"long", "short"}:
        return None
    risk = abs(entry - initial_stop)
    if risk <= 0:
        return None
    move = price - entry if direction == "long" else entry - price
    return move / risk


def is_risk_free(trade_state: Dict) -> bool:
    """True when the live stop is at entry or better in the trade's direction.

    "Better" means the worst outcome from here is a scratch or a win, which is
    the precondition for granting a TTL extension: extending a position that can
    still lose money would be extending exposure, not extending a thesis.
    """
    entry = trade_state.get("entry")
    sl = trade_state.get("sl")
    direction = trade_state.get("direction")
    if entry is None or sl is None or direction not in {"long", "short"}:
        return False
    return sl >= entry if direction == "long" else sl <= entry


def maybe_extend_expiry(
    trade_state: Dict,
    price: float,
    expiry: "datetime",
    *,
    start_time: "datetime",
    now: "datetime",
    grace_minutes: int = 5,
) -> "datetime":
    """Grant a green, risk-free position more monitoring time.

    Returns the (possibly unchanged) expiry. Extension is granted only when all
    of the following hold, so the default path is byte-identical to before:

    * ``TTL_EXTEND_WHEN_GREEN`` is on;
    * expiry is within ``grace_minutes`` (one bar) — we are actually about to
      time out, so this is not silently lengthening every trade's life;
    * unrealised profit ≥ ``TTL_EXTEND_MIN_R``;
    * the stop is already at entry or better (:func:`is_risk_free`);
    * the new expiry stays inside ``TTL_EXTEND_MAX_HOURS`` from entry.

    Losers, scratches and anything whose stop has not moved time out on schedule
    exactly as they did before. Pre-close/maintenance force-closes are evaluated
    earlier in the monitor loop and are unaffected — a gap window still wins.
    """
    from datetime import timedelta

    if not TTL_EXTEND_WHEN_GREEN:
        return expiry
    if now < expiry - timedelta(minutes=grace_minutes):
        return expiry
    if not is_risk_free(trade_state):
        return expiry

    r = unrealised_r(trade_state, price)
    if r is None or r < TTL_EXTEND_MIN_R:
        return expiry

    hard_cap = start_time + timedelta(hours=TTL_EXTEND_MAX_HOURS)
    if expiry >= hard_cap:
        return expiry

    return min(expiry + timedelta(hours=TTL_EXTEND_HOURS), hard_cap)


def _arm_reference(
    candle_close: float,
    candle_high: Optional[float],
    candle_low: Optional[float],
    is_long: bool,
    rules: Dict,
    *,
    touch_safe: bool,
) -> float:
    """Price a trail step tests its trigger against.

    Returns the bar's favourable extreme when touch-arming is enabled, the step
    is touch-safe (its stop lands strictly inside its trigger) and the extreme
    was supplied; otherwise the close, which is the pre-2026-08-06 behaviour.

    ``rules["trail_arm_on_touch"]`` overrides the config default per trade, so a
    caller can replay a trade on either basis without touching global config.
    """
    if not touch_safe:
        return candle_close
    if not rules.get("trail_arm_on_touch", TRAIL_ARM_ON_TOUCH):
        return candle_close
    extreme = candle_high if is_long else candle_low
    if extreme is None:
        return candle_close
    # Defensive: a malformed bar whose "extreme" is worse than its close must
    # never make a step arm LATER than close-basis would have.
    return max(candle_close, extreme) if is_long else min(candle_close, extreme)


def _evaluate_trailing(
    trade_state: Dict,
    candle_close: float,
    rules: Dict,
    candle_high: Optional[float] = None,
    candle_low: Optional[float] = None,
) -> Optional[str]:
    """Check trailing SL steps.

    Arming is close-based by default. When ``TRAIL_ARM_ON_TOUCH`` is on and the
    bar's high/low are supplied, a step may instead arm on the bar's favourable
    EXTREME — but only where ``trail_to_r < trigger_r``, so the resulting stop
    is always strictly inside the price that armed it. See config.

    Mutates ``trade_state["trail_consec"]`` and ``trade_state["trail_step"]``.
    Returns ``TRAIL_SL`` when a new step triggers, else ``None``.
    """
    entry = trade_state.get("entry")
    direction = trade_state.get("direction")
    initial_stop = trade_state.get("initial_stop") or trade_state.get("sl")

    if entry is None or initial_stop is None:
        return None

    risk = abs(entry - initial_stop)
    if risk <= 0:
        return None

    trail_steps: List[Tuple[float, int, float]] = rules.get(
        "trail_config", DEFAULT_TRAIL_STEPS
    )
    if not trail_steps:
        return None

    # Initialise trailing state on first call
    if "trail_step" not in trade_state:
        trade_state["trail_step"] = 0
        trade_state["trail_consec"] = 0

    step_idx = trade_state["trail_step"]
    is_long = direction == "long"

    if step_idx >= len(trail_steps):
        return _evaluate_continuous_trailing(
            trade_state, candle_close, entry, risk, is_long, trail_steps, rules
        )

    trigger_r, candle_confirm, trail_to_r = trail_steps[step_idx]

    # The step must move the stop STRICTLY inside its own trigger for a touch to
    # be safe to arm on; the opening 0.5R→0.5R step does not, and stays on close.
    ref = _arm_reference(
        candle_close, candle_high, candle_low, is_long, rules,
        touch_safe=trail_to_r < trigger_r,
    )

    if is_long:
        trigger_price = entry + trigger_r * risk
        beyond = ref >= trigger_price
    else:
        trigger_price = entry - trigger_r * risk
        beyond = ref <= trigger_price

    if beyond:
        trade_state["trail_consec"] += 1
    else:
        trade_state["trail_consec"] = 0

    if trade_state["trail_consec"] >= candle_confirm:
        if is_long:
            new_sl = entry + trail_to_r * risk
        else:
            new_sl = entry - trail_to_r * risk

        trade_state["_pending_trail_sl"] = new_sl
        trade_state["_trail_label"] = f"trail_step_{step_idx}_to_{trail_to_r}R"
        # Snapshot the pre-fire step state. If the broker rejects the SL move
        # (e.g. MT5 10016 "Invalid stops") the monitor restores this so the
        # step re-fires on the next candle instead of being silently consumed,
        # which would leave the SL a full step behind until the NEXT threshold.
        trade_state["_pending_trail_rollback"] = {
            "trail_step": step_idx,
            "trail_consec": trade_state["trail_consec"],
        }
        trade_state["trail_step"] = step_idx + 1
        trade_state["trail_consec"] = 0
        return TRAIL_SL

    return None


def _evaluate_continuous_trailing(
    trade_state: Dict,
    candle_close: float,
    entry: float,
    risk: float,
    is_long: bool,
    trail_steps: List[Tuple[float, int, float]],
    rules: Dict,
) -> Optional[str]:
    """Continue trailing after all fixed steps have fired.

    Uses the last fixed step's trail_to_r as a baseline, then ratchets
    the SL forward in CONTINUOUS_TRAIL_STEP_R increments each time the
    close moves another increment beyond the current trail level.
    """
    step_r = rules.get("continuous_trail_step_r", CONTINUOUS_TRAIL_STEP_R)
    confirm = rules.get("continuous_trail_confirm", CONTINUOUS_TRAIL_CONFIRM)

    last_trail_to_r = trail_steps[-1][2] if trail_steps else 0.0

    if "cont_trail_r" not in trade_state:
        trade_state["cont_trail_r"] = last_trail_to_r
        trade_state["cont_trail_consec"] = 0

    current_trail_r = trade_state["cont_trail_r"]
    next_trigger_r = current_trail_r + step_r

    # Deliberately close-basis, whatever TRAIL_ARM_ON_TOUCH says: this tier
    # ratchets the stop to `current_trail_r + step_r`, which is the trigger
    # level itself. Arming on a touch would park the stop exactly at the price
    # that armed it, so the touch-safe condition (stop strictly inside trigger)
    # can never hold here.
    if is_long:
        trigger_price = entry + next_trigger_r * risk
        beyond = candle_close >= trigger_price
    else:
        trigger_price = entry - next_trigger_r * risk
        beyond = candle_close <= trigger_price

    if beyond:
        trade_state["cont_trail_consec"] += 1
    else:
        trade_state["cont_trail_consec"] = 0

    if trade_state["cont_trail_consec"] >= confirm:
        new_trail_r = current_trail_r + step_r
        if is_long:
            new_sl = entry + new_trail_r * risk
        else:
            new_sl = entry - new_trail_r * risk

        trade_state["_pending_trail_sl"] = new_sl
        trade_state["_trail_label"] = f"continuous_trail_to_{new_trail_r}R"
        # Same broker-rejection rollback contract as the fixed steps above.
        trade_state["_pending_trail_rollback"] = {
            "cont_trail_r": current_trail_r,
            "cont_trail_consec": trade_state["cont_trail_consec"],
        }
        trade_state["cont_trail_r"] = new_trail_r
        trade_state["cont_trail_consec"] = 0
        return TRAIL_SL

    return None


__all__ = ["evaluate_trade", "MOVE_SL_TO_BE", "TRAIL_SL", "EXIT_TP", "EXIT_SL", "PARTIAL_TP2"]
