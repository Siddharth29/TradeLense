"""Historical candle service for backtesting using CSV files."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any
from functools import lru_cache

import pandas as pd

from .candles import Candle, validate_candle_integrity
from .logger import get_logger
from .config import DATA_DIR
from .candle_type_validation import (
    validate_candle_list_types,
    normalize_candle_list_types,
    normalize_candle_types
)

logger = get_logger(__name__)

# Map asset names to actual CSV filenames on disk.
# This avoids hardcoding naming conventions and lets HistoricalCandleService
# work for any asset whose data files exist in DATA_DIR.
_CSV_FILENAME_MAP: Dict[str, Dict[str, str]] = {
    "BTCUSD": {
        "5m": "btcusd_5m_2years.csv",
        "15m": "btcusd_15m_2years.csv",
    },
    "GBPUSD": {
        "5m": "gbpusd_5m_2years.csv",
        "15m": "gbpusd_15m_2years.csv",
    },
    "EURUSD": {
        "5m": "eurusd_5m_2years.csv",
        "15m": "eurusd_15m_2years.csv",
    },
}


def _resolve_csv_filename(asset: str, interval: str) -> str:
    """Resolve the actual CSV filename for an asset and interval.

    Falls back to ``{asset.lower()}_{interval}_2years.csv`` when no explicit
    mapping exists.
    """
    asset_upper = asset.upper()
    if asset_upper in _CSV_FILENAME_MAP:
        mapping = _CSV_FILENAME_MAP[asset_upper]
        if interval in mapping:
            return mapping[interval]
    # Fallback convention
    return f"{asset.lower()}_{interval}_2years.csv"


@lru_cache(maxsize=4)
def _load_csv_dataframe(filename: str) -> pd.DataFrame:
    """Load CSV file and cache it in memory.
    
    Parameters
    ----------
    filename : str
        CSV filename (e.g., 'btcusd_5m_2years.csv')
    
    Returns
    -------
    pd.DataFrame
        DataFrame with time as index and columns: open, high, low, close, volume
    """
    filepath = DATA_DIR / filename
    if not filepath.exists():
        raise FileNotFoundError(
            f"CSV file not found: {filepath}. "
            f"Please run scripts/download_candle_data.py to download candle data."
        )
    
    logger.info(f"Loading CSV file: {filepath}")
    df = pd.read_csv(filepath, index_col=0, parse_dates=True)
    
    # Ensure index is timezone-aware (UTC)
    if df.index.tz is None:
        df.index = df.index.tz_localize('UTC')
    
    # Sort by time
    df = df.sort_index()
    
    logger.info(f"Loaded {len(df)} candles from {filename}")
    return df


class HistoricalCandleService:
    """Service for fetching historical candles from CSV files for backtesting with caching.
    
    This service loads historical candle data from CSV files (btcusd_5m_2years.csv
    and btcusd_15m_2years.csv) for use in
    backtesting scenarios. It includes
    caching to optimize performance when the same data is requested multiple times.
    """
    
    def __init__(self, enable_cache: bool = True, cache_size: int = 128):
        """Initialize the historical candle service.
        
        Parameters
        ----------
        enable_cache : bool
            Whether to enable caching (default: True)
        cache_size : int
            Maximum number of cached queries (default: 128)
        """
        self.enable_cache = enable_cache
        self.cache_size = cache_size
        self._cache: Dict[str, List[Dict[str, Any]]] = {}
        self._cache_hits = 0
        self._cache_misses = 0
    
    def _generate_cache_key(
        self,
        interval: str,
        asset: str,
        start_date: datetime,
        end_date: datetime,
        limit: int
    ) -> str:
        """Generate a cache key for candle query."""
        import json
        import hashlib
        key_data = {
            "interval": interval,
            "asset": asset,
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
            "limit": limit
        }
        key_str = json.dumps(key_data, sort_keys=True)
        return hashlib.sha256(key_str.encode()).hexdigest()[:16]
    
    def _get_from_cache(self, cache_key: str) -> List[Dict[str, Any]] | None:
        """Get candles from cache."""
        if not self.enable_cache:
            return None
        
        if cache_key in self._cache:
            self._cache_hits += 1
            logger.debug(f"Cache hit (hits: {self._cache_hits}, misses: {self._cache_misses})")
            return self._cache[cache_key]
        
        self._cache_misses += 1
        return None
    
    def _add_to_cache(self, cache_key: str, candles: List[Dict[str, Any]]) -> None:
        """Add candles to cache with LRU eviction."""
        if not self.enable_cache:
            return
        
        # Simple LRU: remove oldest if cache is full
        if len(self._cache) >= self.cache_size:
            first_key = next(iter(self._cache))
            del self._cache[first_key]
        
        self._cache[cache_key] = candles
    
    def clear_cache(self) -> None:
        """Clear the candle cache."""
        self._cache.clear()
        self._cache_hits = 0
        self._cache_misses = 0
        logger.info("Candle cache cleared")
    
    def get_cache_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        total_requests = self._cache_hits + self._cache_misses
        hit_rate = (self._cache_hits / total_requests * 100) if total_requests > 0 else 0.0
        
        return {
            "enabled": self.enable_cache,
            "size": len(self._cache),
            "max_size": self.cache_size,
            "hits": self._cache_hits,
            "misses": self._cache_misses,
            "hit_rate": round(hit_rate, 2)
        }
    
    async def fetch_historical_5m(
        self,
        asset: str,
        start_date: datetime,
        end_date: datetime,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Fetch 5-minute candles from CSV file with caching.
        
        Parameters
        ----------
        asset : str
            Asset symbol (e.g., 'BTCUSD') - currently only BTCUSD is supported
        start_date : datetime
            Start date for fetching candles (timezone-aware)
        end_date : datetime
            End date for fetching candles (timezone-aware)
        limit : int
            Maximum number of candles to fetch (default: 100)
        
        Returns
        -------
        List[Dict[str, Any]]
            List of candle dictionaries with keys: time, open, high, low, close, volume
        """
        # For CSV-based service, we load all data in date range
        # Limit is only applied at the end, so we allow large limits
        # (backtest runner uses limit=5000 to fetch all candles in range)
        actual_limit = limit if limit > 0 else None
        
        try:
            # Ensure timezone-aware
            if start_date.tzinfo is None:
                start_date = start_date.replace(tzinfo=timezone.utc)
            if end_date.tzinfo is None:
                end_date = end_date.replace(tzinfo=timezone.utc)
            
            # Check cache first
            cache_key = self._generate_cache_key("5m", asset, start_date, end_date, actual_limit or 0)
            cached_candles = self._get_from_cache(cache_key)
            if cached_candles is not None:
                logger.info(
                    f"[CSV-BACKTEST] Using cached 5m candles for {asset} from CSV file "
                    f"(cache hit: {len(cached_candles)} candles)"
                )
                return cached_candles
            
            # Load CSV file (cached by lru_cache)
            filename = _resolve_csv_filename(asset, "5m")
            filepath = DATA_DIR / filename
            logger.info(
                f"[CSV-BACKTEST] Loading 5m candles from CSV file for {asset}: {filepath} "
                f"(date range: {start_date} to {end_date})"
            )
            try:
                df = _load_csv_dataframe(filename)
            except FileNotFoundError as e:
                logger.error(
                    f"[CSV-BACKTEST] CSV file not found for {asset}: Expected file '{filename}' in {DATA_DIR}. "
                    f"Available mappings: {list(_CSV_FILENAME_MAP.keys())}"
                )
                raise
            
            # Filter by date range
            mask = (df.index >= start_date) & (df.index <= end_date)
            filtered_df = df[mask]
            
            # Limit results (if limit is None or 0, return all)
            if actual_limit is not None and actual_limit > 0:
                filtered_df = filtered_df.tail(actual_limit)
            
            # Convert to list of dictionaries
            candles = []
            for idx, row in filtered_df.iterrows():
                candle = {
                    "time": idx,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row.get("volume", 0.0))
                }
                candles.append(candle)
            
            # Normalize types to ensure consistency with live trading format
            candles = normalize_candle_list_types(candles)
            
            # Validate types (log warning if invalid but continue)
            is_valid, error_msg = validate_candle_list_types(candles)
            if not is_valid:
                logger.warning(f"Type validation warning for 5m candles: {error_msg}")
            
            # Add to cache
            self._add_to_cache(cache_key, candles)
            
            logger.info(
                f"[CSV-BACKTEST] Successfully fetched {len(candles)} 5m candles for {asset} from CSV file "
                f"({filename}): date range {start_date} to {end_date} "
                f"(limit={actual_limit or 'all'}, returned={len(candles)})"
            )
            
            return candles
            
        except Exception as e:
            logger.error(f"Failed to fetch historical 5m candles: {e}")
            raise ValueError(f"Failed to fetch historical 5m candles: {e}") from e
    
    async def fetch_historical_15m(
        self,
        asset: str,
        start_date: datetime,
        end_date: datetime,
        limit: int = 70
    ) -> List[Dict[str, Any]]:
        """Fetch 15-minute candles from CSV file with caching.
        
        Parameters
        ----------
        asset : str
            Asset symbol (e.g., 'BTCUSD') - currently only BTCUSD is supported
        start_date : datetime
            Start date for fetching candles (timezone-aware)
        end_date : datetime
            End date for fetching candles (timezone-aware)
        limit : int
            Maximum number of candles to fetch (default: 70)
        
        Returns
        -------
        List[Dict[str, Any]]
            List of candle dictionaries with keys: time, open, high, low, close, volume
        """
        # For CSV-based service, we load all data in date range
        # Limit is only applied at the end, so we allow large limits
        # (backtest runner uses limit=2000 to fetch all candles in range)
        actual_limit = limit if limit > 0 else None
        
        try:
            # Ensure timezone-aware
            if start_date.tzinfo is None:
                start_date = start_date.replace(tzinfo=timezone.utc)
            if end_date.tzinfo is None:
                end_date = end_date.replace(tzinfo=timezone.utc)
            
            # Check cache first
            cache_key = self._generate_cache_key("15m", asset, start_date, end_date, actual_limit or 0)
            cached_candles = self._get_from_cache(cache_key)
            if cached_candles is not None:
                logger.info(
                    f"[CSV-BACKTEST] Using cached 15m candles for {asset} from CSV file "
                    f"(cache hit: {len(cached_candles)} candles)"
                )
                return cached_candles
            
            # Load CSV file (cached by lru_cache)
            filename = _resolve_csv_filename(asset, "15m")
            filepath = DATA_DIR / filename
            logger.info(
                f"[CSV-BACKTEST] Loading 15m candles from CSV file for {asset}: {filepath} "
                f"(date range: {start_date} to {end_date})"
            )
            try:
                df = _load_csv_dataframe(filename)
            except FileNotFoundError as e:
                logger.error(
                    f"[CSV-BACKTEST] CSV file not found for {asset}: Expected file '{filename}' in {DATA_DIR}. "
                    f"Available mappings: {list(_CSV_FILENAME_MAP.keys())}"
                )
                raise
            
            # Filter by date range
            mask = (df.index >= start_date) & (df.index <= end_date)
            filtered_df = df[mask]
            
            # Limit results (if limit is None or 0, return all)
            if actual_limit is not None and actual_limit > 0:
                filtered_df = filtered_df.tail(actual_limit)
            
            # Convert to list of dictionaries
            candles = []
            for idx, row in filtered_df.iterrows():
                candle = {
                    "time": idx,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row.get("volume", 0.0))
                }
                candles.append(candle)
            
            # Normalize types to ensure consistency with live trading format
            candles = normalize_candle_list_types(candles)
            
            # Validate types (log warning if invalid but continue)
            is_valid, error_msg = validate_candle_list_types(candles)
            if not is_valid:
                logger.warning(f"Type validation warning for 15m candles: {error_msg}")
            
            # Add to cache
            self._add_to_cache(cache_key, candles)
            
            logger.info(
                f"[CSV-BACKTEST] Successfully fetched {len(candles)} 15m candles for {asset} from CSV file "
                f"({filename}): date range {start_date} to {end_date} "
                f"(limit={actual_limit or 'all'}, returned={len(candles)})"
            )
            
            return candles
            
        except Exception as e:
            logger.error(f"Failed to fetch historical 15m candles: {e}")
            raise ValueError(f"Failed to fetch historical 15m candles: {e}") from e
    
    def validate_candle_data(
        self,
        candles_5m: List[Dict[str, Any]],
        candles_15m: List[Dict[str, Any]],
        expected_count_5m: int | None = None,
        expected_count_15m: int | None = None
    ) -> tuple[bool, str]:
        """Validate completeness and gaps in candle data.
        
        Parameters
        ----------
        candles_5m : List[Dict[str, Any]]
            List of 5-minute candle dictionaries
        candles_15m : List[Dict[str, Any]]
            List of 15-minute candle dictionaries
        expected_count_5m : int, optional
            Expected number of 5m candles
        expected_count_15m : int, optional
            Expected number of 15m candles
        
        Returns
        -------
        tuple[bool, str]
            (is_valid, message) - True if valid, False otherwise with error message
        """
        # Convert to Candle objects for validation
        try:
            candle_objs_5m = []
            for c in candles_5m:
                time_val = c.get("time")
                if isinstance(time_val, str):
                    time_val = pd.Timestamp(time_val)
                elif not isinstance(time_val, pd.Timestamp):
                    time_val = pd.Timestamp(time_val)
                
                candle_objs_5m.append(Candle(
                    time=time_val,
                    open=c.get("open", 0.0),
                    high=c.get("high", 0.0),
                    low=c.get("low", 0.0),
                    close=c.get("close", 0.0),
                    volume=c.get("volume", 0.0)
                ))
            
            candle_objs_15m = []
            for c in candles_15m:
                time_val = c.get("time")
                if isinstance(time_val, str):
                    time_val = pd.Timestamp(time_val)
                elif not isinstance(time_val, pd.Timestamp):
                    time_val = pd.Timestamp(time_val)
                
                candle_objs_15m.append(Candle(
                    time=time_val,
                    open=c.get("open", 0.0),
                    high=c.get("high", 0.0),
                    low=c.get("low", 0.0),
                    close=c.get("close", 0.0),
                    volume=c.get("volume", 0.0)
                ))
            
            # Validate 5m candles
            is_valid_5m, msg_5m = validate_candle_integrity(
                candle_objs_5m,
                interval="5m",
                expected_count=expected_count_5m,
                check_recency=False,
                strict_mode=False
            )
            
            # Validate 15m candles
            is_valid_15m, msg_15m = validate_candle_integrity(
                candle_objs_15m,
                interval="15m",
                expected_count=expected_count_15m,
                check_recency=False,
                strict_mode=False
            )
            
            if not is_valid_5m:
                return False, f"5m candles validation failed: {msg_5m}"
            
            if not is_valid_15m:
                return False, f"15m candles validation failed: {msg_15m}"
            
            return True, "Candle data validated successfully"
            
        except Exception as e:
            logger.error(f"Candle validation error: {e}")
            return False, f"Validation error: {str(e)}"
