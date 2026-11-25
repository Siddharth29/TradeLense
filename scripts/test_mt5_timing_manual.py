"""Manual test script for MT5 timing fixes."""
import asyncio
import json
import sys
import os
from datetime import datetime, timezone, timedelta

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from packages.adapters.mt5_adapter import MT5Adapter

async def main():
    """Run manual timing tests."""
    print("=" * 60)
    print("MT5 Timing Fix Manual Test")
    print("=" * 60)
    
    # Load config
    config_path = os.path.join(os.path.dirname(__file__), '..', 'config', 'mt5_config.json')
    try:
        with open(config_path, 'r') as f:
            config_data = json.load(f)
            config = config_data['mt5']  # Extract mt5 section
    except FileNotFoundError:
        print(f"❌ MT5 config not found at: {config_path}")
        return
    
    adapter = MT5Adapter()
    
    try:
        # Connect
        print("\n1. Connecting to MT5...")
        await adapter.connect(**config)
        print("✓ Connected")
        
        # Test current time
        print(f"\n2. Current time: {datetime.now(timezone.utc)}")
        
        # Test _compute_bounds method
        print("\n3. Testing _compute_bounds at various times...")
        test_times = [
            datetime.now(timezone.utc),
            datetime(2025, 10, 4, 17, 30, 0, tzinfo=timezone.utc),
            datetime(2025, 10, 4, 18, 30, 0, tzinfo=timezone.utc),
            datetime(2025, 10, 4, 0, 15, 0, tzinfo=timezone.utc),
        ]
        
        for test_time in test_times:
            start, end = adapter._compute_bounds(test_time, 5, 70)
            span_minutes = (end - start).total_seconds() / 60
            print(f"  Time: {test_time}")
            print(f"    Start: {start}")
            print(f"    End:   {end}")
            print(f"    Span:  {span_minutes:.1f} minutes")
            print(f"    Crosses day: {start.date() != end.date()}")
            print()
        
        # Test 5m candles
        print("4. Fetching 70x 5m candles for BTCUSD...")
        candles_5m = await adapter.candles('BTCUSD', '5m', 70)
        print(f"✓ Got {len(candles_5m)} candles")
        if candles_5m:
            print(f"  First: {candles_5m[0].time}")
            print(f"  Last:  {candles_5m[-1].time}")
            
            age = datetime.now(timezone.utc) - candles_5m[-1].time
            print(f"  Age of last candle: {age}")
            
            if age.total_seconds() > 600:
                print(f"  ⚠️  WARNING: Candles are {age.total_seconds()/60:.1f} min old")
            else:
                print(f"  ✓ Candles are fresh")
        
        # Test 15m candles
        print("\n5. Fetching 50x 15m candles for BTCUSD...")
        candles_15m = await adapter.candles('BTCUSD', '15m', 50)
        print(f"✓ Got {len(candles_15m)} candles")
        if candles_15m:
            print(f"  First: {candles_15m[0].time}")
            print(f"  Last:  {candles_15m[-1].time}")
            
            age = datetime.now(timezone.utc) - candles_15m[-1].time
            print(f"  Age of last candle: {age}")
        
        # Test at simulated 17:30 (original bug time)
        print("\n6. Testing _compute_bounds at simulated 17:30...")
        test_time = datetime(2025, 10, 4, 17, 30, 0, tzinfo=timezone.utc)
        start, end = adapter._compute_bounds(test_time, 5, 70)
        print(f"  Start: {start}")
        print(f"  End:   {end}")
        print(f"  Expected ~350 minutes: {(end-start).total_seconds()/60:.1f} min")
        
        # Test at simulated 18:30 (another bug time)
        print("\n7. Testing _compute_bounds at simulated 18:30...")
        test_time = datetime(2025, 10, 4, 18, 30, 0, tzinfo=timezone.utc)
        start, end = adapter._compute_bounds(test_time, 5, 70)
        print(f"  Start: {start}")
        print(f"  End:   {end}")
        print(f"  Expected ~350 minutes: {(end-start).total_seconds()/60:.1f} min")
        
        # Test midnight crossing
        print("\n8. Testing midnight boundary crossing...")
        test_time = datetime(2025, 10, 5, 0, 15, 0, tzinfo=timezone.utc)
        start, end = adapter._compute_bounds(test_time, 5, 70)
        print(f"  Start: {start} (date: {start.date()})")
        print(f"  End:   {end} (date: {end.date()})")
        print(f"  Crosses day boundary: {start.date() != end.date()}")
        
        # Test candle count validation
        print("\n9. Testing candle count thresholds...")
        print(f"  BTCUSD is crypto: {'BTCUSD' in ['BTCUSD', 'BTCUSDT', 'ETHUSD', 'ETHUSDT', 'XAUUSD']}")
        print(f"  Expected threshold: 95% for crypto")
        print(f"  For 70 candles: min acceptable = {int(70 * 0.95)}")
        
        print("\n" + "=" * 60)
        print("✓ All manual tests completed!")
        print("=" * 60)
        
    except Exception as e:
        print(f"\n❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        
    finally:
        try:
            adapter.disconnect()
            print("\n✓ MT5 disconnected")
        except:
            pass

if __name__ == '__main__':
    asyncio.run(main())
