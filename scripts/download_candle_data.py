"""CLI script for downloading 2 years of BTCUSD candle data from MT5 and saving to CSV.

Usage:
    python scripts/download_candle_data.py
    python scripts/download_candle_data.py --asset BTCUSD
"""

import argparse
import asyncio
import sys
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from packages.adapters.mt5_adapter import MT5Adapter
from packages.core.config import DATA_DIR
from packages.core.logger import get_logger
import pandas as pd

logger = get_logger(__name__)


def _load_mt5_config() -> dict[str, str]:
    """Load LIVE MT5 credentials. Never falls back to the demo account.

    2026-08-07: this used to read the COMMITTED ``config/mt5_config.json``
    (Example-Demo) and, on any error, fall back to hardcoded trial
    credentials — silently. Since the file it writes is the backtest's candle
    source, that meant a failed read produced a CSV full of DEMO-server prices
    that then looked exactly like real data for months. The repo already treats
    this as a hard rule elsewhere (``scripts/audit/pull_live_mt5.py``:
    "refusing to use demo config"); this now follows it.
    """
    import json

    config_path = Path(__file__).resolve().parents[1] / "config" / "mt5_config.local.json"
    if not config_path.exists():
        raise SystemExit(
            f"LIVE creds not found at {config_path}. Refusing to fall back to the "
            f"committed demo config — backtest candles must come from the real account."
        )
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)["mt5"]
    if "real" not in str(cfg.get("server", "")).lower():
        raise SystemExit(
            f"server '{cfg.get('server')}' is not a real account — refusing."
        )
    return cfg


async def fetch_mt5_bars(
    adapter: MT5Adapter,
    symbol: str,
    interval: str,
    start_date: datetime,
    end_date: datetime
) -> list[dict[str, any]]:
    """Fetch bars from MT5 using copy_rates_range.
    
    Parameters
    ----------
    adapter : MT5Adapter
        Connected MT5 adapter
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
        List of bars with time, open, high, low, close, volume
    """
    mt5 = adapter._mt5
    if mt5 is None:
        raise RuntimeError("MT5 not connected")
    
    # Normalize symbol for MT5
    normalized_symbol = adapter._normalize_symbol(symbol)
    
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
            timeout=120.0  # Longer timeout for large date ranges
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
    bars = []
    for row in raw:
        # Access numpy structured array fields by name
        bar = {
            "time": pd.to_datetime(int(row["time"]), unit="s", utc=True),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["tick_volume"]) if "tick_volume" in row.dtype.names else 0.0
        }
        bars.append(bar)
    
    logger.info(f"Fetched {len(bars)} bars for {symbol} {interval}")
    return bars


async def download_candle_data(
    asset: str = "BTCUSD",
    years: int = 2
) -> None:
    """Download 2 years of candle data for BTCUSD and save to CSV.
    
    Parameters
    ----------
    asset : str
        Asset symbol (default: BTCUSD)
    years : int
        Number of years of data to download (default: 2)
    """
    print("=" * 70)
    print(f"Downloading {years} years of {asset} candle data from MT5")
    print("=" * 70)
    
    # Calculate date range (2 years back from today)
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=years * 365)
    
    print(f"Asset:      {asset}")
    print(f"Start Date: {start_date.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"End Date:   {end_date.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print("=" * 70)
    print()
    
    # Create MT5 adapter and connect
    print("Initializing MT5 connection...")
    adapter = MT5Adapter()
    
    try:
        # Load credentials
        mt5_config = _load_mt5_config()
        server = mt5_config.get("server")
        login = str(mt5_config.get("login"))
        password = mt5_config.get("password")
        
        print(f"Connecting to MT5: server={server}, login={login}")
        await adapter.connect(server=server, login=login, password=password)
        print("Successfully connected to MT5")
        print()
        
        # Ensure data directory exists
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        
        # Download for each interval
        intervals = ["5m", "15m"]
        
        for interval in intervals:
            print(f"Downloading {interval} candles...")
            
            # Fetch data in chunks (1 month at a time to avoid timeouts)
            all_candles = []
            current_start = start_date
            chunk_size = timedelta(days=30)
            
            while current_start < end_date:
                current_end = min(current_start + chunk_size, end_date)
                
                try:
                    bars = await fetch_mt5_bars(
                        adapter,
                        asset,
                        interval,
                        current_start,
                        current_end
                    )
                    
                    if bars:
                        print(f"  Fetched {len(bars)} {interval} candles from {current_start.date()} to {current_end.date()}")
                        all_candles.extend(bars)
                    else:
                        print(f"  No {interval} candles found for {current_start.date()} to {current_end.date()}")
                    
                    # Move to next chunk
                    current_start = current_end
                    
                    # Small delay to avoid rate limiting
                    await asyncio.sleep(0.1)
                    
                except Exception as e:
                    logger.error(
                        f"Error fetching {interval} chunk from {current_start} to {current_end}: {e}"
                    )
                    # Continue with next chunk
                    current_start = current_end
            
            # Convert to DataFrame and save to CSV
            if all_candles:
                # Remove duplicates by time (keep first occurrence)
                seen_times = set()
                unique_candles = []
                for candle in all_candles:
                    time_key = candle["time"]
                    if time_key not in seen_times:
                        seen_times.add(time_key)
                        unique_candles.append(candle)
                
                # Sort by time
                unique_candles.sort(key=lambda x: x["time"])
                
                # Create DataFrame
                df = pd.DataFrame(unique_candles)
                df.set_index("time", inplace=True)
                
                # Save to CSV
                filename = f"{asset.lower()}_{interval}_2years.csv"
                filepath = DATA_DIR / filename
                df.to_csv(filepath)
                
                print(f"  Saved {len(unique_candles)} {interval} candles to {filepath}")
                print(f"  Date range: {df.index[0]} to {df.index[-1]}")
                print()
            else:
                print(f"  No {interval} candles to save")
                print()
        
        print("=" * 70)
        print("Download complete!")
        print("=" * 70)
        
    except Exception as e:
        logger.error(f"Failed to download candle data: {e}", exc_info=True)
        print(f"Error: {e}")
        raise
    
    finally:
        # Disconnect MT5
        try:
            adapter.disconnect()
            print("MT5 disconnected")
        except Exception:
            pass


async def main():
    """Main entry point for the CLI script."""
    parser = argparse.ArgumentParser(
        description="Download 2 years of BTCUSD candle data from MT5 and save to CSV"
    )
    parser.add_argument(
        "--asset",
        default="BTCUSD",
        help="Asset symbol (default: BTCUSD)"
    )
    parser.add_argument(
        "--years",
        type=int,
        default=2,
        help="Number of years of data to download (default: 2)"
    )
    
    args = parser.parse_args()
    
    try:
        await download_candle_data(asset=args.asset, years=args.years)
    except KeyboardInterrupt:
        print("\nDownload interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

