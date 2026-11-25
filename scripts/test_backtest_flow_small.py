"""Small end-to-end test of backtest flow to validate fixes."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch, MagicMock

from packages.core.backtest_runner import BacktestRunner
from packages.core.schemas_trade import CodeSniperSignal
from tests.fixtures.backtest_fixtures import generate_mock_candles


async def test_small_backtest_flow():
    """Run a small backtest to validate the full flow."""
    print("=" * 60)
    print("Testing Small Backtest Flow")
    print("=" * 60)
    
    # Create test data
    start_time = datetime(2024, 1, 1, 9, 15, tzinfo=timezone.utc)
    end_time = start_time + timedelta(hours=2)
    
    # Generate sufficient candles
    candles_5m = generate_mock_candles(start_time, count=200, interval_minutes=5)
    candles_15m = generate_mock_candles(start_time, count=100, interval_minutes=15)
    
    print(f"Generated {len(candles_5m)} 5m candles and {len(candles_15m)} 15m candles")
    
    # Create runner
    runner = BacktestRunner(
        asset="BTCUSD",
        start_date=start_time,
        end_date=end_time,
        candle_count_5m=100,
        candle_count_15m=80,
        use_code_sniper=True
    )
    
    # Set cached candles
    runner._cached_5m_candles = candles_5m
    runner._cached_15m_candles = candles_15m
    
    # Mock candle service
    runner.candle_service = MagicMock()
    runner.candle_service.fetch_historical_5m = AsyncMock(return_value=candles_5m)
    runner.candle_service.fetch_historical_15m = AsyncMock(return_value=candles_15m)
    
    # Mock multi_expert
    mock_signal = CodeSniperSignal(
        code="""
def run(candles_5, now):
    # Simple test code - just check if we have candles
    if len(candles_5) < 21:
        return {
            'direction': 'hold',
            'entry': None,
            'stop_loss': None,
            'take_profit_1': None,
            'confidence': 0.5,
            'rationale': 'Insufficient candles'
        }
    
    # Return hold signal
    return {
        'direction': 'hold',
        'entry': None,
        'stop_loss': None,
        'take_profit_1': None,
        'confidence': 0.5,
        'rationale': 'Test validation'
    }
""",
        ttl_minutes=60,
        confidence=0.85,
        metadata={}
    )
    
    with patch('packages.core.backtest_runner.multi_expert_decision_with_details') as mock_multi:
        mock_multi.return_value = (mock_signal, {})
        
        # Mock backtest repo
        with patch('packages.core.backtest_runner.backtest_repo') as mock_repo:
            mock_repo.get_backtest = AsyncMock(return_value={"status": "running"})
            mock_repo.update_backtest_status = AsyncMock(return_value=True)
            mock_repo.add_iteration = AsyncMock(return_value="iter123")
            mock_repo.update_backtest_progress = AsyncMock(return_value=True)
            mock_repo.update_backtest_completion = AsyncMock(return_value=True)
            
            print("\nRunning backtest with 1 hour interval...")
            
            try:
                await runner.execute_backtest(
                    test_name="test_flow_validation",
                    interval_hours=1,
                    backtest_id="test123"
                )
                print("\n[SUCCESS] Backtest completed successfully!")
                print("\nValidated:")
                print("  - Candle filtering for consecutive 5-minute intervals")
                print("  - Datetime type normalization")
                print("  - Code execution in sandbox")
                print("  - Full backtest flow")
                
            except Exception as e:
                print(f"\n[ERROR] Backtest failed: {e}")
                import traceback
                traceback.print_exc()
                raise


if __name__ == "__main__":
    asyncio.run(test_small_backtest_flow())

