"""Decimal lot normalization without increasing an approved quantity."""

from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import math


def positive_decimal(value, name: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be finite and positive") from exc
    if isinstance(value, bool) or not number.is_finite() or number <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return number


def floor_volume(value, step, minimum, maximum=None) -> float:
    """Floor to a valid step; reject below minimum, never round up to it."""
    approved = positive_decimal(value, "volume")
    increment = positive_decimal(step, "volume step")
    lower = positive_decimal(minimum, "minimum volume")
    upper = positive_decimal(maximum, "maximum volume") if maximum is not None else None
    if upper is not None and upper < lower:
        raise ValueError("Maximum volume is below minimum volume")
    capped = min(approved, upper) if upper is not None else approved
    volume = (capped / increment).to_integral_value(rounding=ROUND_FLOOR) * increment
    if volume < lower or volume <= 0:
        raise ValueError("Approved quantity is below broker minimum; refusing to increase risk")
    normalized = float(volume)
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError("Normalized volume is outside the supported numeric range")
    return normalized
