from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Literal, List, Dict, Any

import pandas as pd
from .session import ensure_ist, is_session_open
from .asset_registry import CRYPTO_SYMBOLS, MAINTENANCE_SYMBOLS


class MarketClosedError(RuntimeError):
    """Raised when a candle fetch is attempted while the market is closed.

    Subclasses :class:`RuntimeError` and always carries ``"market closed"`` in
    its message so that both ``isinstance(e, MarketClosedError)`` checks *and*
    the codebase's existing ``"market closed" in str(e).lower()`` handlers
    catch it.  Carries ``seconds_until_open`` so callers can sleep precisely
    instead of burning retries against a broker that is intentionally idle.
    """

    def __init__(self, symbol: str, seconds_until_open: int = 0):
        self.symbol = symbol
        self.seconds_until_open = int(seconds_until_open)
        super().__init__(
            f"{symbol} market closed — reopen in ~{self.seconds_until_open}s "
            f"(skipping candle fetch)"
        )


@dataclass
class Candle:
    """Simple OHLC candle."""

    time: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None


# ── Assets that trade 24/7 (no weekend closure) ─────────────────────────
# NOTE: These assets have no *weekend* gap, but brokers like Exness still
# impose a daily maintenance window (~20:55-22:05 UTC).  The daily-break
# logic below handles that separately.
_24_7_ASSETS = set(CRYPTO_SYMBOLS)

# Broker daily maintenance window applies to ALL symbols including crypto
_BROKER_MAINTENANCE_ASSETS = set(MAINTENANCE_SYMBOLS)


def is_expected_market_gap(
    prev_time: pd.Timestamp,
    curr_time: pd.Timestamp,
    asset: str | None = None,
) -> bool:
    """Return True if the gap between two candle timestamps is an expected
    recurring market closure rather than missing data.

    Forex, metals, and indices have a regular Friday-to-Sunday closure.
    Crypto has no weekly closure. Recognize broker maintenance separately.

    Parameters
    ----------
    prev_time, curr_time : pd.Timestamp
        Adjacent candle timestamps (must be tz-aware or both naive).
    asset : str, optional
        Asset symbol.  When None the function assumes the asset has weekend
        closures (conservative — avoids false-positive gap errors).
    """
    prev_ts = pd.Timestamp(prev_time)
    curr_ts = pd.Timestamp(curr_time)
    gap = curr_ts - prev_ts
    if gap <= pd.Timedelta(0):
        return False
    prev_utc = prev_ts.tz_convert("UTC") if prev_ts.tzinfo else prev_ts
    curr_utc = curr_ts.tz_convert("UTC") if curr_ts.tzinfo else curr_ts

    # ── Daily maintenance break (ALL broker symbols including crypto) ───
    # Brokers like Exness impose a daily maintenance window on all symbols
    # (typically ~20:55-22:05 UTC).  Accept any gap up to 6h where the
    # prev candle falls in the 17-22 UTC band and next resumes by 23:59.
    if (
        gap <= pd.Timedelta(hours=6)
        and prev_utc.date() == curr_utc.date()
        and (asset is None or asset.upper() in _BROKER_MAINTENANCE_ASSETS)
    ):
        if 17 <= prev_utc.hour <= 22 and 21 <= curr_utc.hour <= 23:
            return True

    # Crypto: no weekend gap (daily maintenance handled above)
    if asset and asset.upper() in _24_7_ASSETS:
        return False

    if not pd.Timedelta(hours=36) <= gap <= pd.Timedelta(hours=56):
        return False

    friday_close = prev_utc.weekday() == 4 and prev_utc.hour >= 20
    weekend_reopen = (curr_utc.weekday() == 6 and curr_utc.hour >= 20) or (
        curr_utc.weekday() == 0 and curr_utc.hour < 2
    )
    return friday_close and weekend_reopen


def check_series_completeness(candles: pd.DataFrame, interval: str = "5m") -> bool:
    """Return ``True`` if ``candles`` forms a contiguous time series.

    Parameters
    ----------
    candles:
        DataFrame indexed by datetime representing candles.
    interval:
        Expected spacing between consecutive candles.  Supports ``"Xm"`` and
        ``"Xh"`` style strings where *X* is an integer.

    Returns
    -------
    bool
        ``True`` when the index exactly matches the expected date range,
        ``False`` otherwise.
    """

    if candles.empty:
        return True

    if interval.endswith("m"):
        freq = f"{interval[:-1]}min"
    elif interval.endswith("h"):
        freq = f"{interval[:-1]}H"
    else:
        freq = interval

    idx = candles.index.sort_values()
    
    # FIX: Preserve timezone from source data
    tz = idx.tz if hasattr(idx, 'tz') else None
    expected = pd.date_range(start=idx[0], end=idx[-1], freq=freq, tz=tz)
    
    return idx.equals(expected)


def validate_candle_integrity(candles: List["Candle"], interval: str = "5m", expected_count: int | None = None, check_recency: bool = True, strict_mode: bool = True, allow_small_gaps: bool = False, symbol: str | None = None) -> tuple[bool, str]:
    """Validate that a list of candles forms a complete, contiguous series.
    
    This function performs comprehensive integrity checks to ensure that:
    1. The candle sequence has no gaps
    2. The expected number of candles is present (if specified)
    3. The latest candle is within the expected time window
    
    Parameters
    ----------
    candles:
        List of Candle objects to validate
    interval:
        Expected interval between candles (e.g., "5m", "15m")
    expected_count:
        Expected number of candles (optional)
        
    Returns
    -------
    tuple[bool, str]
        (is_valid, error_message). If is_valid is True, error_message is empty.
    """
    from datetime import datetime, timezone, timedelta
    
    if not candles:
        return True, ""
    
    # Convert to DataFrame for easier analysis
    df = pd.DataFrame([
        {
            'time': c.time,
            'open': c.open,
            'high': c.high,
            'low': c.low,
            'close': c.close,
            'volume': c.volume
        }
        for c in candles
    ])
    df = df.set_index('time').sort_index()
    
    # Check expected count - be more flexible in production with timing tolerance
    if expected_count is not None and len(candles) != expected_count:
        shortfall = expected_count - len(candles)
        # More lenient for timing-related issues during auto runs
        acceptable_shortfall = max(3, int(expected_count * 0.15))  # Allow 15% shortfall or 3 candles minimum
        
        if shortfall > acceptable_shortfall:
            error_msg = f"CRITICAL: Expected {expected_count} candles, got {len(candles)} - shortfall {shortfall} exceeds acceptable limit {acceptable_shortfall}"
            if strict_mode:
                return False, error_msg
            else:
                return False, error_msg
        elif shortfall > 0:
            # Warn but allow to proceed - common during timing edge cases
            warning_msg = f"WARNING: Expected {expected_count} candles, got {len(candles)} - shortfall {shortfall} is within acceptable limit {acceptable_shortfall} (timing tolerance)"
            return True, warning_msg
    
    # Production mode: Accept all broker data without validation
    # Broker data is considered authoritative regardless of volume or OHLC patterns
    
    # Check series completeness
    if not check_series_completeness(df, interval):
        # Find gaps
        if interval.endswith("m"):
            step_minutes = int(interval[:-1])
            freq = f"{step_minutes}min"
        elif interval.endswith("h"):
            step_hours = int(interval[:-1])
            freq = f"{step_hours}H"
        else:
            return False, f"Unsupported interval: {interval}"
        
        idx = df.index.sort_values()
        expected = pd.date_range(start=idx[0], end=idx[-1], freq=freq)
        missing = expected.difference(idx)
        
        if len(missing) > 0:
            # Filter out missing candles that fall within expected market gaps
            if symbol:
                actual_missing = []
                for ts in missing.sort_values():
                    # Find the candle just before this missing timestamp
                    before = idx[idx < ts]
                    if len(before) > 0:
                        prev = before[-1]
                        # Find the next actual candle after this missing timestamp
                        after = idx[idx > ts]
                        next_ts = after[0] if len(after) > 0 else ts + pd.Timedelta(freq)
                        if is_expected_market_gap(prev, next_ts, asset=symbol):
                            continue
                    actual_missing.append(ts)
                missing = pd.DatetimeIndex(actual_missing)

            if len(missing) > 0:
                missing_str = ", ".join([str(ts) for ts in missing[:5]])
                if len(missing) > 5:
                    missing_str += f" ... and {len(missing) - 5} more"

                if allow_small_gaps and len(missing) <= 2:
                    return True, f"WARNING: Small gaps detected but allowed: {missing_str}"

                return False, f"Missing candles at: {missing_str}"
    
    # Check that the latest candle is recent (within 2 intervals of now)
    if check_recency:
        now = datetime.now(timezone.utc)
        latest_candle = df.index[-1]

        # Skip recency check during broker daily maintenance window
        # (20:55-22:05 UTC) — candles are legitimately stale then
        if 20 <= now.hour <= 22 and now.minute >= 50 if now.hour == 20 else True:
            latest_pd = pd.Timestamp(latest_candle)
            if symbol and is_expected_market_gap(latest_pd, pd.Timestamp(now), asset=symbol):
                return True, ""

        if interval.endswith("m"):
            step_minutes = int(interval[:-1])
            if step_minutes == 5:
                max_age = timedelta(minutes=60)
            else:
                max_age = timedelta(minutes=step_minutes * 2)
        elif interval.endswith("h"):
            step_hours = int(interval[:-1])
            max_age = timedelta(hours=step_hours * 2)
        else:
            return False, f"Unsupported interval: {interval}"

        if now - latest_candle > max_age:
            return False, f"Latest candle is too old: {latest_candle} (now: {now})"
    
    return True, ""


def validate_candle_sequence_and_alignment(candles: List["Candle"], current_time = None, expected_count: int = 21, interval: str = "5m", backtest_mode: bool = False, asset: str | None = None) -> tuple[bool, str]:
    """Validate candle count, sequence, and timestamp alignment for trading execution.
    
    This function ensures:
    1. Exactly expected_count candles (or specified count) are present
    2. Candles are in correct chronological sequence
    3. Last candle timestamp is aligned to current interval (skipped in backtest_mode)
    
    Parameters
    ----------
    candles:
        List of Candle objects to validate
    current_time:
        Current time for alignment check (defaults to now, ignored in backtest_mode)
    expected_count:
        Expected number of candles (default 21)
    interval:
        Expected interval between candles ("5m" or "15m", default "5m")
    backtest_mode:
        If True, skip time alignment check (for historical candles)
        
    Returns
    -------
    tuple[bool, str]
        (is_valid, error_message)
    """
    from datetime import datetime, timezone, timedelta
    
    if current_time is None and not backtest_mode:
        current_time = datetime.now(timezone.utc)
    
    # Check candle count
    if len(candles) != expected_count:
        return False, f"CRITICAL: Expected exactly {expected_count} candles, got {len(candles)}"
    
    if not candles:
        return False, "CRITICAL: No candles provided"
    
    # Sort candles by time to ensure correct sequence
    sorted_candles = sorted(candles, key=lambda c: c.time)
    
    # Check chronological sequence and 5-minute intervals
    for i in range(1, len(sorted_candles)):
        prev_time = sorted_candles[i-1].time
        curr_time = sorted_candles[i].time
        
        if curr_time <= prev_time:
            return False, f"CRITICAL: Candles not in chronological order - candle {i-1} at {prev_time} >= candle {i} at {curr_time}"
        
        # Check interval based on specified timeframe
        time_diff = curr_time - prev_time
        if interval == "5m":
            expected_diff = timedelta(minutes=5)
            interval_name = "5-minute"
        elif interval == "15m":
            expected_diff = timedelta(minutes=15)
            interval_name = "15-minute"
        else:
            return False, f"CRITICAL: Unsupported interval '{interval}' - must be '5m' or '15m'"
        
        if time_diff != expected_diff:
            # Allow expected weekends and maintenance gaps.
            if is_expected_market_gap(prev_time, curr_time, asset):
                continue
            return False, f"CRITICAL: Invalid {interval_name} interval between candles - expected {expected_diff}, got {time_diff}"
    
    # Skip time alignment check in backtest mode (historical candles don't align to current time)
    if not backtest_mode:
        # Check last candle alignment - must be the last CLOSED candle
        # Since our broker adapters now fetch the last CLOSED candle (not current in-progress),
        # we validate against: floor(current_time) - interval
        last_candle_time = sorted_candles[-1].time
        
        # Floor current time to interval boundary
        current_minute = current_time.minute
        
        # Get interval in minutes
        if interval == "5m":
            interval_minutes = 5
            rounded_minute = (current_minute // 5) * 5
        elif interval == "15m":
            interval_minutes = 15
            rounded_minute = (current_minute // 15) * 15
        else:
            return False, f"CRITICAL: Unsupported interval '{interval}' for alignment check"
        
        # Floor to current interval boundary
        current_interval_start = current_time.replace(
            minute=rounded_minute,
            second=0,
            microsecond=0
        )
        
        # Subtract one interval to get the last CLOSED candle's expected time
        # This matches what our broker adapters now fetch
        expected_last_candle = current_interval_start - timedelta(minutes=interval_minutes)
        
        # FIXED: Last candle must be the previous CLOSED interval, not current
        # Examples at current time 14:07:
        # - For 5m: Current interval 14:05, expect last candle 14:00 (previous closed)
        # - For 5m: Current interval 14:05, NOT 14:05 (that's still in progress)
        # - For 15m: Current interval 14:00, expect last candle 13:45 (previous closed)
        
        # Last candle should match the previous closed interval
        if last_candle_time != expected_last_candle:
            return False, f"CRITICAL: Last candle time {last_candle_time} not aligned to expected last closed {interval} interval {expected_last_candle}. Current time: {current_time}. Method: validate_candle_sequence_and_alignment"
    
    return True, ""


def filter_consecutive_candles(
    candles: List[Dict[str, Any]],
    interval: str = "5m",
    start_from_first: bool = True,
    asset: str | None = None,
) -> List[Dict[str, Any]]:
    """Filter candles to only include those with strict consecutive intervals.
    
    This function filters a list of candle dictionaries to ensure each candle
    is exactly the specified interval (5m or 15m) after the previous candle.
    Candles with gaps or irregular intervals are removed.
    
    Parameters
    ----------
    candles : List[Dict[str, Any]]
        List of candle dictionaries with 'time' field
    interval : str
        Expected interval between candles ("5m" or "15m", default "5m")
    start_from_first : bool
        If True, start from the first candle. If False, start from last candle and work backwards.
    asset : str, optional
        Asset symbol.  When provided, expected market closures (e.g. forex
        weekends) are treated as valid continuations instead of gaps.
        
    Returns
    -------
    List[Dict[str, Any]]
        Filtered list of candles with strict consecutive intervals
    """
    if not candles:
        return []
    
    # Parse interval
    if interval == "5m":
        expected_diff = timedelta(minutes=5)
    elif interval == "15m":
        expected_diff = timedelta(minutes=15)
    else:
        raise ValueError(f"Unsupported interval '{interval}' - must be '5m' or '15m'")
    
    # Sort candles by time
    sorted_candles = sorted(candles, key=lambda c: pd.Timestamp(c.get('time')))
    
    if len(sorted_candles) == 0:
        return []
    
    filtered = [sorted_candles[0]]  # Always include first candle

    for i in range(1, len(sorted_candles)):
        # Check interval from LAST FILTERED candle (not previous in sorted list)
        # This allows skipping gaps and finding consecutive sequences after them
        prev_filtered_time = pd.Timestamp(filtered[-1].get('time'))
        curr_time = pd.Timestamp(sorted_candles[i].get('time'))

        time_diff = curr_time - prev_filtered_time

        # Only include if exactly the expected interval from last filtered candle
        # or if the gap is an expected market closure (weekend/holiday)
        if time_diff == expected_diff:
            filtered.append(sorted_candles[i])
        elif is_expected_market_gap(prev_filtered_time, curr_time, asset):
            filtered.append(sorted_candles[i])
        # If gap detected, skip this candle (don't break - check next ones)
        # This allows recovering from gaps and continuing consecutive sequences

    return filtered


def resample_ohlc(df: pd.DataFrame, tf: Literal["5m", "15m"]) -> pd.DataFrame:
    """Resample minute data to the requested timeframe.

    Parameters
    ----------
    df:
        Dataframe indexed by datetime with ``open``, ``high``, ``low`` and
        ``close`` columns representing 1‑minute candles.
    tf:
        Target timeframe. Only ``"5m"`` and ``"15m"`` are supported.

    Returns
    -------
    pandas.DataFrame
        Resampled OHLC data with a datetime index.
    """

    if tf not in {"5m", "15m"}:
        raise ValueError("tf must be '5m' or '15m'")

    rule = "5min" if tf == "5m" else "15min"
    agg_map: dict[str, str] = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
    }
    if "volume" in df.columns:
        agg_map["volume"] = "sum"
    agg = df.resample(rule).agg(agg_map)

    # Historical 15‑minute data in this project is labelled with the close time
    # of each interval, whereas 5‑minute candles are labelled with the open
    # time. Reproduce that behaviour here so downstream code receives identical
    # timestamps to the pre‑refactor CSVs.
    if tf == "15m":
        agg = agg.shift(freq=rule)

    agg.index = agg.index.map(ensure_ist)
    agg = agg.dropna()
    return agg[agg.index.map(is_session_open)]

try:  # pragma: no cover - optional when repo unavailable
    from packages.db.repositories import candles as _repo
except Exception:  # pragma: no cover - repository unavailable
    _repo = None  # type: ignore[assignment]


async def store_candles(asset: str, candles: pd.DataFrame) -> None:
    """Persist *candles* for *asset* using the candles repository.

    The dataframe must be indexed by timestamp and contain ``open``, ``high``,
    ``low`` and ``close`` columns.
    """

    if _repo is None:  # pragma: no cover - requires repository runtime
        raise RuntimeError("candles repository unavailable")
    docs = []
    has_vol = "volume" in candles.columns
    for ts, row in candles.iterrows():
        doc = {
            "asset": asset,
            "ts": ts.to_pydatetime(),
            "open": row.open,
            "high": row.high,
            "low": row.low,
            "close": row.close,
        }
        if has_vol:
            doc["volume"] = float(row.volume)
        docs.append(doc)
    await _repo.insert_many(docs)


async def load_candles(
    asset: str,
    *,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
    limit: int = 0,
) -> pd.DataFrame:
    """Return candles for *asset* from the repository as a dataframe."""

    if _repo is None:  # pragma: no cover - requires repository runtime
        raise RuntimeError("candles repository unavailable")
    start_dt = start.to_pydatetime() if start is not None else None
    end_dt = end.to_pydatetime() if end is not None else None
    docs = await _repo.get_many(asset, start=start_dt, end=end_dt, limit=limit)
    if not docs:
        return pd.DataFrame(columns=["open", "high", "low", "close"])
    df = pd.DataFrame(docs).set_index("ts").sort_index()
    df.index = pd.to_datetime(df.index)
    cols = ["open", "high", "low", "close"]
    if "volume" in df.columns:
        cols.append("volume")
    return df[cols]
