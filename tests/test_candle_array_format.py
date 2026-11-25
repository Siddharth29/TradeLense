"""Test candle array format to ensure correct indices are used."""

import pytest


def test_candle_array_format():
    """Test that candle arrays have the correct structure."""
    # Standard candle format: [timestamp, open, high, low, close, volume]
    candle = ["2025-01-10T10:30:00Z", 100.0, 102.0, 98.0, 101.0, 1500.0]
    
    # Verify correct indices
    assert candle[0] == "2025-01-10T10:30:00Z"  # timestamp
    assert candle[1] == 100.0  # open
    assert candle[2] == 102.0  # high
    assert candle[3] == 98.0   # low
    assert candle[4] == 101.0  # close
    assert candle[5] == 1500.0 # volume


def test_candle_array_extraction():
    """Test extracting OHLCV data from candle arrays."""
    candles_5 = [
        ["2025-01-10T10:00:00Z", 100.0, 102.0, 98.0, 101.0, 1500.0],
        ["2025-01-10T10:05:00Z", 101.0, 103.0, 99.0, 102.0, 1600.0],
        ["2025-01-10T10:10:00Z", 102.0, 104.0, 100.0, 103.0, 1700.0],
    ]
    
    # Extract using correct indices (as AI should do)
    opens = [c[1] for c in candles_5]
    highs = [c[2] for c in candles_5]
    lows = [c[3] for c in candles_5]
    closes = [c[4] for c in candles_5]
    volumes = [c[5] for c in candles_5]
    
    # Verify correct extraction
    assert opens == [100.0, 101.0, 102.0]
    assert highs == [102.0, 103.0, 104.0]
    assert lows == [98.0, 99.0, 100.0]
    assert closes == [101.0, 102.0, 103.0]
    assert volumes == [1500.0, 1600.0, 1700.0]


def test_candle_array_wrong_indices():
    """Test that using wrong indices produces wrong results."""
    candles_5 = [
        ["2025-01-10T10:00:00Z", 100.0, 102.0, 98.0, 101.0, 1500.0],
    ]
    
    # WRONG indices (as in the bug)
    wrong_highs = [c[1] for c in candles_5]  # c[1] is open, not high!
    wrong_lows = [c[2] for c in candles_5]   # c[2] is high, not low!
    
    # Verify these are WRONG
    assert wrong_highs == [100.0]  # Should be 102.0
    assert wrong_lows == [102.0]   # Should be 98.0
    
    # CORRECT indices
    correct_highs = [c[2] for c in candles_5]
    correct_lows = [c[3] for c in candles_5]
    
    # Verify these are CORRECT
    assert correct_highs == [102.0]
    assert correct_lows == [98.0]


def test_atr_calculation_with_correct_indices():
    """Test ATR calculation using correct candle array indices."""
    candles_5 = [
        ["2025-01-10T10:00:00Z", 100.0, 102.0, 98.0, 101.0, 1500.0],
        ["2025-01-10T10:05:00Z", 101.0, 105.0, 100.0, 104.0, 1600.0],
        ["2025-01-10T10:10:00Z", 104.0, 107.0, 103.0, 106.0, 1700.0],
    ]
    
    # Extract using correct indices
    highs = [c[2] for c in candles_5]
    lows = [c[3] for c in candles_5]
    closes = [c[4] for c in candles_5]
    
    # Calculate True Range using correct indices
    tr = []
    for i in range(len(candles_5)):
        if i == 0:
            # First candle: TR = high - low
            tr_value = highs[i] - lows[i]
        else:
            # Subsequent candles: TR = max(high-low, |high-prev_close|, |low-prev_close|)
            tr_value = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i-1]),
                abs(lows[i] - closes[i-1])
            )
        tr.append(tr_value)
    
    # Verify True Range calculations
    assert tr[0] == 4.0  # 102 - 98
    assert tr[1] == 5.0  # max(105-100=5, |105-101|=4, |100-101|=1)
    assert tr[2] == 4.0  # max(107-103=4, |107-104|=3, |103-104|=1)
    
    # Calculate ATR (average of True Range)
    atr = sum(tr) / len(tr)
    assert atr == pytest.approx(4.333, abs=0.01)


def test_generated_code_example():
    """Test that generated code would work with correct indices."""
    candles_5 = [
        ["2025-01-10T10:00:00Z", 100.0, 102.0, 98.0, 101.0, 1500.0],
        ["2025-01-10T10:05:00Z", 101.0, 105.0, 100.0, 104.0, 1600.0],
        ["2025-01-10T10:10:00Z", 104.0, 107.0, 103.0, 106.0, 1700.0],
    ]
    
    # Simulate generated code accessing candles correctly
    def run(candles_5, now):
        # ✓ CORRECT array access
        closes = [c[4] for c in candles_5]   # c[4] = close
        highs = [c[2] for c in candles_5]    # c[2] = high
        lows = [c[3] for c in candles_5]     # c[3] = low
        opens = [c[1] for c in candles_5]    # c[1] = open
        volumes = [c[5] for c in candles_5]  # c[5] = volume
        
        last_close = closes[-1]
        
        # ATR calculation with correct indices
        tr = [
            max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i-1]),
                abs(lows[i] - closes[i-1])
            ) if i > 0 else highs[i] - lows[i]
            for i in range(len(candles_5))
        ]
        atr = sum(tr) / len(tr)
        
        return {
            'last_close': last_close,
            'atr': atr,
            'highs': highs,
            'lows': lows
        }
    
    # Execute the function
    result = run(candles_5, "2025-01-10T10:10:00Z")
    
    # Verify results
    assert result['last_close'] == 106.0
    assert result['atr'] == pytest.approx(4.333, abs=0.01)
    assert result['highs'] == [102.0, 105.0, 107.0]
    assert result['lows'] == [98.0, 100.0, 103.0]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

