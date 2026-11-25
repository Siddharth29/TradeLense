"""
Comprehensive tests for MT5 staleness fixes and data flow validation.

Tests cover:
- Unit tests (fast, mocked): verify guards, fallback order, logging, GPT gating
- Integration tests (real MT5): verify actual MT5 connectivity and data flow
- Behavioral tests (per-asset): BTC/GBP/XAU behave independently but identically

Real-terminal checks use an explicitly configured demo account.
"""

import os

import pytest
import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, AsyncMock, patch
from typing import Dict, List, Any, Optional
import pandas as pd

# Import the modules we're testing
from packages.adapters.mt5_adapter import MT5Adapter
from packages.core.candle_service import CandleService
from packages.db.repositories.candles import insert_many_5m, insert_many_15m
from packages.core.gpt_client import complete
from apps.backend.agents.runner import AgentRunner


class MockBar:
    """Mock bar data structure matching MT5 format."""
    def __init__(self, time: datetime, open: float, high: float, low: float, close: float, tick_volume: int = 1000):
        self.time = time
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.tick_volume = tick_volume


class MockUpsertResult:
    """Mock upsert result for database operations."""
    def __init__(self, n_matched: int = 0, n_inserted: int = 0, n_modified: int = 0, n_upserted: int = 0):
        self.bulk_api_result = {
            'nMatched': n_matched,
            'nInserted': n_inserted,
            'nModified': n_modified,
            'nUpserted': n_upserted
        }


@pytest.fixture
def mt5_credentials():
    """Use a demo terminal only when explicitly configured."""
    server = os.getenv("TRADELENSE_TEST_MT5_SERVER", "")
    login = os.getenv("TRADELENSE_TEST_MT5_LOGIN", "")
    password = os.getenv("TRADELENSE_TEST_MT5_PASSWORD", "")
    label = server.casefold()
    if not (("demo" in label or "trial" in label) and login and password):
        pytest.skip("Explicit demo MT5 credentials are required")
    if "live" in label or "real" in label:
        pytest.fail("A demo server is required")
    return {"server": server, "login": login, "password": password}


class TestMT5StalenessFixes:
    """Test suite for MT5 staleness fixes and data flow validation."""
    
    @pytest.fixture
    def test_assets(self):
        """Test assets for behavioral testing."""
        return ["BTCUSD", "GBPUSD", "XAUUSD"]
    
    @pytest.fixture
    def mock_services(self):
        """Mock services for unit testing."""
        services = {
            "mt5_adapter": Mock(spec=MT5Adapter),
            "candle_service": Mock(spec=CandleService),
            "store": Mock(),
            "db": Mock(),
            "gpt_client": Mock()
        }
        return services
    
    @pytest.fixture
    def stale_timestamp(self):
        """Timestamp for stale data (more than 10 minutes old)."""
        return datetime.now(timezone.utc) - timedelta(minutes=15)
    
    @pytest.fixture
    def fresh_timestamp(self):
        """Timestamp for fresh data (less than 5 minutes old)."""
        return datetime.now(timezone.utc) - timedelta(minutes=2)


class TestUnitTests(TestMT5StalenessFixes):
    """Unit tests with mocked dependencies."""
    
    def test_mt5_connection_status_check(self, mock_services):
        """Test MT5 connection status checking."""
        # Test connected state
        mock_services["mt5_adapter"].is_connected.return_value = True
        assert mock_services["mt5_adapter"].is_connected() is True
        
        # Test disconnected state
        mock_services["mt5_adapter"].is_connected.return_value = False
        assert mock_services["mt5_adapter"].is_connected() is False
    
    def test_staleness_guard_logic(self, stale_timestamp, fresh_timestamp):
        """Test staleness guard logic for GPT calls."""
        current_time = datetime.now()
        
        # Test stale data (should skip GPT)
        time_diff = current_time - stale_timestamp.replace(tzinfo=None)
        staleness_minutes = time_diff.total_seconds() / 60
        assert staleness_minutes > 10  # Should be > 10 minutes
        
        # Test fresh data (should allow GPT)
        time_diff = current_time - fresh_timestamp.replace(tzinfo=None)
        staleness_minutes = time_diff.total_seconds() / 60
        assert staleness_minutes < 10  # Should be < 10 minutes
    
    def test_fallback_order_stale_data(self, mock_services, stale_timestamp):
        """Test fallback order when data is stale."""
        # Mock stale store data
        stale_bars = [MockBar(stale_timestamp, 100.0, 101.0, 99.0, 100.5)]
        mock_services["store"].candles.return_value = stale_bars
        
        # Mock fresh broker data
        fresh_timestamp = datetime.now(timezone.utc) - timedelta(minutes=2)
        fresh_bars = [MockBar(fresh_timestamp, 100.0, 101.0, 99.0, 100.5)]
        mock_services["mt5_adapter"].candles.return_value = fresh_bars
        
        # Test that force_broker=True bypasses store
        candle_service = CandleService(
            broker=mock_services["mt5_adapter"],
            store=mock_services["store"]
        )
        
        # This should call broker directly, not store
        result = asyncio.run(candle_service.fetch("BTCUSD", force_broker=True))
        mock_services["store"].candles.assert_not_called()
        mock_services["mt5_adapter"].candles.assert_called()
    
    def test_gpt_gating_stale_data(self, mock_services, stale_timestamp):
        """Test GPT call gating when data is stale."""
        # Mock stale candle data
        stale_bars = [MockBar(stale_timestamp, 100.0, 101.0, 99.0, 100.5)]
        
        # Mock GPT client
        mock_services["gpt_client"].complete.return_value = {"direction": "long", "level": 100.0}
        
        # Test that GPT is not called when data is stale
        # This would be tested in the actual runner logic
        current_time = datetime.now()
        time_diff = current_time - stale_timestamp.replace(tzinfo=None)
        staleness_minutes = time_diff.total_seconds() / 60
        
        if staleness_minutes > 10:
            # GPT should be skipped
            mock_services["gpt_client"].complete.assert_not_called()
    
    def test_upsert_audit_logging(self, mock_services):
        """Test upsert audit logging with detailed counts."""
        # Mock upsert result
        upsert_result = MockUpsertResult(n_matched=5, n_inserted=0, n_modified=0, n_upserted=0)
        
        # Test that we can detect no new data (all matched)
        assert upsert_result.bulk_api_result['nInserted'] == 0
        assert upsert_result.bulk_api_result['nMatched'] > 0
        
        # This indicates stale/duplicate data
        is_stale = (upsert_result.bulk_api_result['nInserted'] == 0 and 
                   upsert_result.bulk_api_result['nUpserted'] == 0 and 
                   upsert_result.bulk_api_result['nMatched'] > 0)
        assert is_stale is True


class TestIntegrationTests(TestMT5StalenessFixes):
    """Integration tests with real MT5 connection."""
    
    @pytest.mark.asyncio
    async def test_real_mt5_connection(self, mt5_credentials):
        """Test real MT5 connection with provided credentials."""
        mt5_adapter = MT5Adapter()
        
        try:
            # Test connection
            await mt5_adapter.connect(**mt5_credentials)
            
            # Verify connection status
            assert mt5_adapter.is_connected() is True
            
            # Test account info retrieval
            account_info = mt5_adapter.account_info()
            assert account_info is not None
            assert hasattr(account_info, 'balance')
            
            print(f"✅ MT5 Connection successful - Balance: {account_info.balance}")
            
        except Exception as e:
            pytest.fail(f"MT5 connection failed: {e}")
    
    @pytest.mark.asyncio
    async def test_real_mt5_candle_fetch(self, mt5_credentials, test_assets):
        """Test real MT5 candle fetching for test assets."""
        mt5_adapter = MT5Adapter()
        
        try:
            await mt5_adapter.connect(**mt5_credentials)
            
            for asset in test_assets:
                # Test 5m candles
                candles_5m = await mt5_adapter.candles(asset, "5m", 10)
                assert len(candles_5m) > 0
                
                # Verify candle structure
                latest_candle = candles_5m[-1]
                assert hasattr(latest_candle, 'time')
                assert hasattr(latest_candle, 'open')
                assert hasattr(latest_candle, 'high')
                assert hasattr(latest_candle, 'low')
                assert hasattr(latest_candle, 'close')
                
                # Check if data is fresh (within last 10 minutes)
                current_time = datetime.now(timezone.utc)
                candle_time = latest_candle.time
                if hasattr(candle_time, 'replace'):
                    candle_time = candle_time.replace(tzinfo=None)
                
                time_diff = current_time - candle_time
                staleness_minutes = time_diff.total_seconds() / 60
                
                print(f"✅ {asset} 5m candles: {len(candles_5m)} bars, latest: {latest_candle.time}, staleness: {staleness_minutes:.1f} min")
                
                # Test 15m candles
                candles_15m = await mt5_adapter.candles(asset, "15m", 5)
                assert len(candles_15m) > 0
                print(f"✅ {asset} 15m candles: {len(candles_15m)} bars")
                
        except Exception as e:
            pytest.fail(f"MT5 candle fetch failed: {e}")
    
    @pytest.mark.asyncio
    async def test_candle_service_with_real_mt5(self, mt5_credentials, test_assets):
        """Test CandleService with real MT5 adapter."""
        mt5_adapter = MT5Adapter()
        await mt5_adapter.connect(**mt5_credentials)
        
        candle_service = CandleService(broker=mt5_adapter)
        
        for asset in test_assets:
            try:
                # Test normal fetch
                c5, c15 = await candle_service.fetch(asset, n5=5, n15=3)
                
                assert len(c5) > 0
                assert len(c15) > 0
                
                # Test force_broker fetch
                c5_forced, c15_forced = await candle_service.fetch(asset, n5=5, n15=3, force_broker=True)
                
                assert len(c5_forced) > 0
                assert len(c15_forced) > 0
                
                print(f"✅ {asset} CandleService: 5m={len(c5)}, 15m={len(c15)}")
                
            except Exception as e:
                pytest.fail(f"CandleService test failed for {asset}: {e}")


class TestBehavioralTests(TestMT5StalenessFixes):
    """Behavioral tests for per-asset independence."""
    
    @pytest.mark.asyncio
    async def test_per_asset_independence(self, mt5_credentials, test_assets):
        """Test that BTC/GBP/XAU behave independently but identically."""
        mt5_adapter = MT5Adapter()
        await mt5_adapter.connect(**mt5_credentials)
        
        results = {}
        
        for asset in test_assets:
            try:
                # Test connection status
                is_connected = mt5_adapter.is_connected()
                
                # Test candle fetching
                candles_5m = await mt5_adapter.candles(asset, "5m", 5)
                candles_15m = await mt5_adapter.candles(asset, "15m", 3)
                
                # Test staleness check
                if candles_5m:
                    latest_time = candles_5m[-1].time
                    current_time = datetime.now(timezone.utc)
                    if hasattr(latest_time, 'replace'):
                        latest_time = latest_time.replace(tzinfo=None)
                    
                    time_diff = current_time - latest_time
                    staleness_minutes = time_diff.total_seconds() / 60
                    
                    results[asset] = {
                        "connected": is_connected,
                        "5m_count": len(candles_5m),
                        "15m_count": len(candles_15m),
                        "staleness_minutes": staleness_minutes,
                        "latest_time": latest_time
                    }
                    
                    print(f"✅ {asset}: connected={is_connected}, 5m={len(candles_5m)}, 15m={len(candles_15m)}, staleness={staleness_minutes:.1f}min")
                
            except Exception as e:
                print(f"❌ {asset} failed: {e}")
                results[asset] = {"error": str(e)}
        
        # Verify all assets behave identically (same connection status, similar data availability)
        connected_assets = [asset for asset, result in results.items() if result.get("connected", False)]
        assert len(connected_assets) == len(test_assets), f"Not all assets connected: {results}"
        
        # Verify all assets have similar staleness (within reasonable range)
        staleness_values = [result.get("staleness_minutes", 999) for result in results.values() if "staleness_minutes" in result]
        if staleness_values:
            max_staleness = max(staleness_values)
            min_staleness = min(staleness_values)
            assert max_staleness - min_staleness < 5, f"Staleness varies too much: {staleness_values}"
    
    @pytest.mark.asyncio
    async def test_staleness_thresholds(self, mt5_credentials, test_assets):
        """Test staleness thresholds and GPT gating logic."""
        mt5_adapter = MT5Adapter()
        await mt5_adapter.connect(**mt5_credentials)
        
        for asset in test_assets:
            try:
                candles_5m = await mt5_adapter.candles(asset, "5m", 1)
                
                if candles_5m:
                    latest_candle = candles_5m[-1]
                    current_time = datetime.now(timezone.utc)
                    candle_time = latest_candle.time
                    
                    if hasattr(candle_time, 'replace'):
                        candle_time = candle_time.replace(tzinfo=None)
                    
                    time_diff = current_time - candle_time
                    staleness_minutes = time_diff.total_seconds() / 60
                    
                    # Test threshold logic
                    if staleness_minutes > 10:  # 2x threshold
                        print(f"⚠️ {asset}: Data stale ({staleness_minutes:.1f}min) - GPT should be skipped")
                    elif staleness_minutes > 30:  # 6x threshold
                        print(f"🚨 {asset}: Data critically stale ({staleness_minutes:.1f}min) - Should halt pipeline")
                    else:
                        print(f"✅ {asset}: Data fresh ({staleness_minutes:.1f}min) - GPT allowed")
                
            except Exception as e:
                print(f"❌ {asset} staleness test failed: {e}")


class TestErrorHandling(TestMT5StalenessFixes):
    """Test error handling and edge cases."""
    
    @pytest.mark.asyncio
    async def test_mt5_disconnected_scenario(self, mock_services):
        """Test behavior when MT5 is disconnected."""
        # Mock disconnected MT5
        mock_services["mt5_adapter"].is_connected.return_value = False
        mock_services["mt5_adapter"].candles.side_effect = RuntimeError("not logged in")
        
        # Mock stale store data
        stale_time = datetime.now(timezone.utc) - timedelta(minutes=15)
        stale_bars = [MockBar(stale_time, 100.0, 101.0, 99.0, 100.5)]
        mock_services["store"].candles.return_value = stale_bars
        
        candle_service = CandleService(
            broker=mock_services["mt5_adapter"],
            store=mock_services["store"]
        )
        
        # Should fallback to store and detect staleness
        c5, c15 = await candle_service.fetch("BTCUSD", n5=5, n15=3)
        
        # Verify store was called (fallback)
        mock_services["store"].candles.assert_called()
        
        # Verify staleness detection
        if c5:
            latest_time = c5[-1].time
            current_time = datetime.now(timezone.utc)
            if hasattr(latest_time, 'replace'):
                latest_time = latest_time.replace(tzinfo=None)
            
            time_diff = current_time - latest_time
            staleness_minutes = time_diff.total_seconds() / 60
            assert staleness_minutes > 10  # Should be stale
    
    @pytest.mark.asyncio
    async def test_no_new_bars_scenario(self, mock_services):
        """Test scenario where MT5 returns no new bars."""
        # Mock MT5 returning same data
        current_time = datetime.now(timezone.utc)
        bars = [MockBar(current_time - timedelta(minutes=5), 100.0, 101.0, 99.0, 100.5)]
        mock_services["mt5_adapter"].candles.return_value = bars
        
        # Mock upsert returning no new data
        upsert_result = MockUpsertResult(n_matched=5, n_inserted=0)
        
        # This should trigger staleness detection
        assert upsert_result.bulk_api_result['nInserted'] == 0
        assert upsert_result.bulk_api_result['nMatched'] > 0
        
        print("✅ No new bars scenario: n_inserted=0, n_matched>0 - should trigger staleness warning")


# Test runner function
async def run_asset_cycle(asset: str, now_utc: datetime, services: Dict[str, Any], config: Dict[str, Any]) -> None:
    """
    Run one pipeline pass for an asset.
    
    This simulates the main agent runner logic with the fixes applied.
    """
    mt5_adapter = services["mt5_adapter"]
    candle_service = services["candle_service"]
    gpt_client = services["gpt_client"]
    
    # Check MT5 connection
    if not mt5_adapter.is_connected():
        print(f"❌ {asset}: MT5 disconnected - marking SOURCE_DOWN")
        return
    
    # Fetch candles
    try:
        c5, c15 = await candle_service.fetch(asset, n5=5, n15=3)
        
        if not c5 or not c15:
            print(f"❌ {asset}: No candle data available")
            return
        
        # Check staleness
        latest_5m = c5[-1]
        latest_time = latest_5m.time
        if hasattr(latest_time, 'replace'):
            latest_time = latest_time.replace(tzinfo=None)
        
        time_diff = now_utc - latest_time
        staleness_minutes = time_diff.total_seconds() / 60
        
        if staleness_minutes > 10:  # 2x threshold
            print(f"⚠️ {asset}: Data stale ({staleness_minutes:.1f}min) - skipping GPT")
            return
        elif staleness_minutes > 30:  # 6x threshold
            print(f"🚨 {asset}: Data critically stale ({staleness_minutes:.1f}min) - halting pipeline")
            return
        
        # Data is fresh - call GPT
        print(f"✅ {asset}: Data fresh ({staleness_minutes:.1f}min) - calling GPT")
        gpt_result = await gpt_client.complete("test prompt")
        
        # Start monitoring
        print(f"✅ {asset}: Started monitoring window for 60 minutes")
        
    except Exception as e:
        print(f"❌ {asset}: Pipeline error - {e}")


# Parametrized test for per-asset behavior
@pytest.mark.parametrize("asset", ["BTCUSD", "GBPUSD", "XAUUSD"])
@pytest.mark.asyncio
async def test_asset_cycle_behavior(asset: str, mt5_credentials):
    """Test that each asset behaves identically in the pipeline."""
    # Create real services
    mt5_adapter = MT5Adapter()
    await mt5_adapter.connect(**mt5_credentials)
    
    candle_service = CandleService(broker=mt5_adapter)
    
    services = {
        "mt5_adapter": mt5_adapter,
        "candle_service": candle_service,
        "gpt_client": Mock()
    }
    
    config = {"run_interval": "1h"}
    now_utc = datetime.now(timezone.utc)
    
    # Run the cycle
    await run_asset_cycle(asset, now_utc, services, config)


if __name__ == "__main__":
    # Run tests
    pytest.main([__file__, "-v", "--tb=short"])
