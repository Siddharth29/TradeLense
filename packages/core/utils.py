import pandas as pd
import numpy as np


def candles_to_json(df: pd.DataFrame) -> str:
    """Return JSON records for candles including the time column."""
    return df.reset_index().to_json(orient="records", date_format="iso")


def _first_not_none(*values):
    """Return the first non-None value. Unlike ``or``, treats 0.0 as valid."""
    for v in values:
        if v is not None:
            return v
    return None


def calculate_atr(candles: pd.DataFrame, period: int = 14) -> float:
    """Calculate Average True Range (ATR) from a candle DataFrame.

    Parameters
    ----------
    candles:
        DataFrame with columns ``high``, ``low``, ``close``.
    period:
        Look-back period (default 14).

    Returns
    -------
    float
        ATR value, or 0.0 when there are insufficient candles.
    """
    if candles is None or len(candles) < period + 1:
        return 0.0

    high = candles["high"].values
    low = candles["low"].values
    close = candles["close"].values

    tr = np.maximum(
        high[1:] - low[1:],
        np.maximum(
            np.abs(high[1:] - close[:-1]),
            np.abs(low[1:] - close[:-1]),
        ),
    )

    atr = np.mean(tr[-period:]) if len(tr) >= period else np.mean(tr)
    return float(atr)
