import pandas as pd
from zoneinfo import ZoneInfo

from core.candles import check_series_completeness


def test_check_series_completeness_true():
    tz = ZoneInfo("Asia/Kolkata")
    idx = pd.date_range("2024-01-01 09:15", periods=3, freq="5min", tz=tz)
    data = {
        "open": [1, 2, 3],
        "high": [1, 2, 3],
        "low": [1, 2, 3],
        "close": [1, 2, 3],
    }
    candles = pd.DataFrame(data, index=idx)
    assert check_series_completeness(candles, "5m") is True


def test_check_series_completeness_false():
    tz = ZoneInfo("Asia/Kolkata")
    idx = pd.date_range("2024-01-01 09:15", periods=4, freq="5min", tz=tz)
    idx = idx.delete(2)  # remove one timestamp to create a gap
    data = {
        "open": [1, 2, 4],
        "high": [1, 2, 4],
        "low": [1, 2, 4],
        "close": [1, 2, 4],
    }
    candles = pd.DataFrame(data, index=idx)
    assert check_series_completeness(candles, "5m") is False
