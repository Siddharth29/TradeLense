"""CLI script for fetching historical data from MT5 and storing in MongoDB.

Usage:
    python scripts/fetch_historical_data.py --asset BTCUSD --start 2025-05-01 --end 2025-10-31
    python scripts/fetch_historical_data.py --asset NIFTY --start 2025-01-01 --end 2025-12-31 --intervals 5m
"""

import argparse
import asyncio
from datetime import datetime, timezone
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from packages.core.services.mt5_historical_fetcher import MT5HistoricalFetcher
from packages.core.logger import get_logger

logger = get_logger(__name__)


async def main():
    """Main entry point for the CLI script."""
    parser = argparse.ArgumentParser(
        description="Fetch historical data from MT5 and store in MongoDB"
    )
    parser.add_argument(
        "--asset",
        required=True,
        help="Trading symbol (e.g., BTCUSD, NIFTY, EURUSD)"
    )
    parser.add_argument(
        "--start",
        required=True,
        help="Start date in YYYY-MM-DD format (e.g., 2025-05-01)"
    )
    parser.add_argument(
        "--end",
        required=True,
        help="End date in YYYY-MM-DD format (e.g., 2025-10-31)"
    )
    parser.add_argument(
        "--intervals",
        default="5m,15m",
        help="Comma-separated list of intervals (default: 5m,15m)"
    )
    
    args = parser.parse_args()
    
    # Parse dates
    try:
        start_date = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_date = datetime.strptime(args.end, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=timezone.utc
        )
    except ValueError as e:
        print(f"Error parsing dates: {e}")
        print("Date format should be YYYY-MM-DD (e.g., 2025-05-01)")
        sys.exit(1)
    
    # Parse intervals
    intervals = [i.strip() for i in args.intervals.split(',')]
    
    # Validate intervals
    valid_intervals = {"5m", "15m"}
    invalid = set(intervals) - valid_intervals
    if invalid:
        print(f"Error: Invalid intervals: {invalid}")
        print(f"Valid intervals are: {valid_intervals}")
        sys.exit(1)
    
    print("=" * 70)
    print("Fetching Historical Data from MT5")
    print("=" * 70)
    print(f"Asset:      {args.asset}")
    print(f"Start Date: {start_date.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"End Date:   {end_date.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"Intervals:  {', '.join(intervals)}")
    print("=" * 70)
    print()
    
    # Create fetcher
    print("Initializing MT5 connection...")
    fetcher = MT5HistoricalFetcher()
    
    try:
        # Fetch and store data
        print("Fetching historical data from MT5...")
        results = await fetcher.fetch_and_store_historical_data(
            asset=args.asset,
            start_date=start_date,
            end_date=end_date,
            intervals=intervals
        )
        
        print()
        print("=" * 70)
        print("Results:")
        print("=" * 70)
        total_candles = 0
        for interval, count in results.items():
            print(f"{interval:>5}: {count:>8} candles stored")
            total_candles += count
        print("=" * 70)
        print(f"Total: {total_candles:>8} candles stored")
        print("=" * 70)
        print()
        
        # Verify data exists - use direct MongoDB query instead of HistoricalCandleService
        print("Verifying data in MongoDB...")
        from packages.db.repositories import candles as candle_repo
        
        verified = {}
        for interval in intervals:
            if interval == "5m":
                docs = await candle_repo.get_many_5m(
                    asset=args.asset,
                    start=start_date,
                    end=end_date,
                    limit=100000
                )
                verified["5m"] = len(docs)
            elif interval == "15m":
                docs = await candle_repo.get_many_15m(
                    asset=args.asset,
                    start=start_date,
                    end=end_date,
                    limit=100000
                )
                verified["15m"] = len(docs)
        
        print()
        print("=" * 70)
        print("Verification:")
        print("=" * 70)
        for interval, count in verified.items():
            print(f"{interval:>5}: {count:>8} candles found in MongoDB")
        print("=" * 70)
        print()
        
        # Check if counts match
        success = True
        for interval in intervals:
            fetched_count = results.get(interval, 0)
            found_count = verified.get(interval, 0)
            if fetched_count != found_count:
                print(f"WARNING: Mismatch for {interval}!")
                print(f"  Fetched: {fetched_count}")
                print(f"  Found:   {found_count}")
                success = False
        
        if success:
            print("SUCCESS! All data verified in MongoDB")
            print(f"You can now run backtests for {args.asset}")
            return 0
        else:
            print("WARNING: Some data may not have been stored correctly")
            return 1
        
    except Exception as e:
        print()
        print("=" * 70)
        print("ERROR:")
        print("=" * 70)
        print(f"{type(e).__name__}: {e}")
        print("=" * 70)
        logger.error(f"Failed to fetch historical data: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
