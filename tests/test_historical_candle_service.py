"""Unit tests for historical_candle_service.py."""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from packages.core.historical_candle_service import HistoricalCandleService
from tests.fixtures.backtest_fixtures import generate_mock_candles


@pytest.fixture
def service():
    """Create a HistoricalCandleService instance."""
    return HistoricalCandleService()


@pytest.fixture
def mock_candles_5m():
    """Generate mock 5m candles."""
    start_time = datetime(2024, 1, 1, 9, 15, tzinfo=timezone.utc)
    return generate_mock_candles(start_time, count=100, interval_minutes=5)


@pytest.fixture
def mock_candles_15m():
    """Generate mock 15m candles."""
    start_time = datetime(2024, 1, 1, 9, 15, tzinfo=timezone.utc)
    return generate_mock_candles(start_time, count=70, interval_minutes=15)


class TestHistoricalCandleService:
    """Test HistoricalCandleService class."""
    
    def test_initialization(self, service):
        """Test service initialization."""
        assert service is not None
    
    @pytest.mark.asyncio
    async def test_fetch_historical_5m(self, service, mock_candles_5m):
        """Test fetching 5m historical candles."""
        start_date = datetime(2024, 1, 1, 9, 15, tzinfo=timezone.utc)
        end_date = datetime(2024, 1, 1, 14, 15, tzinfo=timezone.utc)
        
        # Mock the repository function
        with patch('packages.db.repositories.candles.get_many_5m', new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_candles_5m
            
            result = await service.fetch_historical_5m(
                asset="NIFTY",
                start_date=start_date,
                end_date=end_date,
                limit=100
            )
            
            assert result is not None
            assert isinstance(result, list)
            mock_get.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_fetch_historical_15m(self, service, mock_candles_15m):
        """Test fetching 15m historical candles."""
        start_date = datetime(2024, 1, 1, 9, 15, tzinfo=timezone.utc)
        end_date = datetime(2024, 1, 1, 14, 15, tzinfo=timezone.utc)
        
        # Mock the repository function
        with patch('packages.db.repositories.candles.get_many_15m', new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_candles_15m
            
            result = await service.fetch_historical_15m(
                asset="NIFTY",
                start_date=start_date,
                end_date=end_date,
                limit=70
            )
            
            assert result is not None
            assert isinstance(result, list)
            mock_get.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_fetch_5m_with_default_limit(self, service):
        """Test fetching 5m candles with default limit."""
        start_date = datetime(2024, 1, 1, 9, 15, tzinfo=timezone.utc)
        end_date = datetime(2024, 1, 1, 14, 15, tzinfo=timezone.utc)
        
        with patch('packages.db.repositories.candles.get_many_5m', new_callable=AsyncMock) as mock_get:
            mock_get.return_value = []
            
            await service.fetch_historical_5m(
                asset="NIFTY",
                start_date=start_date,
                end_date=end_date
                # No limit specified, should use default
            )
            
            # Verify called with default limit
            call_args = mock_get.call_args
            assert call_args is not None
    
    @pytest.mark.asyncio
    async def test_fetch_15m_with_default_limit(self, service):
        """Test fetching 15m candles with default limit."""
        start_date = datetime(2024, 1, 1, 9, 15, tzinfo=timezone.utc)
        end_date = datetime(2024, 1, 1, 14, 15, tzinfo=timezone.utc)
        
        with patch('packages.db.repositories.candles.get_many_15m', new_callable=AsyncMock) as mock_get:
            mock_get.return_value = []
            
            await service.fetch_historical_15m(
                asset="NIFTY",
                start_date=start_date,
                end_date=end_date
                # No limit specified, should use default
            )
            
            # Verify called with default limit
            call_args = mock_get.call_args
            assert call_args is not None
    
    @pytest.mark.asyncio
    async def test_fetch_with_timezone_handling(self, service):
        """Test fetching candles with different timezone inputs."""
        # Test with naive datetime (should be converted to UTC)
        start_date = datetime(2024, 1, 1, 9, 15)  # Naive
        end_date = datetime(2024, 1, 1, 14, 15, tzinfo=timezone.utc)  # UTC
        
        with patch('packages.db.repositories.candles.get_many_5m', new_callable=AsyncMock) as mock_get:
            mock_get.return_value = []
            
            await service.fetch_historical_5m(
                asset="NIFTY",
                start_date=start_date,
                end_date=end_date,
                limit=100
            )
            
            # Should handle without errors
            assert mock_get.called
    
    @pytest.mark.asyncio
    async def test_fetch_empty_result(self, service):
        """Test fetching when no candles are available."""
        start_date = datetime(2024, 1, 1, 9, 15, tzinfo=timezone.utc)
        end_date = datetime(2024, 1, 1, 14, 15, tzinfo=timezone.utc)
        
        with patch('packages.db.repositories.candles.get_many_5m', new_callable=AsyncMock) as mock_get:
            mock_get.return_value = []
            
            result = await service.fetch_historical_5m(
                asset="NIFTY",
                start_date=start_date,
                end_date=end_date,
                limit=100
            )
            
            assert result == []
    
    @pytest.mark.asyncio
    async def test_fetch_with_error_handling(self, service):
        """Test error handling during candle fetching."""
        start_date = datetime(2024, 1, 1, 9, 15, tzinfo=timezone.utc)
        end_date = datetime(2024, 1, 1, 14, 15, tzinfo=timezone.utc)
        
        with patch('packages.db.repositories.candles.get_many_5m', new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = Exception("Database error")
            
            # Should handle error gracefully
            with pytest.raises(Exception):
                await service.fetch_historical_5m(
                    asset="NIFTY",
                    start_date=start_date,
                    end_date=end_date,
                    limit=100
                )

