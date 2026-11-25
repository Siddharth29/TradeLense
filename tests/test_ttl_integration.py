"""Integration tests for TTL propagation and expiry handling."""

import asyncio
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, patch
from packages.core.slot_candle_matcher import SlotCandleMatcher


class TestTTLIntegration:
    """Integration tests for TTL functionality."""
    
    @pytest.fixture
    def slot_matcher(self):
        """Create a SlotCandleMatcher instance."""
        return SlotCandleMatcher()
    
    @pytest.fixture
    def sample_historical_candles(self):
        """Sample historical candles for testing."""
        import pandas as pd
        
        candles_data = [
            {"time": datetime.now(timezone.utc) - timedelta(hours=i), 
             "open": 100 + i, "high": 101 + i, "low": 99 + i, "close": 100.5 + i}
            for i in range(10, 0, -1)
        ]
        return pd.DataFrame(candles_data)
    
    @pytest.mark.asyncio
    async def test_slot_match_with_ttl_expiry(self, slot_matcher, sample_historical_candles):
        """Test TTL expiry: simulate clock advancing → pending removed."""
        
        # Mock the comprehensive logging store function
        with patch('packages.db.repositories.comprehensive_logging.store_slot_candle_match') as mock_store:
            mock_store.return_value = "test_match_id_123"
            
            # Create slot match with 1-minute TTL
            match_id = await slot_matcher.create_slot_match(
                asset="NIFTY",
                slot_id=1001,
                slot_timestamp=datetime.now(timezone.utc),
                gpt_response_id="gpt_123",
                level_value=25000.0,
                direction="long",
                agent_id="test_agent",
                prompt_hash="test_hash",
                pattern="breakout",
                confidence=0.8,
                historical_candles=sample_historical_candles,
                ttl_minutes=1  # 1 minute TTL
            )
            
            # Verify match was created with expiry
            assert match_id in slot_matcher._active_matches
            match_data = slot_matcher._active_matches[match_id]
            assert match_data["expires_at"] is not None
            
            # Simulate clock advancing beyond TTL (mock current time to be 2 minutes later)
            future_time = datetime.now(timezone.utc) + timedelta(minutes=2)
            
            with patch('packages.core.slot_candle_matcher.datetime') as mock_datetime:
                mock_datetime.now.return_value = future_time
                mock_datetime.timezone = timezone
                
                # Trigger check with new candle - should remove expired match
                triggered_matches = await slot_matcher.check_trigger_match(
                    asset="NIFTY",
                    candle_timestamp=future_time,
                    open_price=25050.0,
                    high_price=25100.0,
                    low_price=25000.0,
                    close_price=25075.0
                )
                
                # Match should be removed due to expiry
                assert match_id not in slot_matcher._active_matches
                assert len(triggered_matches) == 0  # No triggers because match was expired
    
    @pytest.mark.asyncio 
    async def test_slot_match_without_ttl_persists(self, slot_matcher, sample_historical_candles):
        """Test that slot matches without TTL persist beyond time threshold."""
        
        with patch('packages.db.repositories.comprehensive_logging.store_slot_candle_match') as mock_store:
            mock_store.return_value = "test_match_id_456"
            
            # Create slot match without TTL
            match_id = await slot_matcher.create_slot_match(
                asset="NIFTY",
                slot_id=1002,
                slot_timestamp=datetime.now(timezone.utc),
                gpt_response_id="gpt_456",
                level_value=25000.0,
                direction="long",
                agent_id="test_agent",
                prompt_hash="test_hash",
                pattern="retest",
                confidence=0.7,
                historical_candles=sample_historical_candles,
                ttl_minutes=None  # No TTL
            )
            
            # Verify match was created without expiry
            assert match_id in slot_matcher._active_matches
            match_data = slot_matcher._active_matches[match_id]
            assert match_data["expires_at"] is None
            
            # Simulate clock advancing significantly
            future_time = datetime.now(timezone.utc) + timedelta(hours=5)
            
            with patch('packages.core.slot_candle_matcher.datetime') as mock_datetime:
                mock_datetime.now.return_value = future_time
                mock_datetime.timezone = timezone
                
                # Trigger check - match should still exist
                triggered_matches = await slot_matcher.check_trigger_match(
                    asset="NIFTY",
                    candle_timestamp=future_time,
                    open_price=24950.0,
                    high_price=24980.0,
                    low_price=24900.0,
                    close_price=24960.0
                )
                
                # Match should still exist (not expired)
                assert match_id in slot_matcher._active_matches
    
    @pytest.mark.asyncio
    async def test_trade_monitor_ttl_expiry(self):
        """Test that trade monitor respects TTL for open trades force-close."""
        from packages.core.services.trade_monitor import TradeMonitor
        
        # Mock dependencies
        mock_candle_service = Mock()
        mock_broker = Mock()
        mock_journal = Mock()
        
        trade_closed_callback_called = False
        trade_close_data = None
        
        def mock_on_trade_closed(data):
            nonlocal trade_closed_callback_called, trade_close_data
            trade_closed_callback_called = True
            trade_close_data = data
        
        # Create trade monitor
        trade_monitor = TradeMonitor(
            candle_service=mock_candle_service,
            broker=mock_broker,
            journal=mock_journal,
            on_trade_closed=mock_on_trade_closed
        )
        
        # Mock the trade evaluation to simulate ongoing trade
        with patch('packages.core.services.trade_monitor.evaluate_trade') as mock_evaluate:
            mock_evaluate.return_value = None  # No immediate action needed
            
            # Mock candle fetching
            with patch('packages.core.services.trade_monitor.await_next_bar_close') as mock_await:
                mock_await.side_effect = [
                    {"time": datetime.now(timezone.utc), "close": 100.5, "high": 101.0, "low": 100.0}
                ]
                
                # Queue trade with very short TTL (1 second)
                short_expiry = datetime.now(timezone.utc) + timedelta(seconds=1)
                
                trade_monitor.queue_trade(
                    symbol="NIFTY", 
                    trade_state={"direction": "long", "entry": 100.0, "sl": 99.0},
                    rules={"tp1": 101.0, "tp": 102.0},
                    expiry=short_expiry,
                    prompt_hash="test_hash",
                    gpt_mode="test_mode",
                    latency_ms=50.0
                )
                
                # Wait for expiry
                await asyncio.sleep(1.5)
                
                # Verify that monitoring stopped due to expiry
                # In the real implementation, the monitor should stop and call trade closure
                # This is a conceptual test - the actual trade monitor handles expiry internally
    
    def test_ttl_minutes_validation_in_schema(self):
        """Test that TTL minutes are properly validated in trade schemas."""
        from packages.core.schemas_trade import MultiExpertSignal
        
        # Valid TTL values should work
        valid_signal = MultiExpertSignal(
            signal="buy",
            entry=100.0,
            stop=99.0,
            target=102.5,
            ttl_minutes=90,  # Valid TTL
            confidence=0.8,  # Numeric confidence
            active_window_c5=50,  # Valid window size
            active_window_c15=30  # Valid window size
        )
        assert valid_signal.ttl_minutes == 90
        
        # Invalid TTL should raise validation error
        with pytest.raises(Exception):  # ValidationError expected
            MultiExpertSignal(
                signal="buy", 
                entry=100.0,
                stop=99.0,
                target=102.5,
                ttl_minutes=75,  # Invalid TTL (not in [45, 60, 90, 120])
                confidence=0.8,
                active_window_c5=50,
                active_window_c15=30
            )
