import pytest
from datetime import datetime, timezone, timedelta
import pandas as pd

from packages.core.candles import validate_candle_integrity, Candle


class TestCandleIntegrity:
    """Test the candle integrity validation functionality."""

    def test_validate_candle_integrity_complete_series(self):
        """Test that a complete 5-minute candle series passes validation."""
        # Create a complete series of 5-minute candles
        base_time = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
        candles = []
        for i in range(5):
            candle_time = base_time + timedelta(minutes=5 * i)
            candles.append(Candle(
                time=pd.Timestamp(candle_time),
                open=100.0 + i,
                high=101.0 + i,
                low=99.0 + i,
                close=100.5 + i,
                volume=1000 + i * 100
            ))
        
        is_valid, error_msg = validate_candle_integrity(candles, "5m", 5, check_recency=False)
        assert is_valid is True
        assert error_msg == ""

    def test_validate_candle_integrity_missing_candles(self):
        """Test that a series with missing candles fails validation."""
        # Create a series with a gap (missing 10:10 candle)
        base_time = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
        candles = []
        times = [0, 5, 15, 20]  # Skip 10 minutes (10:10)
        for i in times:
            candle_time = base_time + timedelta(minutes=i)
            candles.append(Candle(
                time=pd.Timestamp(candle_time),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.5,
                volume=1000
            ))
        
        is_valid, error_msg = validate_candle_integrity(candles, "5m", 4, check_recency=False)
        assert is_valid is False
        assert "Missing candles at:" in error_msg
        assert "2024-01-01 10:10:00+00:00" in error_msg

    def test_validate_candle_integrity_wrong_count(self):
        """Test that wrong candle count fails validation."""
        base_time = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
        candles = []
        for i in range(3):  # Only 3 candles
            candle_time = base_time + timedelta(minutes=5 * i)
            candles.append(Candle(
                time=pd.Timestamp(candle_time),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.5,
                volume=1000
            ))
        
        # Shortfall 2 with expected 5 is INSIDE the deliberate timing
        # tolerance (max(3, 15%)): valid with a warning.
        is_valid, error_msg = validate_candle_integrity(candles, "5m", 5, check_recency=False)
        assert is_valid is True
        assert "WARNING" in error_msg

        # Shortfall beyond the tolerance is a hard failure.
        is_valid, error_msg = validate_candle_integrity(candles, "5m", 10, check_recency=False)
        assert is_valid is False
        assert "Expected 10 candles, got 3" in error_msg

    def test_validate_candle_integrity_old_candles(self):
        """Test that very old candles fail validation."""
        # Create candles that are too old (more than 2 intervals old)
        # Recency tolerance for 5m data is 60 minutes — go well past it.
        old_time = datetime.now(timezone.utc) - timedelta(hours=3)
        candles = []
        for i in range(3):
            candle_time = old_time + timedelta(minutes=5 * i)
            candles.append(Candle(
                time=pd.Timestamp(candle_time),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.5,
                volume=1000
            ))
        
        is_valid, error_msg = validate_candle_integrity(candles, "5m", 3)
        assert is_valid is False
        assert "Latest candle is too old:" in error_msg

    def test_validate_candle_integrity_empty_list(self):
        """Test that empty candle list passes validation."""
        is_valid, error_msg = validate_candle_integrity([], "5m", 0)
        assert is_valid is True
        assert error_msg == ""

    def test_validate_candle_integrity_15m_interval(self):
        """Test validation with 15-minute intervals."""
        base_time = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
        candles = []
        for i in range(3):
            candle_time = base_time + timedelta(minutes=15 * i)
            candles.append(Candle(
                time=pd.Timestamp(candle_time),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.5,
                volume=1000
            ))
        
        is_valid, error_msg = validate_candle_integrity(candles, "15m", 3, check_recency=False)
        assert is_valid is True
        assert error_msg == ""

    def test_validate_candle_integrity_recent_candles(self):
        """Test that recent candles (within 2 intervals) pass validation."""
        # Create candles that are recent (within 10 minutes for 5m interval)
        recent_time = datetime.now(timezone.utc) - timedelta(minutes=5)
        candles = []
        for i in range(3):
            candle_time = recent_time + timedelta(minutes=5 * i)
            candles.append(Candle(
                time=pd.Timestamp(candle_time),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.5,
                volume=1000
            ))
        
        is_valid, error_msg = validate_candle_integrity(candles, "5m", 3)
        assert is_valid is True
        assert error_msg == ""
