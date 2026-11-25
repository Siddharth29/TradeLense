"""The candle contract every strategy execution must satisfy — live and backtest.

Why this module exists
----------------------
Two engines executed judge-generated code against 5-minute candles, and only one of
them checked the candles first:

* the **backtest** (``code_executor.TradingCodeExecutor``) rejected non-consecutive
  bars and misaligned windows before running a line of generated code;
* the **live** path (``code_sniper_executor.CodeSniperExecutor.execute_strategy``)
  went straight to the sandbox, and separately coerced a missing OHLC field to
  ``0.0`` — a candle claiming the market traded to zero.

So the path risking real money was validated *less* than the one risking nothing
(audit 2026-08-27, C4). This module is the single contract both now call, so they
cannot drift apart again: a rule added here binds both engines or neither.

What it enforces
----------------
1. **Every price is real.** A ``None``/non-numeric open/high/low/close is a reject,
   never a zero. A zero low turns every range, wick and support-distance test in the
   generated code into nonsense, and the entry/SL/TP it returns are then derived
   from a market that never existed.
2. **The window is continuous.** Consecutive 5-minute bars, with weekend and
   session breaks excused via ``is_expected_market_gap`` — a "retest of the level
   8 bars ago" must not be tested against a level from a different session.
3. **The window is the expected length**, so the generated code's negative indexing
   addresses the bars the judge was promised.
4. **The newest bar is fresh** (live only — a replayed bar is never "stale").

Failures raise :class:`CandleContractError`. Callers decide whether that skips a
cycle or aborts a backtest, but nobody gets to trade through it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .candles import is_expected_market_gap
from .logger import get_logger

logger = get_logger(__name__)

BAR_INTERVAL = timedelta(minutes=5)
PRICE_FIELDS = ("open", "high", "low", "close")


class CandleContractError(ValueError):
    """A candle window is not fit to execute a strategy against."""


def _bar_time(candle: Mapping[str, Any]) -> Optional[datetime]:
    """The candle's bar-open timestamp, or None when it cannot be read."""
    value = candle.get("time") if isinstance(candle, Mapping) else getattr(candle, "time", None)
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        import pandas as pd
        stamp = pd.Timestamp(value)
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("UTC")
        return stamp.to_pydatetime()
    except Exception:
        return None


def require_real_prices(candle: Mapping[str, Any], where: str) -> Dict[str, float]:
    """Return the candle's OHLC as positive finite floats, or raise.

    The rule this replaces was ``float(candle.get("high") or 0)``. A missing price
    is missing data — it is not a price of zero, and pretending otherwise is how a
    malformed bar reaches the strategy as a tradeable one.
    """
    out: Dict[str, float] = {}
    for field in PRICE_FIELDS:
        raw = candle.get(field) if isinstance(candle, Mapping) else getattr(candle, field, None)
        if raw is None:
            raise CandleContractError(f"{where}: '{field}' is missing")
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise CandleContractError(f"{where}: '{field}' is not numeric ({raw!r})") from exc
        if not isfinite(value) or value <= 0:
            raise CandleContractError(f"{where}: '{field}' is {value}, which is not a price")
        out[field] = value
    if out["high"] < out["low"]:
        raise CandleContractError(f"{where}: high {out['high']} is below low {out['low']}")
    return out


def validate_strategy_window(
    historical_candles: Sequence[Mapping[str, Any]],
    current_candle: Mapping[str, Any],
    *,
    asset: Optional[str] = None,
    expected_history: Optional[int] = None,
    now: Optional[datetime] = None,
    max_staleness_minutes: Optional[int] = None,
) -> None:
    """Raise :class:`CandleContractError` unless this window may drive a strategy.

    ``now`` and ``max_staleness_minutes`` are the live-only freshness check; a
    backtest passes neither, because a replayed bar is old on purpose.
    """
    window = list(historical_candles) + [current_candle]
    if not window:
        raise CandleContractError("empty candle window")

    if expected_history is not None and len(historical_candles) != expected_history:
        raise CandleContractError(
            f"expected {expected_history} historical candles, got {len(historical_candles)}"
        )

    times: List[datetime] = []
    for index, candle in enumerate(window):
        where = f"candle[{index}]"
        require_real_prices(candle, where)
        stamp = _bar_time(candle)
        if stamp is None:
            raise CandleContractError(f"{where}: unreadable timestamp")
        times.append(stamp)

    for index in range(1, len(times)):
        gap = times[index] - times[index - 1]
        if gap == BAR_INTERVAL:
            continue
        if gap <= timedelta(0):
            raise CandleContractError(
                f"candles out of order at index {index}: "
                f"{times[index - 1].isoformat()} -> {times[index].isoformat()}"
            )
        if is_expected_market_gap(times[index - 1], times[index], asset):
            logger.debug(
                "strategy window: expected session gap",
                extra={"asset": asset, "from": times[index - 1].isoformat(),
                       "to": times[index].isoformat()},
            )
            continue
        raise CandleContractError(
            f"non-consecutive candles at index {index}: "
            f"{times[index - 1].isoformat()} -> {times[index].isoformat()} "
            f"(gap {gap}, expected {BAR_INTERVAL})"
        )

    if now is not None and times[-1] + BAR_INTERVAL > now:
        raise CandleContractError("newest candle has not closed")

    if now is not None and max_staleness_minutes is not None:
        age_minutes = (now - times[-1]).total_seconds() / 60
        if age_minutes > max_staleness_minutes:
            raise CandleContractError(
                f"newest candle is {age_minutes:.1f}min old "
                f"(limit {max_staleness_minutes}min) — the market has moved on"
            )


__all__ = [
    "CandleContractError",
    "require_real_prices",
    "validate_strategy_window",
    "BAR_INTERVAL",
]
