"""
Candle validation utilities for production trading system.

This module provides strict validation for candle data to ensure:
- Candles are closed (not in-progress)
- Candles are aligned to interval boundaries
- Candle sequences have no gaps
- Data quality is guaranteed before trigger checks

Zero tolerance for stale/incomplete data in production.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import isclose, isfinite
from typing import List, Tuple, Any

from .candles import Candle, is_expected_market_gap
from .logger import get_logger
from .session import exness_expected_bar_gap

logger = get_logger(__name__)


def is_candle_closed(
    candle_time: datetime, 
    interval_minutes: int, 
    tolerance_seconds: int = 0,
    *,
    current_time: datetime | None = None,
) -> bool:
    """
    Verify candle is closed and aligned to interval boundary.
    
    Parameters
    ----------
    candle_time : datetime
        The timestamp of the candle
    interval_minutes : int
        Candle interval (5, 15, etc.)
    tolerance_seconds : int
        Tolerance for closure check (0 = strict, must be fully closed)
    
    Returns
    -------
    bool
        True if candle is closed and valid
    
    Examples
    --------
    >>> # Current time: 10:05:30 UTC
    >>> candle_time = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
    >>> is_candle_closed(candle_time, 5, tolerance_seconds=0)
    True  # 10:00 candle closed at 10:05, now 10:05:30
    
    >>> candle_time = datetime(2024, 1, 1, 10, 5, tzinfo=timezone.utc)
    >>> is_candle_closed(candle_time, 5, tolerance_seconds=0)
    False  # 10:05 candle still in progress at 10:05:30
    """
    # Ensure UTC timezone
    if candle_time.tzinfo is None:
        candle_time = candle_time.replace(tzinfo=timezone.utc)
    
    now = current_time if current_time is not None else datetime.now(timezone.utc)
    
    # Must be on interval boundary (0, 5, 10, 15, 20, 25, 30, etc.)
    if candle_time.minute % interval_minutes != 0:
        logger.debug(
            f"Candle not aligned to {interval_minutes}m boundary: {candle_time}",
            extra={"candle_time": candle_time, "minute": candle_time.minute}
        )
        return False
    
    # Must have seconds and microseconds at 0
    if candle_time.second != 0 or candle_time.microsecond != 0:
        logger.debug(
            f"Candle time not at exact minute boundary: {candle_time}",
            extra={"second": candle_time.second, "microsecond": candle_time.microsecond}
        )
        return False
    
    # Calculate expected close time
    expected_close = candle_time + timedelta(minutes=interval_minutes)
    seconds_since_close = (now - expected_close).total_seconds()
    
    # With 0 tolerance: candle must be fully closed (now >= expected_close)
    # With tolerance: allow some leeway for broker delays
    is_closed = seconds_since_close >= -tolerance_seconds
    
    if not is_closed:
        logger.debug(
            f"Candle not yet closed: {candle_time}",
            extra={
                "candle_time": candle_time,
                "expected_close": expected_close,
                "now": now,
                "seconds_until_close": -seconds_since_close
            }
        )
    
    return is_closed


def validate_candle_sequence(
    candles: List[Candle], 
    interval_minutes: int,
    check_gaps: bool = True,
    asset: str | None = None,
    *,
    current_time: datetime | None = None,
) -> Tuple[bool, str]:
    """
    Validate candle sequence for gaps, alignment, and closure.
    
    Parameters
    ----------
    candles : List[Candle]
        List of candles to validate
    interval_minutes : int
        Candle interval (5, 15, etc.)
    check_gaps : bool
        Whether to check for gaps in sequence (default: True)
    
    Returns
    -------
    Tuple[bool, str]
        (is_valid, error_message) - empty message if valid
    
    Examples
    --------
    >>> candles = [candle1, candle2, candle3]  # Properly spaced 5m candles
    >>> valid, msg = validate_candle_sequence(candles, 5)
    >>> print(valid)
    True
    """
    if not candles:
        return False, "Empty candle list"
    
    # Check last candle is closed (most critical)
    last_candle = candles[-1]
    if not is_candle_closed(last_candle.time, interval_minutes, current_time=current_time, tolerance_seconds=0):
        error_msg = f"Last candle not closed: {last_candle.time}"
        logger.warning(error_msg, extra={"last_candle_time": last_candle.time})
        return False, error_msg
    
    # Check all candles are on interval boundaries
    for i, candle in enumerate(candles):
        # Ensure timezone
        candle_time = candle.time
        if candle_time.tzinfo is None:
            candle_time = candle_time.replace(tzinfo=timezone.utc)
        
        if candle_time.minute % interval_minutes != 0:
            error_msg = f"Candle {i} not aligned to {interval_minutes}m boundary: {candle_time}"
            logger.warning(error_msg, extra={"index": i, "candle_time": candle_time})
            return False, error_msg
        
        if candle_time.second != 0 or candle_time.microsecond != 0:
            error_msg = f"Candle {i} not at exact minute boundary: {candle_time}"
            logger.warning(error_msg, extra={"index": i, "candle_time": candle_time})
            return False, error_msg
    
    # Check for gaps (optional but recommended for data quality)
    if check_gaps and len(candles) > 1:
        expected_delta = timedelta(minutes=interval_minutes)
        
        for i in range(1, len(candles)):
            prev_time = candles[i-1].time
            curr_time = candles[i].time
            
            # Ensure timezone
            if prev_time.tzinfo is None:
                prev_time = prev_time.replace(tzinfo=timezone.utc)
            if curr_time.tzinfo is None:
                curr_time = curr_time.replace(tzinfo=timezone.utc)
            
            actual_delta = curr_time - prev_time
            
            if actual_delta != expected_delta:
                expected_gap = exness_expected_bar_gap(asset, prev_time, curr_time, interval_minutes) if asset else None
                if expected_gap is None:
                    expected_gap = is_expected_market_gap(prev_time, curr_time, asset)
                if expected_gap:
                    logger.info(
                        f"Weekend/holiday gap detected between {prev_time} and {curr_time}",
                        extra={
                            "gap_hours": actual_delta.total_seconds() / 3600,
                            "prev_time": prev_time,
                            "curr_time": curr_time
                        }
                    )
                    continue
                
                error_msg = (
                    f"Gap detected between candle {i-1} and {i}: "
                    f"{prev_time} -> {curr_time} "
                    f"(expected {expected_delta}, got {actual_delta})"
                )
                logger.warning(
                    error_msg,
                    extra={
                        "index": i,
                        "prev_time": prev_time,
                        "curr_time": curr_time,
                        "expected_delta": expected_delta,
                        "actual_delta": actual_delta
                    }
                )
                return False, error_msg
    
    # All validations passed
    return True, ""


def validate_live_candle_window(
    candles: List[Candle],
    *,
    expected_count: int,
    interval_minutes: int,
    asset: str,
    current_time: datetime,
) -> Tuple[bool, str]:
    """Require the complete, ordered and fresh input window before prompting."""
    if len(candles) != expected_count:
        return False, f"Expected {expected_count} candles, got {len(candles)}"

    valid, error = validate_candle_sequence(
        candles, interval_minutes, asset=asset, current_time=current_time
    )
    if not valid:
        return False, error

    current_time = current_time.astimezone(timezone.utc)
    interval_start = current_time.replace(
        minute=current_time.minute // interval_minutes * interval_minutes,
        second=0,
        microsecond=0,
    )
    expected_last = interval_start - timedelta(minutes=interval_minutes)
    if candles[-1].time != expected_last:
        return False, f"Expected last closed bar at {expected_last}, got {candles[-1].time}"

    for candle in candles:
        valid, error = validate_candle_data_quality(candle)
        if not valid:
            return False, f"Invalid candle at {candle.time}: {error}"
    return True, ""


def validate_timeframe_consistency(
    candles_5m: List[Candle], candles_15m: List[Candle]
) -> Tuple[bool, str]:
    """Compare closed M15 bars with complete overlapping M5 triplets.

    A timestamp can be closed while its cached OHLC still contains the first
    ticks of that bar. Do not synthesize a replacement or choose one timeframe
    as truth: reject the pair so the caller can re-fetch both from the broker.
    Bars outside the shared history (or across a session break) are not inferred.
    """
    by_time = {c.time: c for c in candles_5m}
    for bar in candles_15m:
        parts = [by_time.get(bar.time + timedelta(minutes=m)) for m in (0, 5, 10)]
        if any(part is None for part in parts):
            continue
        expected = {
            "open": parts[0].open,
            "high": max(part.high for part in parts),
            "low": min(part.low for part in parts),
            "close": parts[-1].close,
        }
        if bar.volume is not None and all(part.volume is not None for part in parts):
            expected["volume"] = sum(part.volume for part in parts)
        for field, value in expected.items():
            actual = getattr(bar, field)
            if not isclose(float(actual), float(value), rel_tol=1e-9, abs_tol=1e-10):
                return False, (
                    f"M5/M15 mismatch at {bar.time}: {field} "
                    f"M15={actual}, M5 aggregate={value}"
                )
    return True, ""


def validate_candle_data_quality(candle: Candle) -> Tuple[bool, str]:
    """
    Validate individual candle data quality.
    
    Checks for:
    - Valid OHLC relationships (high >= low, open/close within range)
    - Non-zero values
    - Valid volume (if present)
    
    Parameters
    ----------
    candle : Candle
        Candle to validate
    
    Returns
    -------
    Tuple[bool, str]
        (is_valid, error_message)
    """
    try:
        # Check for None values
        if candle.open is None or candle.high is None or candle.low is None or candle.close is None:
            return False, "Candle has None OHLC values"
        
        # Convert to float for comparison
        o, h, l, c = float(candle.open), float(candle.high), float(candle.low), float(candle.close)
        
        # Check for zero/negative values
        if not all(isfinite(price) and price > 0 for price in (o, h, l, c)):
            return False, f"Candle has invalid OHLC values: O={o}, H={h}, L={l}, C={c}"
        
        # Validate OHLC relationships
        if h < l:
            return False, f"High ({h}) less than low ({l})"
        
        if o > h or o < l:
            return False, f"Open ({o}) outside high-low range [{l}, {h}]"
        
        if c > h or c < l:
            return False, f"Close ({c}) outside high-low range [{l}, {h}]"
        
        # Check volume if present
        if candle.volume is not None:
            v = float(candle.volume)
            if not isfinite(v) or v < 0:
                return False, f"Invalid volume: {v}"
        
        return True, ""
        
    except (ValueError, TypeError) as e:
        return False, f"Data type error in candle: {e}"


def get_candle_close_timestamp(candle_time: datetime, interval_minutes: int) -> datetime:
    """
    Get the expected close timestamp for a candle.
    
    Parameters
    ----------
    candle_time : datetime
        Start time of the candle
    interval_minutes : int
        Candle interval
    
    Returns
    -------
    datetime
        Expected close time
    """
    if candle_time.tzinfo is None:
        candle_time = candle_time.replace(tzinfo=timezone.utc)
    
    return candle_time + timedelta(minutes=interval_minutes)


def seconds_until_candle_close(candle_time: datetime, interval_minutes: int) -> float:
    """
    Calculate seconds until candle closes.
    
    Returns negative value if candle is already closed.
    
    Parameters
    ----------
    candle_time : datetime
        Start time of the candle
    interval_minutes : int
        Candle interval
    
    Returns
    -------
    float
        Seconds until close (negative if already closed)
    """
    if candle_time.tzinfo is None:
        candle_time = candle_time.replace(tzinfo=timezone.utc)
    
    now = datetime.now(timezone.utc)
    expected_close = candle_time + timedelta(minutes=interval_minutes)
    
    return (expected_close - now).total_seconds()


