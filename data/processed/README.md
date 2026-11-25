# Historical candles

Optional backtests use locally supplied historical OHLCV CSVs. Place both intervals here as <asset>_5m_2years.csv and <asset>_15m_2years.csv, using the lowercase asset symbol. The suffix is a filename convention; the files need to cover the requested window and include earlier candles for indicator warm-up.

Use columns time, open, high, low, close, volume. Time is the UTC candle-open timestamp. Use market data you have permission to access and keep your CSVs local.
