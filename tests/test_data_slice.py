from core.io_loader import load_csv


def test_exact_candle_counts():
    # Repointed to MT5 data (the NIFTU CSVs were removed in the MT5-only pivot).
    # Position-based timestamps keep this data-agnostic: pick a ts that exists in
    # the data so the slice end is exact for any sorted OHLC CSV.
    five = load_csv("btcusd_5m_2years.csv").sort_index()
    fifteen = load_csv("btcusd_15m_2years.csv").sort_index()

    ts5 = five.index[200]
    c5 = five.loc[:ts5].tail(20)
    assert len(c5) == 20
    assert c5.index[-1] == ts5

    ts15 = fifteen.index[200]
    c15 = fifteen.loc[:ts15].tail(30)
    assert len(c15) == 30
    assert c15.index[-1] == ts15
