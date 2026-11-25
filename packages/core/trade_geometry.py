"""Pure price geometry shared by live execution and backtesting."""

from __future__ import annotations

from .models import Side
from .volume import positive_decimal


def first_target_rr(entry: float, stop: float, target: float) -> float:
    """RR after sided geometry validation, preserving decimal boundaries."""
    e, s, t = (positive_decimal(v, name) for v, name in
               ((entry, "entry"), (stop, "stop"), (target, "TP1")))
    if e == s:
        raise ValueError("Stop distance must be positive")
    return float(abs(t - e) / abs(e - s))


def cap_structural_targets(
    entry: float,
    stop_loss: float,
    take_profit_1: float,
    take_profit_2: float,
    side: Side,
    max_rr: float,
) -> tuple[float, float]:
    """Cap TP2 without moving the structural first target.

    TP1 is the level against which trade quality is judged. Scaling it when
    an excessive TP2 is capped can silently turn an accepted 1.2R trade into
    a 0.6R trade. Reject incompatible geometry instead of inventing a new TP1.
    All inputs are final fill/stop prices; invalid geometry raises ValueError.
    """
    # Decimal subtraction keeps an exact boundary (e.g. 1.202 target,
    # 1.200 entry, 1.199 stop, 2R cap) from failing due to float cancellation.
    e, stop, first, second, cap = [positive_decimal(value, name) for value, name in (
        (entry, "entry"), (stop_loss, "stop"), (take_profit_1, "TP1"),
        (take_profit_2, "TP2"), (max_rr, "RR cap"),
    )]
    if side not in (Side.BUY, Side.SELL):
        raise ValueError(f"Invalid trade side: {side}")
    sign = 1 if side == Side.BUY else -1
    risk = sign * (e - stop)
    reward_1 = sign * (first - e)
    reward_2 = sign * (second - e)
    if risk <= 0 or reward_1 <= 0 or reward_2 < reward_1:
        raise ValueError("Invalid stop/target ordering or RR cap")
    capped_reward = min(reward_2, cap * risk)
    if capped_reward < reward_1:
        raise ValueError("TP2 RR cap would place TP2 before the structural TP1")
    final_tp2 = take_profit_2 if capped_reward == reward_2 else float(e + sign * capped_reward)
    return take_profit_1, final_tp2
