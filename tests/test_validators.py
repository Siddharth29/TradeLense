"""Unit tests for trade validators using synthetic data."""

import pytest

from packages.core.validators import median_range_5m, rr, inside_window, anti_scalp


class TestMedianRange5m:
    """Test median range calculation for 5-minute candles."""
    
    def test_single_candle(self):
        """Test median range with single candle."""
        candles = [{"high": 105.0, "low": 100.0}]
        result = median_range_5m(candles)
        assert result == 5.0
    
    def test_multiple_candles_odd_count(self):
        """Test median range with odd number of candles."""
        candles = [
            {"high": 105.0, "low": 100.0},  # range: 5.0
            {"high": 110.0, "low": 107.0},  # range: 3.0
            {"high": 115.0, "low": 109.0},  # range: 6.0
        ]
        result = median_range_5m(candles)
        assert result == 5.0  # median of [5.0, 3.0, 6.0]
    
    def test_multiple_candles_even_count(self):
        """Test median range with even number of candles."""
        candles = [
            {"high": 105.0, "low": 100.0},  # range: 5.0
            {"high": 110.0, "low": 107.0},  # range: 3.0
            {"high": 115.0, "low": 109.0},  # range: 6.0
            {"high": 108.0, "low": 104.0},  # range: 4.0
        ]
        result = median_range_5m(candles)
        assert result == 4.5  # median of [5.0, 3.0, 6.0, 4.0] = (4.0 + 5.0) / 2
    
    def test_empty_window_raises_error(self):
        """Test that empty window raises ValueError."""
        with pytest.raises(ValueError, match="c5_window cannot be empty"):
            median_range_5m([])
    
    def test_missing_high_field_raises_error(self):
        """Test that missing 'high' field raises ValueError."""
        candles = [{"low": 100.0}]  # Missing 'high'
        with pytest.raises(ValueError, match="Candle missing required 'high' or 'low' field"):
            median_range_5m(candles)
    
    def test_missing_low_field_raises_error(self):
        """Test that missing 'low' field raises ValueError."""
        candles = [{"high": 105.0}]  # Missing 'low'
        with pytest.raises(ValueError, match="Candle missing required 'high' or 'low' field"):
            median_range_5m(candles)


class TestRiskReward:
    """Test risk-reward ratio calculation."""
    
    def test_valid_buy_trade(self):
        """Test valid buy trade risk-reward calculation."""
        # Buy: entry=100, stop=95, target=110
        # Risk = 100-95 = 5, Reward = 110-100 = 10, RR = 10/5 = 2.0
        result = rr(entry=100.0, stop=95.0, target=110.0, side="buy")
        assert result == 2.0
    
    def test_valid_sell_trade(self):
        """Test valid sell trade risk-reward calculation."""
        # Sell: entry=100, stop=105, target=90
        # Risk = 105-100 = 5, Reward = 100-90 = 10, RR = 10/5 = 2.0
        result = rr(entry=100.0, stop=105.0, target=90.0, side="sell")
        assert result == 2.0
    
    def test_fractional_rr(self):
        """Test fractional risk-reward ratio."""
        # Buy: entry=100, stop=95, target=102.5
        # Risk = 5, Reward = 2.5, RR = 0.5
        result = rr(entry=100.0, stop=95.0, target=102.5, side="buy")
        assert result == 0.5
    
    def test_invalid_side_raises_error(self):
        """Test that invalid side raises ValueError."""
        with pytest.raises(ValueError, match="Invalid side 'hold'"):
            rr(entry=100.0, stop=95.0, target=110.0, side="hold")
    
    def test_buy_invalid_stop_above_entry(self):
        """Test that buy trade with stop above entry raises error."""
        with pytest.raises(ValueError, match="For buy trades, stop must be below entry"):
            rr(entry=100.0, stop=105.0, target=110.0, side="buy")
    
    def test_buy_invalid_target_below_entry(self):
        """Test that buy trade with target below entry raises error."""
        with pytest.raises(ValueError, match="For buy trades, target must be above entry"):
            rr(entry=100.0, stop=95.0, target=90.0, side="buy")
    
    def test_sell_invalid_stop_below_entry(self):
        """Test that sell trade with stop below entry raises error."""
        with pytest.raises(ValueError, match="For sell trades, stop must be above entry"):
            rr(entry=100.0, stop=95.0, target=90.0, side="sell")
    
    def test_sell_invalid_target_above_entry(self):
        """Test that sell trade with target above entry raises error."""
        with pytest.raises(ValueError, match="For sell trades, target must be below entry"):
            rr(entry=100.0, stop=105.0, target=110.0, side="sell")


class TestInsideWindow:
    """Test whether trade levels are within price action windows."""
    
    def test_levels_inside_window(self):
        """Test trade levels within price range."""
        c5_window = [
            {"high": 105.0, "low": 100.0},
            {"high": 108.0, "low": 102.0},
        ]
        c15_window = [
            {"high": 110.0, "low": 98.0},
        ]
        # Combined range: 98.0 to 110.0
        # Trade levels: entry=103, stop=101, target=107 (all within range)
        result = inside_window(
            entry=103.0, stop=101.0, target=107.0,
            c5_window=c5_window, c15_window=c15_window
        )
        assert result is True
    
    def test_levels_outside_window(self):
        """Test trade levels outside price range."""
        c5_window = [
            {"high": 105.0, "low": 100.0},
        ]
        c15_window = [
            {"high": 107.0, "low": 102.0},
        ]
        # Combined range: 100.0 to 107.0
        # Trade levels: entry=103, stop=98, target=110 (stop and target outside)
        result = inside_window(
            entry=103.0, stop=98.0, target=110.0,
            c5_window=c5_window, c15_window=c15_window
        )
        assert result is False
    
    def test_partial_outside_window(self):
        """Test when some levels are outside window."""
        c5_window = [{"high": 105.0, "low": 100.0}]
        c15_window = [{"high": 103.0, "low": 98.0}]
        # Combined range: 98.0 to 105.0
        # Trade levels: entry=102, stop=99, target=107 (target outside)
        result = inside_window(
            entry=102.0, stop=99.0, target=107.0,
            c5_window=c5_window, c15_window=c15_window
        )
        assert result is False
    
    def test_empty_c5_window_raises_error(self):
        """Test that empty c5_window raises ValueError."""
        c15_window = [{"high": 105.0, "low": 100.0}]
        with pytest.raises(ValueError, match="Both c5_window and c15_window must be non-empty"):
            inside_window(
                entry=102.0, stop=99.0, target=107.0,
                c5_window=[], c15_window=c15_window
            )
    
    def test_empty_c15_window_raises_error(self):
        """Test that empty c15_window raises ValueError."""
        c5_window = [{"high": 105.0, "low": 100.0}]
        with pytest.raises(ValueError, match="Both c5_window and c15_window must be non-empty"):
            inside_window(
                entry=102.0, stop=99.0, target=107.0,
                c5_window=c5_window, c15_window=[]
            )
    
    def test_missing_candle_fields_raises_error(self):
        """Test that candles missing high/low fields raise error."""
        c5_window = [{"high": 105.0}]  # Missing 'low'
        c15_window = [{"high": 107.0, "low": 100.0}]
        with pytest.raises(ValueError, match="Candle missing required 'high' or 'low' field"):
            inside_window(
                entry=102.0, stop=99.0, target=107.0,
                c5_window=c5_window, c15_window=c15_window
            )


class TestAntiScalp:
    """Test anti-scalping validator."""
    
    def test_buy_trade_passes_anti_scalp(self):
        """Test buy trade that passes anti-scalp check."""
        c5_window = [
            {"high": 105.0, "low": 100.0},  # range: 5.0
            {"high": 107.0, "low": 102.0},  # range: 5.0
            {"high": 109.0, "low": 104.0},  # range: 5.0
        ]
        # Median range = 5.0, k=6.0, min_distance = 5.0 * 6.0 = 30.0
        # Buy: entry=100, target=135, distance = 135-100 = 35.0 >= 30.0
        result = anti_scalp(
            entry=100.0, target=135.0, side="buy", 
            c5_window=c5_window, k=6.0
        )
        assert result is True
    
    def test_buy_trade_fails_anti_scalp(self):
        """Test buy trade that fails anti-scalp check."""
        c5_window = [
            {"high": 105.0, "low": 100.0},  # range: 5.0
            {"high": 107.0, "low": 102.0},  # range: 5.0
        ]
        # Median range = 5.0, k=6.0, min_distance = 30.0
        # Buy: entry=100, target=120, distance = 20.0 < 30.0
        result = anti_scalp(
            entry=100.0, target=120.0, side="buy",
            c5_window=c5_window, k=6.0
        )
        assert result is False
    
    def test_sell_trade_passes_anti_scalp(self):
        """Test sell trade that passes anti-scalp check."""
        c5_window = [
            {"high": 110.0, "low": 105.0},  # range: 5.0
        ]
        # Median range = 5.0, k=4.0, min_distance = 20.0
        # Sell: entry=100, target=75, distance = 100-75 = 25.0 >= 20.0
        result = anti_scalp(
            entry=100.0, target=75.0, side="sell",
            c5_window=c5_window, k=4.0
        )
        assert result is True
    
    def test_sell_trade_fails_anti_scalp(self):
        """Test sell trade that fails anti-scalp check."""
        c5_window = [
            {"high": 110.0, "low": 105.0},  # range: 5.0
        ]
        # Median range = 5.0, k=6.0, min_distance = 30.0
        # Sell: entry=100, target=85, distance = 15.0 < 30.0
        result = anti_scalp(
            entry=100.0, target=85.0, side="sell",
            c5_window=c5_window, k=6.0
        )
        assert result is False
    
    def test_invalid_buy_trade_returns_false(self):
        """Test that invalid buy trade (target <= entry) returns False."""
        c5_window = [{"high": 105.0, "low": 100.0}]
        result = anti_scalp(
            entry=100.0, target=95.0, side="buy",  # Invalid: target < entry
            c5_window=c5_window
        )
        assert result is False
    
    def test_invalid_sell_trade_returns_false(self):
        """Test that invalid sell trade (target >= entry) returns False."""
        c5_window = [{"high": 105.0, "low": 100.0}]
        result = anti_scalp(
            entry=100.0, target=105.0, side="sell",  # Invalid: target > entry
            c5_window=c5_window
        )
        assert result is False
    
    def test_invalid_side_raises_error(self):
        """Test that invalid side raises ValueError."""
        c5_window = [{"high": 105.0, "low": 100.0}]
        with pytest.raises(ValueError, match="Invalid side 'hold'"):
            anti_scalp(
                entry=100.0, target=110.0, side="hold",
                c5_window=c5_window
            )
    
    def test_empty_window_raises_error(self):
        """Test that empty window raises ValueError."""
        with pytest.raises(ValueError, match="c5_window cannot be empty"):
            anti_scalp(
                entry=100.0, target=110.0, side="buy",
                c5_window=[]
            )
    
    def test_default_k_value(self):
        """Test that default k=6.0 is used when not specified."""
        c5_window = [{"high": 105.0, "low": 100.0}]  # range: 5.0
        # Default k=6.0, min_distance = 30.0
        # Buy: entry=100, target=125, distance = 25.0 < 30.0
        result = anti_scalp(entry=100.0, target=125.0, side="buy", c5_window=c5_window)
        assert result is False
        
        # With sufficient distance
        result = anti_scalp(entry=100.0, target=135.0, side="buy", c5_window=c5_window)
        assert result is True
    
    def test_different_k_values(self):
        """Test behavior with different k multiplier values."""
        c5_window = [{"high": 105.0, "low": 100.0}]  # range: 5.0
        
        # k=2.0, min_distance = 10.0
        result = anti_scalp(entry=100.0, target=115.0, side="buy", c5_window=c5_window, k=2.0)
        assert result is True  # distance = 15.0 >= 10.0
        
        # k=10.0, min_distance = 50.0
        result = anti_scalp(entry=100.0, target=115.0, side="buy", c5_window=c5_window, k=10.0)
        assert result is False  # distance = 15.0 < 50.0

