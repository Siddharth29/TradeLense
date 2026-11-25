"""MT5 Historical Data Fetcher Service.

This service fetches historical candle data from MT5 broker and stores it in MongoDB
for backtesting purposes.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any
import asyncio
import json
from pathlib import Path

import pandas as pd

from packages.adapters.mt5_adapter import MT5Adapter
from packages.db.repositories import candles as candle_repo
from packages.core.logger import get_logger

logger = get_logger(__name__)


def _load_mt5_config() -> Dict[str, str]:
    """Load MT5 configuration from config file."""
    config_path = Path(__file__).resolve().parents[3] / "config" / "mt5_config.json"
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
        # Return MT5 section from config
        return config.get("mt5", {
            "server": "Example-Demo",
            "login": "00000000",
            "password": "CHANGE_ME"
        })
    except Exception as e:
        logger.warning(f"Failed to load MT5 config from {config_path}: {e}")
        # Return default credentials as fallback
        return {
            "server": "Example-Demo",
            "login": "00000000",
            "password": "CHANGE_ME"
        }


class MT5HistoricalFetcher:
    """Service for fetching and storing historical candle data from MT5."""
    
    def __init__(self, mt5_adapter: MT5Adapter | None = None):
        """Initialize the fetcher with MT5 adapter.
        
        Parameters
        ----------
        mt5_adapter : MT5Adapter, optional
            Pre-configured MT5 adapter. If None, creates new adapter with config.
        """
        self.adapter = mt5_adapter or MT5Adapter()
        self._connected = False
    
    async def _ensure_connection(self) -> None:
        """Ensure MT5 is connected, connect if not."""
        if self._connected:
            return
        
        try:
            # Load credentials from config file
            mt5_config = _load_mt5_config()
            server = mt5_config.get("server", "Example-Demo")
            login = str(mt5_config.get("login", "00000000"))
            password = mt5_config.get("password", "CHANGE_ME")
            
            logger.info(f"Connecting to MT5: server={server}, login={login}")
            
            await self.adapter.connect(
                server=server,
                login=login,
                password=password
            )
            
            self._connected = True
            logger.info("Successfully connected to MT5")
            
        except Exception as e:
            logger.error(f"Failed to connect to MT5: {e}", exc_info=True)
            raise ConnectionError(f"MT5 connection failed: {e}") from e
    
    async def fetch_and_store_historical_data(
        self,
        asset: str,
        start_date: datetime,
        end_date: datetime,
        intervals: List[str] = None,
        backtest_id: str | None = None
    ) -> Dict[str, int]:
        """Fetch historical data from MT5 and store in MongoDB.
        
        Parameters
        ----------
        asset : str
            Trading symbol (e.g., 'BTCUSD', 'NIFTY')
        start_date : datetime
            Start date for historical data (timezone-aware)
        end_date : datetime
            End date for historical data (timezone-aware)
        intervals : List[str], optional
            List of intervals to fetch. Default: ['5m', '15m']
        
        Returns
        -------
        Dict[str, int]
            Dictionary with counts of candles stored per interval
            Example: {"5m": 12000, "15m": 4000}
        
        Raises
        ------
        ConnectionError
            If MT5 connection fails
        ValueError
            If invalid parameters or MT5 returns no data
        """
        if intervals is None:
            intervals = ["5m", "15m"]
        
        # Validate dates
        if start_date.tzinfo is None:
            start_date = start_date.replace(tzinfo=timezone.utc)
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)
        
        if start_date >= end_date:
            raise ValueError("start_date must be before end_date")
        
        if end_date > datetime.now(timezone.utc):
            raise ValueError("end_date cannot be in the future")
        
        # Ensure MT5 connection
        await self._ensure_connection()
        
        # Fetch data for each interval
        results = {}
        
        for interval in intervals:
            try:
                if interval == "5m":
                    count = await self._fetch_and_store_5m(asset, start_date, end_date, backtest_id=backtest_id)
                    results["5m"] = count
                elif interval == "15m":
                    count = await self._fetch_and_store_15m(asset, start_date, end_date, backtest_id=backtest_id)
                    results["15m"] = count
                else:
                    logger.warning(f"Unsupported interval: {interval}")
                    continue
                
                logger.info(f"Stored {count} {interval} candles for {asset}")
                
            except Exception as e:
                logger.error(f"Failed to fetch {interval} data for {asset}: {e}", exc_info=True)
                results[interval] = 0
        
        return results
    
    async def _fetch_and_store_5m(
        self,
        asset: str,
        start_date: datetime,
        end_date: datetime,
        backtest_id: str | None = None
    ) -> int:
        """Fetch 5-minute candles from MT5 and store in MongoDB."""
        logger.info(f"Fetching 5m candles for {asset} from {start_date} to {end_date}")
        
        # Fetch from MT5 in chunks (1 month at a time to avoid timeouts)
        all_candles = []
        current_start = start_date
        chunk_size = timedelta(days=30)
        
        while current_start < end_date:
            current_end = min(current_start + chunk_size, end_date)
            
            try:
                # Fetch raw bars from MT5
                bars = await self._fetch_mt5_bars(
                    asset,
                    "5m",
                    current_start,
                    current_end
                )
                
                if bars:
                    logger.info(
                        f"Fetched {len(bars)} 5m bars for {asset} "
                        f"from {current_start} to {current_end}"
                    )
                    
                    # Convert to MongoDB format
                    candles = self._convert_bars_to_candles(asset, bars)
                    # Add backtest_id if provided
                    if backtest_id:
                        for candle in candles:
                            candle["backtest_id"] = backtest_id
                    all_candles.extend(candles)
                else:
                    logger.warning(
                        f"No 5m bars found for {asset} "
                        f"from {current_start} to {current_end}"
                    )
                
                # Move to next chunk
                current_start = current_end
                
                # Small delay to avoid rate limiting
                await asyncio.sleep(0.1)
                
            except Exception as e:
                logger.error(
                    f"Error fetching 5m chunk for {asset} "
                    f"from {current_start} to {current_end}: {e}"
                )
                # Continue with next chunk
                current_start = current_end
        
        # Store in MongoDB
        if all_candles:
            await candle_repo.insert_many_5m(all_candles)
            logger.info(f"Stored {len(all_candles)} 5m candles for {asset} in MongoDB")
            return len(all_candles)
        else:
            logger.warning(f"No 5m candles to store for {asset}")
            return 0
    
    async def _fetch_and_store_15m(
        self,
        asset: str,
        start_date: datetime,
        end_date: datetime,
        backtest_id: str | None = None
    ) -> int:
        """Fetch 15-minute candles from MT5 and store in MongoDB."""
        logger.info(f"Fetching 15m candles for {asset} from {start_date} to {end_date}")
        
        # Fetch from MT5 in chunks (1 month at a time)
        all_candles = []
        current_start = start_date
        chunk_size = timedelta(days=30)
        
        while current_start < end_date:
            current_end = min(current_start + chunk_size, end_date)
            
            try:
                # Fetch raw bars from MT5
                bars = await self._fetch_mt5_bars(
                    asset,
                    "15m",
                    current_start,
                    current_end
                )
                
                if bars:
                    logger.info(
                        f"Fetched {len(bars)} 15m bars for {asset} "
                        f"from {current_start} to {current_end}"
                    )
                    
                    # Convert to MongoDB format
                    candles = self._convert_bars_to_candles(asset, bars)
                    # Add backtest_id if provided
                    if backtest_id:
                        for candle in candles:
                            candle["backtest_id"] = backtest_id
                    all_candles.extend(candles)
                else:
                    logger.warning(
                        f"No 15m bars found for {asset} "
                        f"from {current_start} to {current_end}"
                    )
                
                # Move to next chunk
                current_start = current_end
                
                # Small delay to avoid rate limiting
                await asyncio.sleep(0.1)
                
            except Exception as e:
                logger.error(
                    f"Error fetching 15m chunk for {asset} "
                    f"from {current_start} to {current_end}: {e}"
                )
                # Continue with next chunk
                current_start = current_end
        
        # Store in MongoDB
        if all_candles:
            await candle_repo.insert_many_15m(all_candles)
            logger.info(f"Stored {len(all_candles)} 15m candles for {asset} in MongoDB")
            return len(all_candles)
        else:
            logger.warning(f"No 15m candles to store for {asset}")
            return 0
    
    async def _fetch_mt5_bars(
        self,
        symbol: str,
        interval: str,
        start_date: datetime,
        end_date: datetime
    ) -> List[Dict[str, Any]]:
        """Fetch bars from MT5 using copy_rates_range.
        
        Parameters
        ----------
        symbol : str
            Trading symbol
        interval : str
            Timeframe ('5m' or '15m')
        start_date : datetime
            Start date
        end_date : datetime
            End date
        
        Returns
        -------
        List[Dict[str, Any]]
            List of bars with time, open, high, low, close, tick_volume
        """
        mt5 = self.adapter._mt5
        if mt5 is None:
            raise RuntimeError("MT5 not connected")
        
        # Normalize symbol for MT5
        normalized_symbol = self.adapter._normalize_symbol(symbol)
        
        # Get MT5 timeframe constant
        timeframe = {
            "5m": getattr(mt5, "TIMEFRAME_M5"),
            "15m": getattr(mt5, "TIMEFRAME_M15"),
        }[interval]
        
        logger.info(
            f"Fetching MT5 bars: symbol={normalized_symbol}, "
            f"interval={interval}, start={start_date}, end={end_date}"
        )
        
        try:
            # Fetch bars from MT5
            raw = await asyncio.wait_for(
                asyncio.to_thread(
                    mt5.copy_rates_range,
                    normalized_symbol,
                    timeframe,
                    start_date,
                    end_date
                ),
                timeout=60.0  # Longer timeout for historical data
            )
        except asyncio.TimeoutError:
            logger.error(f"MT5 copy_rates_range timeout for {normalized_symbol}")
            return []
        except Exception as e:
            logger.error(f"MT5 copy_rates_range failed: {e}", exc_info=True)
            return []
        
        if raw is None:
            logger.warning(f"MT5 returned None for {normalized_symbol} {interval}")
            return []
        
        if hasattr(raw, '__len__') and len(raw) == 0:
            logger.warning(f"MT5 returned empty data for {normalized_symbol} {interval}")
            return []
        
        # Convert numpy structured array to list of dicts
        import pandas as pd
        bars = []
        for row in raw:
            bar = {
                "time": pd.to_datetime(int(row["time"]), unit="s", utc=True),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "tick_volume": int(row["tick_volume"])
            }
            bars.append(bar)
        
        logger.info(f"Converted {len(bars)} MT5 bars to format")
        return bars
    
    def _convert_bars_to_candles(
        self,
        asset: str,
        bars: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Convert MT5 bars to MongoDB candle format.
        
        Parameters
        ----------
        asset : str
            Trading symbol
        bars : List[Dict[str, Any]]
            MT5 bars with keys: time, open, high, low, close, tick_volume
        
        Returns
        -------
        List[Dict[str, Any]]
            Candles in MongoDB format (without client_order_id to avoid index conflicts)
        """
        candles = []
        
        for bar in bars:
            # Ensure time is datetime (timezone-aware UTC)
            time_val = bar["time"]
            if not isinstance(time_val, datetime):
                # If it's a pandas Timestamp or other type, convert to datetime
                if hasattr(time_val, 'to_pydatetime'):
                    time_val = time_val.to_pydatetime()
                else:
                    time_val = pd.to_datetime(time_val).to_pydatetime()
            
            # Ensure timezone-aware
            if time_val.tzinfo is None:
                time_val = time_val.replace(tzinfo=timezone.utc)
            
            candle = {
                "asset": asset,
                "time": time_val,
                "open": float(bar["open"]),
                "high": float(bar["high"]),
                "low": float(bar["low"]),
                "close": float(bar["close"]),
                "volume": float(bar.get("tick_volume", 0))  # Ensure float, not int
            }
            # Note: Not including client_order_id - historical candles don't need it
            # and the collection has a unique index on it that causes conflicts
            candles.append(candle)
        
        return candles
    
    async def verify_data_exists(
        self,
        asset: str,
        start_date: datetime,
        end_date: datetime
    ) -> Dict[str, int]:
        """Verify that data exists in MongoDB for given range.
        
        Returns count of candles found for each interval.
        """
        from packages.core.historical_candle_service import HistoricalCandleService
        
        hist_service = HistoricalCandleService(enable_cache=False)
        
        # Check 5m candles
        candles_5m = await hist_service.fetch_historical_5m(
            asset=asset,
            start_date=start_date,
            end_date=end_date,
            limit=100000  # Large limit to get all
        )
        
        # Check 15m candles
        candles_15m = await hist_service.fetch_historical_15m(
            asset=asset,
            start_date=start_date,
            end_date=end_date,
            limit=100000  # Large limit to get all
        )
        
        return {
            "5m": len(candles_5m),
            "15m": len(candles_15m)
        }

