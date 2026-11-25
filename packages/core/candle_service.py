"""Service for retrieving candle data from a broker or MongoDB.

Optimized for performance with smart caching, minimal I/O, and clean architecture.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

import pandas as pd

from .candles import Candle, load_candles, resample_ohlc, MarketClosedError
from .logger import get_logger
from .config import (
    CANDLE_SERVICE_DEFAULT_5M,
    CANDLE_SERVICE_DEFAULT_15M,
    CANDLE_SERVICE_CACHE_SIZE_5M,
    CANDLE_SERVICE_CACHE_SIZE_15M,
)

logger = get_logger(__name__)


# ============================================================================
# EXCEPTIONS
# ============================================================================


class NoCandleData(Exception):
    """Raised when no candle data is available."""


class CandleFetchFailed(Exception):
    """Raised when candle fetching fails with specific error details."""
    
    def __init__(self, message: str, error: str = "", error_type: str = ""):
        super().__init__(message)
        self.error = error
        self.error_type = error_type


# ============================================================================
# CANDLE SERVICE
# ============================================================================


@dataclass
class CandleService:
    """Fetch and cache 5m and 15m candles for trading symbols.

    Implements intelligent caching with automatic invalidation based on
    candle window boundaries. Minimizes broker API calls while ensuring
    data freshness.

    Parameters
    ----------
    broker : Any | None
        Broker instance with async ``candles(token, symbol, interval, limit)`` method.
        Primary data source when provided. Falls back to MongoDB if None.
    store : Any | None
        MongoDB store instance for loading historical candles.
    token : str | None
        Authentication token for broker API calls.
    five_count : int
        Default number of 5-minute candles to return (default: 20).
    fifteen_count : int
        Default number of 15-minute candles to return (default: 30).
    """

    broker: Any | None = None
    store: Any | None = None
    token: str | None = None
    five_count: int = CANDLE_SERVICE_DEFAULT_5M
    fifteen_count: int = CANDLE_SERVICE_DEFAULT_15M

    # Internal cache configuration
    _cache_size_5m: int = CANDLE_SERVICE_CACHE_SIZE_5M
    _cache_size_15m: int = CANDLE_SERVICE_CACHE_SIZE_15M
    
    # Ring-buffer caches (symbol_interval -> List[Candle])
    _cache_5m: Dict[str, List[Candle]] = field(default_factory=lambda: defaultdict(list))
    _cache_15m: Dict[str, List[Candle]] = field(default_factory=lambda: defaultdict(list))
    
    # Timestamp tracking for cache invalidation
    _last_cached_time_5m: Dict[str, datetime] = field(default_factory=dict)
    _last_cached_time_15m: Dict[str, datetime] = field(default_factory=dict)

    # ========================================================================
    # CACHE KEY & TIME UTILITIES
    # ========================================================================

    @staticmethod
    def _get_cache_key(symbol: str, interval: str) -> str:
        """Generate cache key from symbol and interval."""
        return f"{symbol}_{interval}"
    
    @staticmethod
    def _convert_to_datetime(timestamp: Any) -> datetime:
        """Convert various timestamp formats to timezone-aware datetime.
        
        Handles pd.Timestamp, datetime, and string formats.
        Always returns UTC-localized datetime.
        """
        if hasattr(timestamp, 'to_pydatetime'):
            return timestamp.to_pydatetime()
        if isinstance(timestamp, datetime):
            return timestamp
        return pd.Timestamp(timestamp).to_pydatetime()
    
    @staticmethod
    def _get_candle_window_start(dt: datetime, interval_minutes: int) -> datetime:
        """Calculate the start time of the candle window for given datetime."""
        return dt.replace(
            minute=(dt.minute // interval_minutes) * interval_minutes,
            second=0,
            microsecond=0
        )
    
    @staticmethod
    def _get_expected_last_closed(current_time: datetime, interval_minutes: int) -> datetime:
        """
        Calculate expected time of last closed candle.
        
        Examples at 16:37:
        - 5m interval: current is 16:35, last closed is 16:30
        - 15m interval: current is 16:30, last closed is 16:15
        
        Parameters
        ----------
        current_time : datetime
            Current UTC time
        interval_minutes : int
            Candle interval in minutes (5 or 15)
        
        Returns
        -------
        datetime
            Expected time of the last closed candle
        """
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)
        
        minute = current_time.minute
        rounded = (minute // interval_minutes) * interval_minutes
        current_interval = current_time.replace(minute=rounded, second=0, microsecond=0)
        
        # Last closed is previous interval
        return current_interval - timedelta(minutes=interval_minutes)

    # ========================================================================
    # CACHE MANAGEMENT
    # ========================================================================

    def _update_cache(
        self, symbol: str, interval: str, candles: List[Candle], *, requested_count: int = 0
    ) -> None:
        """Update ring-buffer cache with latest candles."""
        if not candles:
            return
        
        cache_key = self._get_cache_key(symbol, interval)
        cache = self._cache_5m if interval == "5m" else self._cache_15m
        default_size = self._cache_size_5m if interval == "5m" else self._cache_size_15m
        cache_size = max(default_size, requested_count, len(cache.get(cache_key, ())))
        latest_time = self._convert_to_datetime(candles[-1].time)
        
        if interval == "5m":
            self._cache_5m[cache_key] = candles[-cache_size:]
            self._last_cached_time_5m[cache_key] = latest_time
        elif interval == "15m":
            self._cache_15m[cache_key] = candles[-cache_size:]
            self._last_cached_time_15m[cache_key] = latest_time
        
        logger.debug(
            f"Cache updated: {symbol} {interval} - {len(candles)} candles, "
            f"latest: {latest_time}"
        )
    
    def _retrieve_from_cache(
        self,
        symbol: str,
        interval: str,
        limit: int
    ) -> List[Candle] | None:
        """Retrieve candles from cache without staleness validation.
        
        Returns None if cache is empty or insufficient data.
        Staleness checks are performed separately via _needs_fresh_data().
        """
        cache_key = self._get_cache_key(symbol, interval)
        cached = (
            self._cache_5m.get(cache_key, []) if interval == "5m" 
            else self._cache_15m.get(cache_key, [])
        )
        
        if not cached:
            return None
        
        # Return last N candles (all if insufficient)
        return cached[-limit:] if len(cached) >= limit else cached
    
    def _needs_fresh_data(self, symbol: str, interval: str) -> bool:
        """
        Enhanced cache expiry check with closure validation.
        
        Returns True if:
        1. Cache is empty
        2. We've entered a new candle window
        3. Latest cached candle is not closed yet
        """
        cache_key = self._get_cache_key(symbol, interval)
        interval_minutes = 5 if interval == "5m" else 15
        
        # Retrieve cache metadata
        if interval == "5m":
            last_time = self._last_cached_time_5m.get(cache_key)
            cached_candles = self._cache_5m.get(cache_key)
        else:
            last_time = self._last_cached_time_15m.get(cache_key)
            cached_candles = self._cache_15m.get(cache_key)
        
        # No cache = need fresh data
        if not last_time or not cached_candles:
            return True
        
        now = datetime.now(timezone.utc)
        
        # NEW: Validate latest cached candle is closed
        from .candle_validation import is_candle_closed
        latest_candle = cached_candles[-1]
        if not is_candle_closed(latest_candle.time, interval_minutes, current_time=now):
            logger.debug(
                f"Latest cached candle not closed yet: {latest_candle.time}",
                extra={"symbol": symbol, "interval": interval}
            )
            return True
        
        # Check if there's a newer closed candle available
        return last_time < self._get_expected_last_closed(now, interval_minutes)
    
    def has_cache(self, symbol: str) -> bool:
        """Check if any cache exists for symbol (either 5m or 15m)."""
        key_5m = self._get_cache_key(symbol, "5m")
        key_15m = self._get_cache_key(symbol, "15m")
        return bool(self._cache_5m.get(key_5m) or self._cache_15m.get(key_15m))
    
    def clear_cache(self, symbol: str | None = None) -> None:
        """Clear cache for specific symbol or all symbols."""
        if symbol:
            key_5m = self._get_cache_key(symbol, "5m")
            key_15m = self._get_cache_key(symbol, "15m")
            
            self._cache_5m.pop(key_5m, None)
            self._cache_15m.pop(key_15m, None)
            self._last_cached_time_5m.pop(key_5m, None)
            self._last_cached_time_15m.pop(key_15m, None)
            
            logger.info(f"Cache cleared for symbol: {symbol}")
        else:
            self._cache_5m.clear()
            self._cache_15m.clear()
            self._last_cached_time_5m.clear()
            self._last_cached_time_15m.clear()
            
            logger.info("All caches cleared")
    
    async def _fetch_interval_from_broker(
        self,
        symbol: str,
        interval: str,
        limit: int,
        *,
        max_retries: int = 3,
        base_delay: float = 1.0,
    ) -> List[Candle]:
        """Fetch a specific interval from the broker with retry and validation."""
        from .candle_validation import validate_candle_sequence

        if limit <= 0:
            return []

        if not self.broker or not hasattr(self.broker, "candles"):
            raise NoCandleData(f"No broker available for {symbol}")

        if hasattr(self.broker, "_mt5") and getattr(self.broker, "_mt5") is None:
            raise RuntimeError("MT5 broker not connected")

        interval_minutes = 5 if interval == "5m" else 15
        last_error: Exception | None = None

        for attempt in range(max_retries + 1):
            try:
                args = (self.token, symbol, interval, limit) if self.token else (symbol, interval, limit)
                raw = await self.broker.candles(*args)
                candles = self._normalize_candles(raw) if raw else []

                if not candles:
                    raise CandleFetchFailed(
                        f"No {interval} data for {symbol}",
                        error=f"no_{interval}_data"
                    )

                # If the last candle is still in-progress (not yet closed), strip it
                # and use the rest.  This happens when the agent runs mid-candle —
                # MT5 always includes the forming candle.  Retrying won't help since
                # the candle genuinely hasn't closed yet.  Using the previous closed
                # candles is the correct behaviour for analysis.
                from .candle_validation import is_candle_closed
                if candles and not is_candle_closed(candles[-1].time, interval_minutes, tolerance_seconds=0):
                    logger.info(
                        f"Stripping unclosed last {interval} candle for {symbol}: {candles[-1].time}",
                        extra={
                            "symbol": symbol,
                            "interval": interval,
                            "stripped_candle": str(candles[-1].time),
                            "remaining": len(candles) - 1,
                        },
                    )
                    candles = candles[:-1]
                    if not candles:
                        raise CandleFetchFailed(
                            f"No closed {interval} candles for {symbol} after stripping in-progress candle",
                            error="no_closed_candles",
                            error_type=interval,
                        )

                valid, message = validate_candle_sequence(candles, interval_minutes, asset=symbol)
                if not valid:
                    raise CandleFetchFailed(
                        f"Incomplete {interval} candles for {symbol}: {message}",
                        error="incomplete_candles",
                        error_type=interval,
                    )
                
                logger.debug(
                    "Broker fetch succeeded",
                    extra={
                        "symbol": symbol,
                        "interval": interval,
                        "count": len(candles),
                        "attempt": attempt + 1,
                    },
                )

                return candles

            except MarketClosedError:
                # Market is intentionally closed — there is nothing to retry.
                # Bubble straight up so the caller skips the cycle and sleeps
                # until reopen instead of burning the retry budget.
                raise
            except (CandleFetchFailed, RuntimeError, NoCandleData) as exc:
                last_error = exc
                if attempt < max_retries:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(
                        "Broker fetch failed, retrying",
                        extra={
                            "symbol": symbol,
                            "interval": interval,
                            "attempt": attempt + 1,
                            "max_attempts": max_retries + 1,
                            "delay": delay,
                            "error": str(exc),
                        },
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.error(
                    "Broker fetch failed after retries",
                    extra={
                        "symbol": symbol,
                        "interval": interval,
                        "attempts": max_retries + 1,
                        "error": str(exc),
                    },
                )
                raise
            except Exception as exc:  # pragma: no cover - unexpected
                logger.error(
                    "Unexpected broker fetch error",
                    extra={
                        "symbol": symbol,
                        "interval": interval,
                        "attempt": attempt + 1,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    },
                )
                raise

        if last_error:
            raise last_error
        raise CandleFetchFailed("Unknown broker fetch error", error="retry_exhausted")
    
    async def initialize_cache(
        self, 
        symbol: str, 
        fetch_5m: bool = True, 
        fetch_15m: bool = True
    ) -> None:
        """Pre-populate cache with full buffer of candles.
        
        Useful for initializing trading systems that need historical context.
        """
        logger.info(f"Initializing cache for {symbol}")
        
        try:
            n5 = self._cache_size_5m if fetch_5m else 1
            n15 = self._cache_size_15m if fetch_15m else 1
            
            await self.fetch(symbol, n5=n5, n15=n15, force_broker=True)
            
            logger.info(f"Cache initialized successfully for {symbol}")
        except Exception as e:
            logger.error(f"Cache initialization failed for {symbol}: {e}")
            raise

    # ========================================================================
    # DATA NORMALIZATION
    # ========================================================================

    @staticmethod
    def _normalize_candles(data: Any) -> List[Candle]:
        """Normalize various candle data formats to Candle objects.
        
        Handles:
        - Existing Candle objects (zero-copy fast path when tz-aware)
        - Dictionaries with OHLCV keys
        - Tuples/lists [time, o, h, l, c, v]
        
        All timestamps are converted to UTC-aware pd.Timestamp.
        """
        if not data:
            return []
        
        result: List[Candle] = []
        
        for idx, item in enumerate(data):
            try:
                # Fast path: already a Candle with tz-aware timestamp
                if isinstance(item, Candle):
                    ts = item.time
                    if hasattr(ts, 'tzinfo') and ts.tzinfo is not None:
                        result.append(item)  # Zero-copy — already correct
                        continue
                    # Needs tz fix — fall through to slow path
                    time_val, o, h, l, c, v = (
                        ts, item.open, item.high, 
                        item.low, item.close, item.volume
                    )
                elif isinstance(item, dict):
                    time_val = item["time"]
                    o, h, l, c = item["open"], item["high"], item["low"], item["close"]
                    v = item.get("volume")
                else:  # tuple/list
                    time_val, o, h, l, c = item[0], item[1], item[2], item[3], item[4]
                    v = item[5] if len(item) > 5 else None
                
                # Ensure UTC timezone
                ts = pd.Timestamp(time_val)
                if ts.tzinfo is None:
                    ts = ts.tz_localize("UTC")
                
                result.append(
                    Candle(
                        time=ts,
                        open=float(o),
                        high=float(h),
                        low=float(l),
                        close=float(c),
                        volume=float(v) if v is not None else None,
                    )
                )
            except Exception as e:
                logger.error(f"Failed to normalize candle at index {idx}: {e}")
                raise
        
        return result

    # ========================================================================
    # PRIMARY FETCH API
    # ========================================================================

    async def fetch(
        self,
        symbol: str,
        n5: int | None = None,
        n15: int | None = None,
        force_broker: bool = False,
    ) -> Tuple[List[Candle], List[Candle]]:
        """Fetch 5m and 15m candles with cache awareness and targeted broker calls."""

        n5 = n5 if n5 is not None else self.five_count
        n15 = n15 if n15 is not None else self.fifteen_count

        tasks: Dict[str, asyncio.Task[List[Candle]]] = {}
        results: Dict[str, List[Candle]] = {}

        if n5 > 0:
            tasks["5m"] = asyncio.create_task(
                self.get_candles(symbol, "5m", n5, force_broker=force_broker)
            )
        else:
            results["5m"] = []

        if n15 > 0:
            tasks["15m"] = asyncio.create_task(
                self.get_candles(symbol, "15m", n15, force_broker=force_broker)
            )
        else:
            results["15m"] = []

        if tasks:
            gathered = await asyncio.gather(*tasks.values(), return_exceptions=True)
            for key, value in zip(tasks.keys(), gathered):
                if isinstance(value, Exception):
                    raise value
                results[key] = value

        c5, c15 = results.get("5m", []), results.get("15m", [])
        if c5 and c15:
            from .candle_validation import validate_timeframe_consistency

            valid, reason = validate_timeframe_consistency(c5, c15)
            if not valid:
                # Neither side is authoritative when their aggregates disagree.
                # Evict the contaminated windows before a bounded caller retry.
                self.clear_cache(symbol)
                raise CandleFetchFailed(reason, error="timeframe_mismatch")
        return c5, c15
    

    # ========================================================================
    # SMART INCREMENTAL FETCH
    # ========================================================================

    async def fetch_incremental(
        self,
        symbol: str,
        interval: str,
        target_count: int
    ) -> List[Candle]:
        """Fetch only the missing closed candles for ``interval`` and update cache."""

        if target_count <= 0:
            return []

        cache_key = self._get_cache_key(symbol, interval)
        cached = (
            self._cache_5m.get(cache_key, []) if interval == "5m"
            else self._cache_15m.get(cache_key, [])
        )

        # A cache that lacks the requested history needs a full broker window.
        if len(cached) < target_count:
            logger.debug(
                "Cache miss - fetching initial window",
                extra={"symbol": symbol, "interval": interval, "target_count": target_count}
            )
            fresh = await self._fetch_interval_from_broker(symbol, interval, target_count)
            self._update_cache(symbol, interval, fresh, requested_count=target_count)
            return fresh[-target_count:]

        # If cache is fresh enough, reuse it directly
        if not self._needs_fresh_data(symbol, interval):
            logger.debug(
                "Cache fresh - returning cached window",
                extra={"symbol": symbol, "interval": interval, "cached": len(cached)}
            )
            return cached[-target_count:] if len(cached) >= target_count else cached

        # A full cache can still be several bars behind (e.g. hourly agents).
        interval_minutes = 5 if interval == "5m" else 15
        expected = self._get_expected_last_closed(datetime.now(timezone.utc), interval_minutes)
        elapsed_bars = (expected - cached[-1].time) // timedelta(minutes=interval_minutes)
        fetch_count = min(target_count, elapsed_bars) if elapsed_bars > 0 else target_count

        logger.debug(
            "Cache stale - fetching incremental candles",
            extra={
                "symbol": symbol,
                "interval": interval,
                "fetch_count": fetch_count,
                "cached_count": len(cached),
                "target_count": target_count,
            }
        )

        fresh = await self._fetch_interval_from_broker(symbol, interval, fetch_count)
        if not fresh:
            logger.debug(
                "Broker returned no incremental candles; using cache",
                extra={"symbol": symbol, "interval": interval}
            )
            return cached[-target_count:] if len(cached) >= target_count else cached

        if fetch_count == target_count:
            self._update_cache(symbol, interval, fresh, requested_count=target_count)
            return fresh[-target_count:]

        latest_cached_time = cached[-1].time
        latest_fresh_time = fresh[-1].time

        if latest_fresh_time <= latest_cached_time:
            logger.debug(
                "No newer closed candle available yet",
                extra={"symbol": symbol, "interval": interval, "latest_cached": latest_cached_time.isoformat()}
            )
            return cached[-target_count:] if len(cached) >= target_count else cached

        merged = self._merge_into_cache(symbol, interval, fresh, requested_count=target_count)

        window = merged[-target_count:]
        if len(window) < target_count:
            logger.debug(
                "Merged window short on candles - fetching full target",
                extra={"symbol": symbol, "interval": interval, "have": len(window), "target": target_count}
            )
            full = await self._fetch_interval_from_broker(symbol, interval, target_count)
            self._update_cache(symbol, interval, full, requested_count=target_count)
            return full[-target_count:]

        return window

    # ========================================================================
    # CONVENIENCE METHODS
    # ========================================================================

    def _merge_into_cache(
        self, symbol: str, interval: str, fresh: List[Candle], *, requested_count: int = 0
    ) -> List[Candle]:
        """Merge *fresh* candles into the existing cache and return the updated cache list.

        Existing candles whose timestamps are also present in *fresh* are replaced
        by the fresh version (fresh data wins).  The merged result is stored back
        into the cache.
        """
        cache_key = self._get_cache_key(symbol, interval)
        existing = (
            self._cache_5m.get(cache_key, []) if interval == "5m"
            else self._cache_15m.get(cache_key, [])
        )
        if existing:
            fresh_times = {c.time for c in fresh}
            merged = [c for c in existing if c.time not in fresh_times] + fresh
        else:
            merged = fresh
        self._update_cache(symbol, interval, merged, requested_count=requested_count)
        return merged

    async def get_candles(
        self, 
        symbol: str, 
        interval: str, 
        limit: int, 
        force_broker: bool = False
    ) -> List[Candle]:
        """Fetch candles with smart incremental caching.
        
        Uses incremental fetch strategy to minimize broker API calls while
        ensuring data freshness. Falls back to full fetch if needed.
        
        Parameters
        ----------
        symbol : str
            Trading symbol.
        interval : str
            Candle interval ("5m" or "15m").
        limit : int
            Number of candles to retrieve.
        force_broker : bool
            Skip cache and fetch from broker.

        Returns
        -------
        List[Candle]
            Requested candles.

        Raises
        ------
        ValueError
            If interval is not "5m" or "15m".
        """
        interval = interval.lower()
        
        if interval not in ["5m", "15m"]:
            logger.error(f"Unsupported interval: {interval}")
            raise ValueError(f"Unsupported interval: {interval}")
        
        # Force broker bypasses cache entirely
        if force_broker:
            logger.debug(
                "Force broker fetch",
                extra={"symbol": symbol, "interval": interval, "limit": limit}
            )
            fresh = await self._fetch_interval_from_broker(symbol, interval, limit)
            self._merge_into_cache(symbol, interval, fresh, requested_count=limit)
            return fresh[-limit:] if limit > 0 else fresh
        
        # Use smart incremental fetch
        try:
            candles = await self.fetch_incremental(symbol, interval, limit)
            logger.debug(
                "Returning candles from cache/incremental",
                extra={
                    "symbol": symbol,
                    "interval": interval,
                    "count": len(candles),
                    "latest": candles[-1].time.isoformat() if candles else None,
                },
            )
            return candles
        except MarketClosedError:
            # Market halted — there is nothing to force-fetch.  Bubble straight
            # up so the caller skips the cycle instead of hammering a frozen
            # feed with another broker round-trip.
            raise
        except Exception as e:
            logger.error(
                "Incremental fetch failed, forcing broker fetch",
                extra={"symbol": symbol, "interval": interval, "error": str(e)}
            )
            fresh = await self._fetch_interval_from_broker(symbol, interval, limit)
            self._merge_into_cache(symbol, interval, fresh, requested_count=limit)
            return fresh[-limit:] if limit > 0 else fresh

    async def get_latest_closed_candle(
        self,
        symbol: str,
        interval: str,
        *,
        force_broker: bool = False,
    ) -> Candle | None:
        """Return the most recent closed candle with minimal broker calls."""

        interval = interval.lower()
        if interval not in ["5m", "15m"]:
            logger.error(f"Unsupported interval: {interval}")
            raise ValueError(f"Unsupported interval: {interval}")

        if force_broker:
            fresh = await self._fetch_interval_from_broker(symbol, interval, 1)
            if not fresh:
                return None
            self._merge_into_cache(symbol, interval, fresh)
            return fresh[-1]

        candles = await self.fetch_incremental(symbol, interval, 1)
        return candles[-1] if candles else None

    # ========================================================================
    # SERIALIZATION UTILITIES
    # ========================================================================

    @staticmethod
    def to_compact_arrays(candles: List[Candle]) -> List[list]:
        """Convert Candle objects to compact array format.
        
        Format: [timestamp_iso, open, high, low, close, volume]
        
        Useful for JSON serialization and network transmission.
        """
        result: List[list] = []
        
        for candle in candles or []:
            ts_str = (
                candle.time.isoformat() 
                if hasattr(candle.time, 'isoformat') 
                else str(candle.time)
            )
            volume = float(candle.volume) if candle.volume is not None else 0.0
            
            result.append([
                ts_str,
                float(candle.open),
                float(candle.high),
                float(candle.low),
                float(candle.close),
                volume,
            ])
        
        return result
    
    def get_compact_from_cache(
        self, 
        symbol: str, 
        interval: str, 
        limit: int
    ) -> List[list] | None:
        """Get compact array format from cache.
        
        Returns None if cache is unavailable or stale.
        """
        cached = self._retrieve_from_cache(symbol, interval, limit)
        return self.to_compact_arrays(cached) if cached else None


# ============================================================================
# EXPORTS
# ============================================================================

__all__ = ["CandleService", "NoCandleData", "CandleFetchFailed"]
