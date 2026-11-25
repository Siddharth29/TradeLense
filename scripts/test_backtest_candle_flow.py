"""CLI script to test the entire backtest candle flow.

This script tests:
1. Clean existing candles (optional)
2. Fetch historical data from MT5
3. Verify data stored in MongoDB
4. Test fetching via HistoricalCandleService
5. Verify backtest can use the data

Usage:
    python scripts/test_backtest_candle_flow.py --asset NIFTY --start 2024-05-01 --end 2024-10-31
    python scripts/test_backtest_candle_flow.py --asset NIFTY --start 2024-05-01 --end 2024-10-31 --clean
    python scripts/test_backtest_candle_flow.py --asset NIFTY --start 2024-05-01 --end 2024-10-31 --verify-only
"""

import argparse
import asyncio
import sys
import os
from datetime import datetime, timezone

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from packages.core.services.mt5_historical_fetcher import MT5HistoricalFetcher
from packages.core.historical_candle_service import HistoricalCandleService
from packages.db.repositories import candles as candle_repo
from packages.core.logger import get_logger

logger = get_logger(__name__)


async def main():
    """Main entry point for the CLI script."""
    parser = argparse.ArgumentParser(
        description="Test the entire backtest candle flow"
    )
    parser.add_argument(
        "--asset",
        required=True,
        help="Trading symbol (e.g., NIFTY, BTCUSD)"
    )
    parser.add_argument(
        "--start",
        required=True,
        help="Start date in YYYY-MM-DD format (e.g., 2024-05-01)"
    )
    parser.add_argument(
        "--end",
        required=True,
        help="End date in YYYY-MM-DD format (e.g., 2024-10-31)"
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Clean existing candles before testing"
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Skip fetching, just verify existing data"
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
        print("Date format should be YYYY-MM-DD (e.g., 2024-05-01)")
        return 1
    
    print("=" * 70)
    print("Testing Backtest Candle Flow")
    print("=" * 70)
    print(f"Asset:      {args.asset}")
    print(f"Start Date: {start_date.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"End Date:   {end_date.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"Clean:      {args.clean}")
    print(f"Verify Only: {args.verify_only}")
    print("=" * 70)
    print()
    
    # Step 1: Clean existing candles if requested
    if args.clean:
        print("Step 1: Cleaning existing candles...")
        try:
            count_5m = await candle_repo.delete_all_5m(args.asset)
            count_15m = await candle_repo.delete_all_15m(args.asset)
            print(f"  [OK] Deleted {count_5m} 5m candles")
            print(f"  [OK] Deleted {count_15m} 15m candles")
            print()
        except Exception as e:
            print(f"  [FAIL] Error cleaning candles: {e}")
            return 1
    
    # Step 2: Fetch historical data (unless verify-only)
    if not args.verify_only:
        print("Step 2: Fetching historical data from MT5...")
        try:
            fetcher = MT5HistoricalFetcher()
            results = await fetcher.fetch_and_store_historical_data(
                asset=args.asset,
                start_date=start_date,
                end_date=end_date,
                intervals=["5m", "15m"]
            )
            print(f"  [OK] Fetched {results.get('5m', 0)} 5m candles")
            print(f"  [OK] Fetched {results.get('15m', 0)} 15m candles")
            print()
        except Exception as e:
            print(f"  [FAIL] Error fetching data: {e}")
            logger.error(f"Failed to fetch historical data: {e}", exc_info=True)
            return 1
    
    # Step 3: Verify data stored in MongoDB
    print("Step 3: Verifying data in MongoDB...")
    try:
        count_5m = await candle_repo.count_5m(
            asset=args.asset,
            start=start_date,
            end=end_date
        )
        count_15m = await candle_repo.count_15m(
            asset=args.asset,
            start=start_date,
            end=end_date
        )
        print(f"  [OK] Found {count_5m} 5m candles in MongoDB")
        print(f"  [OK] Found {count_15m} 15m candles in MongoDB")
        print()
        
        if count_5m == 0 and count_15m == 0:
            print("  [WARN] No candles found in MongoDB!")
            print("    Run without --verify-only to fetch data first.")
            return 1
    except Exception as e:
        print(f"  [FAIL] Error verifying data: {e}")
        return 1
    
    # Step 4: Test fetching via HistoricalCandleService
    print("Step 4: Testing HistoricalCandleService...")
    try:
        service = HistoricalCandleService(enable_cache=False)
        
        # Fetch 5m candles
        candles_5m = await service.fetch_historical_5m(
            asset=args.asset,
            start_date=start_date,
            end_date=end_date,
            limit=100
        )
        print(f"  [OK] Fetched {len(candles_5m)} 5m candles via service")
        
        # Validate candle structure
        if candles_5m:
            sample = candles_5m[0]
            required_fields = ["time", "open", "high", "low", "close", "volume"]
            missing = [f for f in required_fields if f not in sample]
            if missing:
                print(f"  [FAIL] Missing fields in 5m candles: {missing}")
                return 1
            print(f"  [OK] 5m candles have correct structure")
        
        # Fetch 15m candles
        candles_15m = await service.fetch_historical_15m(
            asset=args.asset,
            start_date=start_date,
            end_date=end_date,
            limit=50
        )
        print(f"  [OK] Fetched {len(candles_15m)} 15m candles via service")
        
        # Validate candle structure
        if candles_15m:
            sample = candles_15m[0]
            required_fields = ["time", "open", "high", "low", "close", "volume"]
            missing = [f for f in required_fields if f not in sample]
            if missing:
                print(f"  [FAIL] Missing fields in 15m candles: {missing}")
                return 1
            print(f"  [OK] 15m candles have correct structure")
        
        print()
    except Exception as e:
        print(f"  [FAIL] Error testing HistoricalCandleService: {e}")
        logger.error(f"Failed to test service: {e}", exc_info=True)
        return 1
    
    # Step 5: Test that backtest can use the data
    print("Step 5: Verifying backtest compatibility...")
    try:
        # Check if we have enough candles for a typical backtest
        min_5m = 100
        min_15m = 50
        
        if count_5m >= min_5m:
            print(f"  [OK] Sufficient 5m candles for backtest (have {count_5m}, need {min_5m})")
        else:
            print(f"  [WARN] Insufficient 5m candles for backtest (have {count_5m}, need {min_5m})")
        
        if count_15m >= min_15m:
            print(f"  [OK] Sufficient 15m candles for backtest (have {count_15m}, need {min_15m})")
        else:
            print(f"  [WARN] Insufficient 15m candles for backtest (have {count_15m}, need {min_15m})")
        
        print()
    except Exception as e:
        print(f"  [FAIL] Error verifying backtest compatibility: {e}")
        return 1
    
    print("=" * 70)
    print("[SUCCESS] All tests passed!")
    print("=" * 70)
    print()
    print("You can now run backtests for this asset/date range.")
    
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

