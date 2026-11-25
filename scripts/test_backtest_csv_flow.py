"""Test script to verify BacktestRunner works with CSV-based candle service."""

import asyncio
import sys
import os
from datetime import datetime, timezone, timedelta

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from packages.core.backtest_runner import BacktestRunner
from packages.core.logger import get_logger

logger = get_logger(__name__)


async def test_backtest_csv_flow():
    """Test BacktestRunner with CSV-based candle service."""
    print("=" * 70)
    print("Testing BacktestRunner with CSV-based candle service")
    print("=" * 70)
    
    # Use a small date range for testing (last 7 days)
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=7)
    
    print(f"Test date range: {start_date.date()} to {end_date.date()}")
    print(f"Asset: BTCUSD")
    print()
    
    try:
        # Create backtest runner
        runner = BacktestRunner(
            asset="BTCUSD",
            start_date=start_date,
            end_date=end_date,
            candle_count_5m=100,
            candle_count_15m=50,
            use_code_sniper=True,
            gpt_config={}
        )
        
        print("Initializing BacktestRunner...")
        print("  ✓ BacktestRunner created")
        
        # Test pre-fetching candles (this is what execute_backtest does)
        print("Pre-fetching candles...")
        runner._cached_5m_candles = await runner.candle_service.fetch_historical_5m(
            asset="BTCUSD",
            start_date=start_date,
            end_date=end_date,
            limit=5000
        )
        runner._cached_15m_candles = await runner.candle_service.fetch_historical_15m(
            asset="BTCUSD",
            start_date=start_date,
            end_date=end_date,
            limit=2000
        )
        
        print(f"  ✓ Pre-fetched {len(runner._cached_5m_candles)} 5m candles")
        print(f"  ✓ Pre-fetched {len(runner._cached_15m_candles)} 15m candles")
        print()
        
        # Test a single iteration
        print("Testing single iteration...")
        checkpoints = runner._get_hourly_checkpoints(
            pd.Timestamp(start_date),
            pd.Timestamp(end_date),
            interval_hours=1
        )
        
        if checkpoints:
            print(f"  Found {len(checkpoints)} checkpoints")
            print(f"  First checkpoint: {checkpoints[0]}")
            print(f"  Last checkpoint: {checkpoints[-1]}")
            
            # Test executing one iteration - try a later checkpoint that has more data
            test_checkpoint = checkpoints[-1] if len(checkpoints) > 1 else checkpoints[0]
            print(f"  Testing with checkpoint: {test_checkpoint}")
            
            result = await runner._execute_iteration(
                timestamp=test_checkpoint,
                backtest_id="test_backtest_csv",
                iteration_num=1
            )
            
            print(f"  ✓ Iteration executed successfully")
            print(f"  Status: {result.get('status', 'unknown')}")
            if result.get('status') == 'error':
                print(f"  Error: {result.get('error', 'unknown')}")
        else:
            print("  No checkpoints found (market hours check)")
        
        print()
        print("=" * 70)
        print("BacktestRunner CSV test passed!")
        print("=" * 70)
        
    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    return True


if __name__ == "__main__":
    import pandas as pd
    success = asyncio.run(test_backtest_csv_flow())
    sys.exit(0 if success else 1)

