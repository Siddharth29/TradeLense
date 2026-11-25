import json

import pandas as pd

from core.utils import candles_to_json


def test_candles_json_includes_time():
    # Synthetic frame — the original NIFTY CSV was purged with the
    # Indian-market cleanup; this utility is data-source-agnostic.
    idx = pd.date_range("2024-02-12 09:15", periods=20, freq="5min", tz="UTC")
    df = pd.DataFrame(
        {"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100},
        index=idx,
    )
    out = candles_to_json(df)
    records = json.loads(out)
    assert len(records) == 20
    assert "time" in records[-1]
    assert pd.to_datetime(records[-1]["time"]) == idx[-1]
