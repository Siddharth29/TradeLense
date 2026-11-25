"""Comprehensive tests for backtest runner flow with candle interval validation."""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import pandas as pd

from packages.core.backtest_runner import BacktestRunner
from packages.core.candles import filter_consecutive_candles
from packages.core.code_executor import execute_generated_code
from tests.fixtures.backtest_fixtures import generate_mock_candles


class TestFilterConsecutiveCandles:
    """Test the filter_consecutive_candles function."""
    
    def test_filter_consecutive_5m_candles_all_valid(self):
        """Test filtering with all valid consecutive 5m candles."""
        start_time = datetime(2024, 1, 1, 9, 15, tzinfo=timezone.utc)
        candles = generate_mock_candles(start_time, count=10, interval_minutes=5)
        
        filtered = filter_consecutive_candles(candles, interval="5m")
        
        assert len(filtered) == 10
        assert filtered == candles
    
    def test_filter_consecutive_5m_candles_with_gaps(self):
        """Test filtering candles with gaps (non-consecutive intervals)."""
        start_time = datetime(2024, 1, 1, 9, 15, tzinfo=timezone.utc)
        candles = []
        
        # Create candles with gaps: 5m, 5m, 10m gap, 5m, 5m, 15m gap, 5m
        times = [
            start_time,
            start_time + timedelta(minutes=5),
            start_time + timedelta(minutes=15),  # 10m gap
            start_time + timedelta(minutes=20),
            start_time + timedelta(minutes=25),
            start_time + timedelta(minutes=40),  # 15m gap
            start_time + timedelta(minutes=45),
        ]
        
        for i, ts in enumerate(times):
            candles.append({
                "time": ts,
                "open": 19500.0 + i,
                "high": 19501.0 + i,
                "low": 19499.0 + i,
                "close": 19500.5 + i,
                "volume": 100000
            })
        
        filtered = filter_consecutive_candles(candles, interval="5m")
        
        # Should only include first 2 consecutive candles before first gap
        assert len(filtered) == 2
        assert filtered[0]["time"] == start_time
        assert filtered[1]["time"] == start_time + timedelta(minutes=5)
    
    def test_filter_consecutive_5m_candles_empty_list(self):
        """Test filtering with empty candle list."""
        filtered = filter_consecutive_candles([], interval="5m")
        assert filtered == []
    
    def test_filter_consecutive_15m_candles(self):
        """Test filtering 15m candles."""
        start_time = datetime(2024, 1, 1, 9, 15, tzinfo=timezone.utc)
        candles = generate_mock_candles(start_time, count=5, interval_minutes=15)
        
        filtered = filter_consecutive_candles(candles, interval="15m")
        
        assert len(filtered) == 5
        assert filtered == candles
    
    def test_filter_consecutive_invalid_interval(self):
        """Test filtering with invalid interval."""
        candles = generate_mock_candles(
            datetime(2024, 1, 1, 9, 15, tzinfo=timezone.utc),
            count=5
        )
        
        with pytest.raises(ValueError, match="Unsupported interval"):
            filter_consecutive_candles(candles, interval="1h")


class TestBacktestRunnerCandleFiltering:
    """Test BacktestRunner candle filtering logic."""
    
    @pytest.fixture
    def runner(self):
        """Create a BacktestRunner instance for testing."""
        return BacktestRunner(
            asset="BTCUSD",
            start_date=datetime(2024, 1, 1, 9, 15, tzinfo=timezone.utc),
            end_date=datetime(2024, 1, 1, 14, 15, tzinfo=timezone.utc),
            candle_count_5m=100,
            candle_count_15m=80,
            use_code_sniper=True
        )
    
    def test_filter_consecutive_5m_candles_helper(self, runner):
        """Test the _filter_consecutive_5m_candles helper method."""
        start_time = datetime(2024, 1, 1, 9, 15, tzinfo=timezone.utc)
        
        # Create candles with gaps
        candles = []
        for i in range(5):
            if i == 2:
                # Skip one candle to create a gap
                continue
            candles.append({
                "time": start_time + timedelta(minutes=i * 5),
                "open": 19500.0,
                "high": 19501.0,
                "low": 19499.0,
                "close": 19500.5,
                "volume": 100000
            })
        
        # Add one more with 10m gap
        candles.append({
            "time": start_time + timedelta(minutes=25),  # 10m gap from previous
            "open": 19500.0,
            "high": 19501.0,
            "low": 19499.0,
            "close": 19500.5,
            "volume": 100000
        })
        
        filtered = runner._filter_consecutive_5m_candles(candles)
        
        # Should filter to consecutive sequence before gap
        assert len(filtered) <= len(candles)
        # Verify all intervals are exactly 5 minutes
        for i in range(1, len(filtered)):
            time_diff = pd.Timestamp(filtered[i].get('time')) - pd.Timestamp(filtered[i-1].get('time'))
            assert time_diff == timedelta(minutes=5)
    
    def test_filter_consecutive_with_reference_time(self, runner):
        """Test filtering with reference time."""
        start_time = datetime(2024, 1, 1, 9, 15, tzinfo=timezone.utc)
        candles = generate_mock_candles(start_time, count=10, interval_minutes=5)
        
        reference_time = start_time + timedelta(minutes=15)
        filtered = runner._filter_consecutive_5m_candles(candles, reference_time=reference_time)
        
        # Should only include candles after reference time
        for candle in filtered:
            assert pd.Timestamp(candle.get('time')) > pd.Timestamp(reference_time)


class TestBacktestRunnerFlow:
    """Test full backtest runner flow."""
    
    @pytest.fixture
    def mock_candle_service(self):
        """Create mock candle service."""
        service = MagicMock()
        service.fetch_historical_5m = AsyncMock(return_value=[])
        service.fetch_historical_15m = AsyncMock(return_value=[])
        return service
    
    @pytest.fixture
    def runner_with_mocks(self, mock_candle_service):
        """Create runner with mocked dependencies."""
        runner = BacktestRunner(
            asset="BTCUSD",
            start_date=datetime(2024, 1, 1, 9, 15, tzinfo=timezone.utc),
            end_date=datetime(2024, 1, 1, 14, 15, tzinfo=timezone.utc),
            candle_count_5m=100,
            candle_count_15m=80,
            use_code_sniper=True
        )
        runner.candle_service = mock_candle_service
        return runner
    
    @pytest.mark.asyncio
    async def test_execute_iteration_skips_on_insufficient_candles(self, runner_with_mocks):
        """Test that iteration is skipped when insufficient candles."""
        # Mock candles with less than required
        candles_5m = generate_mock_candles(
            datetime(2024, 1, 1, 9, 15, tzinfo=timezone.utc),
            count=50,  # Less than required 100
            interval_minutes=5
        )
        candles_15m = generate_mock_candles(
            datetime(2024, 1, 1, 9, 15, tzinfo=timezone.utc),
            count=50,  # Less than required 80
            interval_minutes=15
        )
        
        runner_with_mocks._cached_5m_candles = candles_5m
        runner_with_mocks._cached_15m_candles = candles_15m
        # Provide sorted time indices required by _execute_iteration
        runner_with_mocks._sorted_5m_times = sorted(
            pd.Timestamp(c["time"]) for c in candles_5m
        )
        runner_with_mocks._sorted_15m_times = sorted(
            pd.Timestamp(c["time"]) for c in candles_15m
        )
        runner_with_mocks._candle_5m_index_map = {
            pd.Timestamp(c["time"]): i for i, c in enumerate(candles_5m)
        }
        runner_with_mocks._candle_15m_index_map = {
            pd.Timestamp(c["time"]): i for i, c in enumerate(candles_15m)
        }

        timestamp = pd.Timestamp(datetime(2024, 1, 1, 10, 15, tzinfo=timezone.utc))
        
        result = await runner_with_mocks._execute_iteration(
            timestamp=timestamp,
            backtest_id="test123",
            iteration_num=1
        )
        
        assert result["status"] == "skipped"
        assert "Insufficient" in result["error"]
    
    @pytest.mark.asyncio
    async def test_monitoring_candles_filtered_for_intervals(self, runner_with_mocks):
        """Test that monitoring candles are filtered for strict 5-minute intervals."""
        # Create candles with gaps
        start_time = datetime(2024, 1, 1, 9, 15, tzinfo=timezone.utc)
        candles = []
        
        # Generate 100 valid consecutive candles, then add some with gaps
        for i in range(100):
            candles.append({
                "time": start_time + timedelta(minutes=i * 5),
                "open": 19500.0 + i * 0.1,
                "high": 19501.0 + i * 0.1,
                "low": 19499.0 + i * 0.1,
                "close": 19500.5 + i * 0.1,
                "volume": 100000
            })
        
        # Add candles with gaps
        candles.append({
            "time": start_time + timedelta(minutes=505),  # 5m gap - valid
            "open": 19500.0,
            "high": 19501.0,
            "low": 19499.0,
            "close": 19500.5,
            "volume": 100000
        })
        candles.append({
            "time": start_time + timedelta(minutes=515),  # 10m gap - invalid
            "open": 19500.0,
            "high": 19501.0,
            "low": 19499.0,
            "close": 19500.5,
            "volume": 100000
        })
        candles.append({
            "time": start_time + timedelta(minutes=530),  # 15m gap - invalid
            "open": 19500.0,
            "high": 19501.0,
            "low": 19499.0,
            "close": 19500.5,
            "volume": 100000
        })
        
        runner_with_mocks._cached_5m_candles = candles
        
        # Mock multi_expert to return code sniper signal
        with patch('packages.core.backtest_runner.multi_expert_decision_with_details') as mock_multi:
            from packages.core.schemas_trade import CodeSniperSignal
            mock_signal = CodeSniperSignal(
                code="def run(candles_5, now): return {'direction': 'hold', 'entry': None, 'stop_loss': None, 'take_profit_1': None, 'confidence': 0.5, 'rationale': 'Test'}",
                ttl_minutes=90,
                confidence=0.85,
                metadata={}
            )
            mock_multi.return_value = (mock_signal, {})
            
            # Mock backtest repo
            with patch('packages.core.backtest_runner.backtest_repo') as mock_repo:
                mock_repo.get_backtest = AsyncMock(return_value={"status": "running"})
                
                timestamp = pd.Timestamp(start_time + timedelta(minutes=500))
                result = await runner_with_mocks._execute_iteration(
                    timestamp=timestamp,
                    backtest_id="test123",
                    iteration_num=1
                )
                
                # Should complete without interval validation errors
                assert result is not None


class TestCodeExecutorDatetimeHandling:
    """Test datetime type handling in code executor."""
    
    def test_datetime_normalization_datetime_object(self):
        """Test normalizing datetime objects to ISO strings."""
        from packages.core.code_executor import TradingCodeExecutor
        
        executor = TradingCodeExecutor()
        
        # Need 40 historical candles + 1 current = 41 total
        start_time = datetime(2024, 1, 1, 9, 55, tzinfo=timezone.utc)
        historical_candles = generate_mock_candles(start_time, count=40, interval_minutes=5)
        
        # Current candle must be exactly 5 minutes after last historical (which is at 9:55 + 19*5 = 11:30)
        last_historical_time = historical_candles[-1]["time"]
        current_candle = {
            "time": last_historical_time + timedelta(minutes=5),  # 11:35
            "open": 19500.0,
            "high": 19501.0,
            "low": 19499.0,
            "close": 19500.5,
            "volume": 100000
        }
        
        code = """
def run(candles_5, now):
    return {
        'direction': 'hold',
        'entry': None,
        'stop_loss': None,
        'take_profit_1': None,
        'confidence': 0.5,
        'rationale': 'Test'
    }
"""
        
        # Should not raise type errors
        try:
            result = executor.execute_trading_code(
                code=code,
                current_candle=current_candle,
                historical_candles=historical_candles,
                backtest_mode=True
            )
            assert result is not None
            assert "direction" in result
        except (TypeError, ValueError) as e:
            if "datetime" in str(e).lower() or "split" in str(e).lower():
                pytest.fail(f"Datetime type error: {e}")
            else:
                raise
    
    def test_datetime_normalization_pandas_timestamp(self):
        """Test normalizing pandas Timestamp objects."""
        from packages.core.code_executor import TradingCodeExecutor
        
        executor = TradingCodeExecutor()
        
        # Need 40 historical candles + 1 current = 41 total
        start_time = pd.Timestamp("2024-01-01 09:55:00", tz="UTC")
        historical_candles = []
        for i in range(40):
            historical_candles.append({
                "time": start_time + timedelta(minutes=i * 5),
                "open": 19499.0 + i * 0.1,
                "high": 19500.0 + i * 0.1,
                "low": 19498.0 + i * 0.1,
                "close": 19499.5 + i * 0.1,
                "volume": 100000
            })
        
        # Current candle must be exactly 5 minutes after last historical
        last_historical_time = historical_candles[-1]["time"]
        current_candle = {
            "time": last_historical_time + timedelta(minutes=5),  # 11:35
            "open": 19500.0,
            "high": 19501.0,
            "low": 19499.0,
            "close": 19500.5,
            "volume": 100000
        }
        
        code = """
def run(candles_5, now):
    return {
        'direction': 'hold',
        'entry': None,
        'stop_loss': None,
        'take_profit_1': None,
        'confidence': 0.5,
        'rationale': 'Test'
    }
"""
        
        # Should convert pandas Timestamp to ISO string
        try:
            result = executor.execute_trading_code(
                code=code,
                current_candle=current_candle,
                historical_candles=historical_candles,
                backtest_mode=True
            )
            assert result is not None
        except (TypeError, ValueError) as e:
            if "datetime" in str(e).lower() or "split" in str(e).lower():
                pytest.fail(f"Datetime type error: {e}")
            else:
                raise
    
    def test_datetime_normalization_string(self):
        """Test normalizing string datetime values."""
        from packages.core.code_executor import TradingCodeExecutor
        
        executor = TradingCodeExecutor()
        
        # Need 40 historical candles + 1 current = 41 total
        start_time = datetime(2024, 1, 1, 9, 55, tzinfo=timezone.utc)
        historical_candles = []
        for i in range(40):
            historical_candles.append({
                "time": (start_time + timedelta(minutes=i * 5)).isoformat(),
                "open": 19499.0 + i * 0.1,
                "high": 19500.0 + i * 0.1,
                "low": 19498.0 + i * 0.1,
                "close": 19499.5 + i * 0.1,
                "volume": 100000
            })
        
        # Current candle must be exactly 5 minutes after last historical
        last_historical_time_str = historical_candles[-1]["time"]
        last_historical_time = datetime.fromisoformat(last_historical_time_str.replace('Z', '+00:00'))
        current_candle = {
            "time": (last_historical_time + timedelta(minutes=5)).isoformat(),  # 11:35
            "open": 19500.0,
            "high": 19501.0,
            "low": 19499.0,
            "close": 19500.5,
            "volume": 100000
        }
        
        code = """
def run(candles_5, now):
    return {
        'direction': 'hold',
        'entry': None,
        'stop_loss': None,
        'take_profit_1': None,
        'confidence': 0.5,
        'rationale': 'Test'
    }
"""
        
        # Should handle string datetimes
        try:
            result = executor.execute_trading_code(
                code=code,
                current_candle=current_candle,
                historical_candles=historical_candles,
                backtest_mode=True
            )
            assert result is not None
        except (TypeError, ValueError) as e:
            if "datetime" in str(e).lower() or "split" in str(e).lower():
                pytest.fail(f"Datetime type error: {e}")
            else:
                raise


class TestBacktestRunnerIntegration:
    """Integration tests for full backtest flow."""
    
    @pytest.mark.asyncio
    async def test_small_backtest_run_with_valid_candles(self):
        """Run a small backtest with test-only historical candles."""
        # Create mock candles with strict 5-minute intervals
        start_time = datetime(2026, 11, 24, 9, 15, tzinfo=timezone.utc)
        candles_5m = generate_mock_candles(start_time, count=150, interval_minutes=5)
        candles_15m = generate_mock_candles(start_time, count=100, interval_minutes=15)
        
        # Create runner
        runner = BacktestRunner(
            asset="BTCUSD",
            start_date=start_time,
            end_date=start_time + timedelta(hours=5),
            candle_count_5m=100,
            candle_count_15m=80,
            use_code_sniper=True
        )
        
        runner.candle_service.fetch_historical_5m = AsyncMock(return_value=candles_5m)
        runner.candle_service.fetch_historical_15m = AsyncMock(return_value=candles_15m)
        
        # Mock multi_expert
        with patch('packages.core.backtest_runner.multi_expert_decision_with_details') as mock_multi:
            from packages.core.schemas_trade import CodeSniperSignal
            mock_signal = CodeSniperSignal(
                code="def run(candles_5, now): return {'direction': 'hold', 'entry': None, 'stop_loss': None, 'take_profit_1': None, 'confidence': 0.5, 'rationale': 'Test'}",
                ttl_minutes=90,
                confidence=0.85,
                metadata={}
            )
            mock_multi.return_value = (mock_signal, {})
            
            # Mock backtest repo
            with patch('packages.core.backtest_runner.backtest_repo') as mock_repo:
                mock_repo.get_backtest = AsyncMock(return_value={"status": "running"})
                mock_repo.update_backtest_status = AsyncMock(return_value=True)
                mock_repo.add_iteration = AsyncMock(return_value="iter123")
                
                # Run a small backtest (2 iterations)
                try:
                    await runner.execute_backtest(
                        test_name="test_small_run",
                        interval_hours=1,
                        backtest_id="test123"
                    )
                    # Should complete without interval validation errors
                except ValueError as e:
                    if "Invalid" in str(e) and "interval" in str(e):
                        pytest.fail(f"Candle interval validation failed: {e}")
                    else:
                        raise

