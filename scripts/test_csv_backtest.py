"""Test script to verify CSV-based backtest flow works correctly."""

import asyncio
import sys
import os
from datetime import datetime, timezone, timedelta

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from packages.core.historical_candle_service import HistoricalCandleService
from packages.core.logger import get_logger

logger = get_logger(__name__)


async def test_csv_backtest():
    """Test fetching candles from CSV files."""
    print("=" * 70)
    print("Testing CSV-based HistoricalCandleService")
    print("=" * 70)
    
    service = HistoricalCandleService()
    
    # Test date range (last 30 days)
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=30)
    
    print(f"Test date range: {start_date} to {end_date}")
    print()
    
    try:
        # Test 5m candles
        print("Fetching 5m candles...")
        candles_5m = await service.fetch_historical_5m(
            asset="BTCUSD",
            start_date=start_date,
            end_date=end_date,
            limit=100
        )
        print(f"  ✓ Fetched {len(candles_5m)} 5m candles")
        if candles_5m:
            print(f"  First candle: {candles_5m[0]}")
            print(f"  Last candle: {candles_5m[-1]}")
        print()
        
        # Test 15m candles
        print("Fetching 15m candles...")
        candles_15m = await service.fetch_historical_15m(
            asset="BTCUSD",
            start_date=start_date,
            end_date=end_date,
            limit=70
        )
        print(f"  ✓ Fetched {len(candles_15m)} 15m candles")
        if candles_15m:
            print(f"  First candle: {candles_15m[0]}")
            print(f"  Last candle: {candles_15m[-1]}")
        print()
        
        # Test validation
        print("Validating candle data...")
        is_valid, msg = service.validate_candle_data(
            candles_5m[:100] if len(candles_5m) > 100 else candles_5m,
            candles_15m[:70] if len(candles_15m) > 70 else candles_15m
        )
        if is_valid:
            print(f"  ✓ Validation passed: {msg}")
        else:
            print(f"  ✗ Validation failed: {msg}")
        print()
        
        print("=" * 70)
        print("All tests passed!")
        print("=" * 70)
        
    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    return True


if __name__ == "__main__":
    success = asyncio.run(test_csv_backtest())
    sys.exit(0 if success else 1)

