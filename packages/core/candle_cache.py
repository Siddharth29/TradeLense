"""
Candle caching system for efficient candle management during monitoring.

This module provides:
1. Candle cache with time-based validation
2. Incremental updates (add new, remove old)
3. Synchronization validation (no gaps, exact time matching)
4. Fallback mechanism for cache invalidation
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Any, Deque, Dict, List, Optional, Tuple
import pandas as pd
import logging

from .logger import get_logger
from .candles import Candle
from .config import CANDLE_CACHE_MAX_CANDLES

logger = get_logger(__name__)


class CandleCache:
    """
    Efficient candle cache system that:
    1. Stores historical candles from sniper call
    2. Provides incremental updates during monitoring
    3. Validates synchronization and time alignment
    4. Handles fallback when cache is invalid
    """
    
    def __init__(self, asset: str, interval: str = "5m", max_candles: int = CANDLE_CACHE_MAX_CANDLES):
        self.asset = asset
        self.interval = interval
        self.max_candles = max_candles
        self.interval_minutes = 5 if interval == "5m" else 15
        
        # Cache state — deque gives O(1) append and popleft vs O(n) pop(0) on a list
        self._candles: Deque[Candle] = deque(maxlen=max_candles)
        self._dataframe: Optional[pd.DataFrame] = None
        self._last_update: Optional[datetime] = None
        self._cache_valid: bool = False
        self._sync_validated: bool = False
        
        # Statistics
        self._stats = {
            "cache_hits": 0,
            "cache_misses": 0,
            "incremental_updates": 0,
            "full_refetches": 0,
            "sync_errors": 0
        }
        
        logger.info(f"CandleCache initialized for {asset} {interval} (max: {max_candles})")
    
    def initialize_from_sniper(self, candles: List[Any]) -> bool:
        """
        Initialize cache with candles from sniper call.

        Args:
            candles: List of candles from sniper call (should be >= 10).  The
            candles may be :class:`Candle` objects, dictionaries or tuples.  In
            all cases the time field is normalised to a ``pandas.Timestamp`` so
            downstream code receives consistent types.

        Returns:
            bool: True if initialization successful, False otherwise
        """
        try:
            def _normalise(data: List[Any]) -> List[Candle]:
                res: List[Candle] = []
                for c in data or []:
                    if isinstance(c, Candle):
                        ts = pd.Timestamp(c.time)
                        if ts.tzinfo is None:
                            ts = ts.tz_localize("UTC")
                        res.append(
                            Candle(
                                time=ts,
                                open=float(c.open),
                                high=float(c.high),
                                low=float(c.low),
                                close=float(c.close),
                                volume=float(c.volume) if c.volume is not None else None,
                            )
                        )
                    elif isinstance(c, dict):
                        ts = pd.Timestamp(c["time"])
                        if ts.tzinfo is None:
                            ts = ts.tz_localize("UTC")
                        res.append(
                            Candle(
                                time=ts,
                                open=float(c["open"]),
                                high=float(c["high"]),
                                low=float(c["low"]),
                                close=float(c["close"]),
                                volume=float(c.get("volume")) if c.get("volume") is not None else None,
                            )
                        )
                    else:  # tuple/list
                        ts = pd.Timestamp(c[0])
                        if ts.tzinfo is None:
                            ts = ts.tz_localize("UTC")
                        vol = float(c[5]) if len(c) > 5 and c[5] is not None else None
                        res.append(
                            Candle(
                                time=ts,
                                open=float(c[1]),
                                high=float(c[2]),
                                low=float(c[3]),
                                close=float(c[4]),
                                volume=vol,
                            )
                        )
                return res

            norm = _normalise(candles)
            if not norm or len(norm) < 10:
                logger.error(f"Insufficient candles for {self.asset}: {len(norm) if norm else 0}")
                return False

            # Validate candle synchronization
            if not self._validate_candle_sync(norm):
                logger.error(f"Candle sync validation failed for {self.asset}")
                return False

            # Store candles — deque(maxlen=max_candles) already enforces the cap
            self._candles = deque(norm[-self.max_candles:], maxlen=self.max_candles)
            self._dataframe = self._convert_to_dataframe(self._candles)
            self._last_update = datetime.now(timezone.utc)
            self._cache_valid = True
            self._sync_validated = True

            logger.info(
                f"Cache initialized for {self.asset}: {len(self._candles)} candles, "
                f"latest: {self._candles[-1].time if self._candles else None}"
            )

            return True

        except Exception as e:
            logger.error(f"Failed to initialize cache for {self.asset}: {e}")
            self._cache_valid = False
            return False
    
    def get_historical_candles(self) -> Optional[pd.DataFrame]:
        """
        Get historical candles from cache.
        
        Returns:
            pd.DataFrame: Historical candles or None if cache invalid
        """
        if not self._cache_valid or self._dataframe is None:
            self._stats["cache_misses"] += 1
            return None
        
        self._stats["cache_hits"] += 1
        return self._dataframe.copy()
    
    def get_current_candle(self) -> Optional[Dict[str, Any]]:
        """
        Get the latest candle as current candle.
        
        Returns:
            Dict: Current candle data or None if cache invalid
        """
        if not self._cache_valid or not self._candles:
            return None
        
        latest_candle = self._candles[-1]
        
        # Ensure proper timestamp serialization
        time_value = latest_candle.time
        if hasattr(time_value, 'isoformat'):
            time_str = time_value.isoformat()
        elif hasattr(time_value, 'to_pydatetime'):
            time_str = time_value.to_pydatetime().isoformat()
        else:
            time_str = str(time_value)
        
        return {
            "time": time_str,
            "open": float(latest_candle.open),
            "high": float(latest_candle.high),
            "low": float(latest_candle.low),
            "close": float(latest_candle.close),
            "volume": float(latest_candle.volume) if latest_candle.volume else 0,
        }
    
    async def update_with_new_candle(self, new_candle: Candle, candle_service: Any) -> bool:
        """
        Rolling window update: Add new candle and remove oldest to maintain fixed window size.

        Args:
            new_candle: New candle to add
            candle_service: Candle service for fallback if needed

        Returns:
            bool: True if update successful, False if fallback needed
        """
        try:
            if not self._cache_valid:
                logger.warning(f"Cache invalid for {self.asset}, triggering fallback")
                return False

            # Same-timestamp candle = no new bar yet → treat as success (no-op)
            if self._candles and new_candle.time == self._candles[-1].time:
                logger.debug(f"No new candle yet for {self.asset} (same timestamp: {new_candle.time})")
                return True

            # Validate new candle time alignment
            if not self._validate_new_candle_time(new_candle):
                logger.warning(f"New candle time validation failed for {self.asset}, triggering fallback")
                return False
            
            # Check for potential gaps and fill if needed
            if self._candles:
                latest_time = self._candles[-1].time
                new_time = new_candle.time
                expected_next_time = latest_time + timedelta(minutes=self.interval_minutes)
                
                # If there's more than one interval gap, try to fill missing candles
                time_gap_minutes = (new_time - latest_time).total_seconds() / 60
                if time_gap_minutes > self.interval_minutes * 1.5:  # More than 1.5 intervals
                    logger.info(
                        f"Detected gap in candles for {self.asset}: "
                        f"last={latest_time}, new={new_time}, gap={time_gap_minutes:.1f}min"
                    )
                    # For now, still add the candle but log the gap
                    # Future enhancement: fetch missing candles
            
            # Rolling window: deque(maxlen=max_candles) automatically drops the
            # oldest element when full — O(1) operation, no manual pop needed.
            removed_candle: Candle | None = None
            if len(self._candles) == self.max_candles:
                removed_candle = self._candles[0]  # peek before it gets evicted
                logger.debug(
                    "Rolling window: Removing oldest candle",
                    extra={
                        "asset": self.asset,
                        "interval": self.interval,
                        "removed": removed_candle.time.isoformat(),
                        "added": new_candle.time.isoformat(),
                    }
                )
            self._candles.append(new_candle)  # deque maxlen evicts oldest automatically
            
            # Update DataFrame incrementally
            self._update_dataframe_incremental(new_candle, removed_candle)
            self._last_update = datetime.now(timezone.utc)
            
            self._stats["incremental_updates"] += 1
            
            logger.debug(
                f"Rolling window update for {self.asset}: {len(self._candles)} candles, "
                f"window: {self._candles[0].time} to {self._candles[-1].time}"
            )
            
            return True
            
        except Exception as e:
            logger.error(f"Failed rolling window update for {self.asset}: {e}")
            return False
    
    async def fallback_refetch(self, candle_service: Any, broker_name: str, agent_id: str) -> bool:
        """
        Fallback to full refetch when cache is invalid.
        
        Args:
            candle_service: Candle service for fetching
            broker_name: Broker name for logging
            agent_id: Agent ID for logging
            
        Returns:
            bool: True if refetch successful, False otherwise
        """
        try:
            logger.warning(f"Fallback refetch for {self.asset}")
            
            # Fetch fresh candles - ensure we get enough for rolling window
            candles = await candle_service.get_candles(
                self.asset, self.interval, max(self.max_candles, 20)  # Minimum 20 candles for safety
            )
            
            if not candles or len(candles) < 10:
                logger.error(f"Fallback refetch failed for {self.asset}: insufficient candles")
                return False
            
            # Reinitialize cache
            success = self.initialize_from_sniper(candles)
            if success:
                self._stats["full_refetches"] += 1
                logger.info(f"Fallback refetch successful for {self.asset}")
            
            return success
            
        except Exception as e:
            logger.error(f"Fallback refetch failed for {self.asset}: {e}")
            return False
    
    def is_cache_valid(self) -> bool:
        """Check if cache is valid and synchronized."""
        return self._cache_valid and self._sync_validated
    
    def is_fresh(self, max_age_minutes: int = 10) -> bool:
        """
        Check if cache is fresh enough.
        
        Args:
            max_age_minutes: Maximum age in minutes
            
        Returns:
            bool: True if cache is fresh, False otherwise
        """
        if not self._last_update:
            return False
        
        age = datetime.now(timezone.utc) - self._last_update
        return age.total_seconds() / 60 <= max_age_minutes
    
    def needs_update(self) -> bool:
        """
        Check if cache needs update based on current time and latest candle time.
        
        Returns:
            bool: True if update needed, False otherwise
        """
        if not self._cache_valid or not self._candles:
            return True
        
        # Get latest candle time
        latest_candle_time = self._candles[-1].time
        current_time = datetime.now(timezone.utc)
        
        # Calculate expected next candle time
        expected_next_candle = latest_candle_time + timedelta(minutes=self.interval_minutes)
        
        # If current time is past expected next candle time (with some buffer), we need update
        time_buffer = timedelta(minutes=1)  # 1 minute buffer
        return current_time >= (expected_next_candle + time_buffer)
    
    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        # Ensure proper timestamp serialization
        last_update_str = None
        if self._last_update:
            if hasattr(self._last_update, 'isoformat'):
                last_update_str = self._last_update.isoformat()
            else:
                last_update_str = str(self._last_update)
        
        window_range = "empty"
        if self._candles:
            start_time = self._candles[0].time
            end_time = self._candles[-1].time
            
            # Ensure proper timestamp serialization for window range
            start_str = start_time.isoformat() if hasattr(start_time, 'isoformat') else str(start_time)
            end_str = end_time.isoformat() if hasattr(end_time, 'isoformat') else str(end_time)
            window_range = f"{start_str} to {end_str}"
        
        return {
            **self._stats,
            "cache_valid": self._cache_valid,
            "sync_validated": self._sync_validated,
            "candle_count": len(self._candles),
            "last_update": last_update_str,
            "is_fresh": self.is_fresh(),
            "window_range": window_range
        }
    
    async def fetch_latest_candle_only(self, candle_service: Any) -> Optional[Candle]:
        """
        Fetch only the latest candle from MT5 (for rolling window updates).
        
        Args:
            candle_service: Candle service for fetching
            
        Returns:
            Candle: Latest candle or None if fetch failed
        """
        try:
            latest_candle = await candle_service.get_latest_closed_candle(
                self.asset, self.interval
            )
            
            if not latest_candle:
                logger.warning(f"No latest candle fetched for {self.asset}")
                return None
            
            logger.debug(
                f"Fetched latest candle for {self.asset}: {latest_candle.time} "
                f"O:{latest_candle.open} H:{latest_candle.high} L:{latest_candle.low} C:{latest_candle.close}"
            )
            
            return latest_candle
            
        except Exception as e:
            logger.error(f"Failed to fetch latest candle for {self.asset}: {e}")
            return None
    
    def _validate_candle_sync(self, candles) -> bool:
        """Validate candle synchronization, allowing expected market gaps.

        Unexpected gaps (non-market-hours, weekends, XAUUSD daily close) are
        permitted via ``is_expected_market_gap``; only truly anomalous gaps are
        treated as errors.

        Args:
            candles: Sequence of Candle objects to validate.

        Returns:
            bool: True if synchronized (or gaps are expected), False otherwise.
        """
        try:
            from .candles import is_expected_market_gap
        except ImportError:
            is_expected_market_gap = None  # type: ignore[assignment]

        try:
            candle_list = list(candles)
            if len(candle_list) < 2:
                return True

            for i in range(1, len(candle_list)):
                prev_time = candle_list[i - 1].time
                curr_time = candle_list[i].time

                if self._is_time_aligned(prev_time, curr_time):
                    continue

                # Allow gaps that the market-gap helper recognises
                if is_expected_market_gap is not None and is_expected_market_gap(
                    prev_time, curr_time, self.asset
                ):
                    logger.debug(
                        f"Expected market gap in {self.asset} candles: "
                        f"{prev_time} -> {curr_time}"
                    )
                    continue

                logger.warning(
                    f"Unexpected time gap in {self.asset} candles: "
                    f"{prev_time} -> {curr_time} (expected {self.interval_minutes}min)"
                )
                self._stats["sync_errors"] += 1
                return False

            logger.debug(f"Candle sync validation passed for {self.asset}: {len(candle_list)} candles")
            return True

        except Exception as e:
            logger.error(f"Candle sync validation error for {self.asset}: {e}")
            self._stats["sync_errors"] += 1
            return False
    
    def _validate_new_candle_time(self, new_candle: Candle) -> bool:
        """
        Validate that new candle time is properly aligned.
        
        Args:
            new_candle: New candle to validate
            
        Returns:
            bool: True if time is valid, False otherwise
        """
        if not self._candles:
            return True
        
        latest_time = self._candles[-1].time
        new_time = new_candle.time
        
        # Check if new candle is after the latest candle (allowing for multiple intervals)
        if new_time <= latest_time:
            logger.warning(
                f"New candle time is not after latest for {self.asset}: "
                f"latest: {latest_time}, new: {new_time}"
            )
            return False
        
        # Allow gaps up to 20 intervals (100min at 5m) to cover market close windows
        # (XAUUSD daily close is ~70min = 14 intervals at 5m)
        max_future_time = latest_time + timedelta(minutes=self.interval_minutes * 20)
        if new_time > max_future_time:
            logger.warning(
                f"New candle time too far in future for {self.asset}: "
                f"latest: {latest_time}, new: {new_time}, max_allowed: {max_future_time}"
            )
            return False
        
        logger.debug(f"New candle time validation passed for {self.asset}: {new_time}")
        return True
    
    def _is_time_aligned(self, prev_time: datetime, curr_time: datetime) -> bool:
        """
        Check if two candle times are properly aligned.
        
        Args:
            prev_time: Previous candle time
            curr_time: Current candle time
            
        Returns:
            bool: True if aligned, False otherwise
        """
        # Calculate expected time difference
        time_diff_minutes = (curr_time - prev_time).total_seconds() / 60
        
        # Allow for exact intervals or multiples (for gaps due to market closure, etc.)
        # Check if it's a multiple of the interval within tolerance
        if time_diff_minutes >= self.interval_minutes:
            # Allow for multiple intervals (e.g., market gaps)
            remainder = time_diff_minutes % self.interval_minutes
            return remainder <= 0.5 or remainder >= (self.interval_minutes - 0.5)  # 30 second tolerance
        
        return False
    
    def _convert_to_dataframe(self, candles) -> pd.DataFrame:
        """Convert candles (list or deque) to DataFrame with proper datetime handling."""
        """
        Convert candles to DataFrame with proper datetime handling.
        
        Args:
            candles: List of candles
            
        Returns:
            pd.DataFrame: Converted candles
        """
        if not candles:
            return pd.DataFrame()
        
        data = []
        for candle in candles:
            # Ensure time is properly handled for JSON serialization
            time_value = candle.time
            
            # Convert to pandas Timestamp for consistent handling
            if isinstance(time_value, pd.Timestamp):
                # Already a pandas Timestamp
                pass
            elif hasattr(time_value, 'to_pydatetime'):
                # Convert pandas Timestamp to datetime then back to Timestamp
                time_value = pd.Timestamp(time_value.to_pydatetime())
            elif hasattr(time_value, 'isoformat'):
                # Convert datetime-like object to pandas Timestamp
                time_value = pd.Timestamp(time_value)
            else:
                # Convert to pandas Timestamp as fallback
                time_value = pd.Timestamp(time_value)
            
            # Ensure timezone awareness
            if time_value.tzinfo is None:
                time_value = time_value.tz_localize("UTC")
            
            data.append({
                'time': time_value,
                'open': float(candle.open),
                'high': float(candle.high),
                'low': float(candle.low),
                'close': float(candle.close),
                'volume': float(candle.volume) if candle.volume else 0
            })
        
        df = pd.DataFrame(data)
        df.set_index('time', inplace=True)
        # Ensure index is properly formatted as datetime
        df.index = pd.to_datetime(df.index)
        return df

    def _update_dataframe_incremental(self, new_candle: Candle, removed: Candle | None) -> None:
        """Incrementally update the cached DataFrame for rolling window updates."""

        if self._dataframe is None or self._dataframe.empty:
            self._dataframe = self._convert_to_dataframe(self._candles)
            return

        if removed is not None:
            remove_ts = pd.Timestamp(removed.time)
            if remove_ts.tzinfo is None:
                remove_ts = remove_ts.tz_localize("UTC")
            self._dataframe = self._dataframe.drop(remove_ts, errors="ignore")

        incremental_df = self._convert_to_dataframe([new_candle])
        self._dataframe = pd.concat([self._dataframe, incremental_df])
        self._dataframe = self._dataframe[~self._dataframe.index.duplicated(keep="last")]  # Keep latest for duplicates
        if len(self._dataframe) > self.max_candles:
            self._dataframe = self._dataframe.iloc[-self.max_candles:]


class CandleCacheManager:
    """
    Manager for multiple candle caches across different assets.
    """
    
    def __init__(self):
        self._caches: Dict[str, CandleCache] = {}
        self._logger = get_logger(__name__)
    
    def get_cache(self, asset: str, interval: str = "5m") -> CandleCache:
        """
        Get or create cache for asset.
        
        Args:
            asset: Asset symbol
            interval: Candle interval
            
        Returns:
            CandleCache: Cache instance
        """
        key = f"{asset}_{interval}"
        if key not in self._caches:
            self._caches[key] = CandleCache(asset, interval)
            self._logger.info(f"Created new cache for {key}")
        
        return self._caches[key]
    
    def remove_cache(self, asset: str, interval: str = "5m") -> None:
        """
        Remove cache for asset.
        
        Args:
            asset: Asset symbol
            interval: Candle interval
        """
        key = f"{asset}_{interval}"
        if key in self._caches:
            del self._caches[key]
            self._logger.info(f"Removed cache for {key}")
    
    def get_all_stats(self) -> Dict[str, Dict[str, Any]]:
        """
        Get statistics for all caches.
        
        Returns:
            Dict: Statistics for all caches
        """
        return {
            key: cache.get_stats() 
            for key, cache in self._caches.items()
        }
    
    def cleanup_invalid_caches(self) -> int:
        """
        Clean up invalid caches.
        
        Returns:
            int: Number of caches cleaned up
        """
        invalid_keys = []
        for key, cache in self._caches.items():
            if not cache.is_cache_valid() or not cache.is_fresh():
                invalid_keys.append(key)
        
        for key in invalid_keys:
            del self._caches[key]
            self._logger.info(f"Cleaned up invalid cache: {key}")
        
        return len(invalid_keys)


# Global cache manager instance
_cache_manager = CandleCacheManager()


def get_cache_manager() -> CandleCacheManager:
    """Get the global cache manager instance."""
    return _cache_manager


def get_cache(asset: str, interval: str = "5m") -> CandleCache:
    """Get cache for asset from global manager."""
    return _cache_manager.get_cache(asset, interval)
