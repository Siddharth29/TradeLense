"""Tests for candle market gap handling:
- BUG-3: XAUUSD daily maintenance window should be recognized as expected gap
- BUG-4: 15m candle gaps during market close should not fail validation
"""
import pytest
import pandas as pd

from core.candles import is_expected_market_gap, _24_7_ASSETS


class TestAssetClassification:
    def test_crypto_in_24_7(self):
        assert "BTCUSD" in _24_7_ASSETS
        assert "ETHUSD" in _24_7_ASSETS

    def test_xauusd_not_in_24_7(self):
        assert "XAUUSD" not in _24_7_ASSETS

    def test_forex_not_in_24_7(self):
        assert "GBPUSD" not in _24_7_ASSETS
        assert "EURUSD" not in _24_7_ASSETS


class TestDailyMaintenanceGap:
    def test_xauusd_daily_break_20_to_22_utc(self):
        prev = pd.Timestamp("2024-06-15 20:55:00", tz="UTC")
        curr = pd.Timestamp("2024-06-15 22:05:00", tz="UTC")
        assert is_expected_market_gap(prev, curr, asset="XAUUSD") is True

    def test_gbpusd_daily_break(self):
        prev = pd.Timestamp("2024-06-15 20:55:00", tz="UTC")
        curr = pd.Timestamp("2024-06-15 22:05:00", tz="UTC")
        assert is_expected_market_gap(prev, curr, asset="GBPUSD") is True

    def test_btcusd_daily_maintenance_break(self):
        """BTCUSD has a broker daily maintenance window (~20:55-22:05 UTC)."""
        prev = pd.Timestamp("2024-06-15 20:55:00", tz="UTC")
        curr = pd.Timestamp("2024-06-15 22:05:00", tz="UTC")
        assert is_expected_market_gap(prev, curr, asset="BTCUSD") is True

    def test_xauusd_non_break_gap_is_not_expected(self):
        prev = pd.Timestamp("2024-06-15 14:00:00", tz="UTC")
        curr = pd.Timestamp("2024-06-15 16:00:00", tz="UTC")
        assert is_expected_market_gap(prev, curr, asset="XAUUSD") is False

    def test_xagusd_daily_break(self):
        prev = pd.Timestamp("2024-06-15 21:00:00", tz="UTC")
        curr = pd.Timestamp("2024-06-15 22:00:00", tz="UTC")
        assert is_expected_market_gap(prev, curr, asset="XAGUSD") is True


class TestWeekendGap:
    def test_forex_weekend_gap(self):
        prev = pd.Timestamp("2024-06-14 21:00:00", tz="UTC")  # Friday
        curr = pd.Timestamp("2024-06-16 21:00:00", tz="UTC")  # Sunday
        assert is_expected_market_gap(prev, curr, asset="GBPUSD") is True

    def test_xauusd_weekend_gap(self):
        prev = pd.Timestamp("2024-06-14 21:00:00", tz="UTC")
        curr = pd.Timestamp("2024-06-16 21:00:00", tz="UTC")
        assert is_expected_market_gap(prev, curr, asset="XAUUSD") is True

    def test_crypto_no_weekend_gap(self):
        prev = pd.Timestamp("2024-06-14 21:00:00", tz="UTC")
        curr = pd.Timestamp("2024-06-16 21:00:00", tz="UTC")
        assert is_expected_market_gap(prev, curr, asset="BTCUSD") is False

    def test_weekend_is_checked_in_utc(self):
        prev = pd.Timestamp("2026-11-14 03:25:00", tz="Asia/Kolkata")
        curr = pd.Timestamp("2026-11-16 03:35:00", tz="Asia/Kolkata")
        assert is_expected_market_gap(prev, curr, asset="GBPUSD") is True

    def test_weekday_data_outage_is_not_a_scheduled_gap(self):
        prev = pd.Timestamp("2026-11-10 14:00:00", tz="UTC")
        curr = pd.Timestamp("2026-11-11 14:00:00", tz="UTC")
        assert is_expected_market_gap(prev, curr, asset="GBPUSD") is False


class TestNoneAsset:
    def test_none_asset_assumes_weekend_closure(self):
        prev = pd.Timestamp("2024-06-14 21:00:00", tz="UTC")
        curr = pd.Timestamp("2024-06-16 21:00:00", tz="UTC")
        assert is_expected_market_gap(prev, curr, asset=None) is True

    def test_none_asset_daily_break(self):
        prev = pd.Timestamp("2024-06-15 20:55:00", tz="UTC")
        curr = pd.Timestamp("2024-06-15 22:05:00", tz="UTC")
        assert is_expected_market_gap(prev, curr, asset=None) is True
