"""Support/resistance must be on the correct side of current price.

Regression for the 2026-06-21 finding that support_levels / resistance_levels
were unfiltered swing lows/highs (the 3 *highest* of each), so ~70% of live
judge contexts (all symbols) reported "support" above price or "resistance"
below price.
"""
import numpy as np
import pytest

from packages.core.market_structure import MarketStructureAnalyzer


def _candles(prices):
    """Build OHLC dicts from a close path (small wicks around each close)."""
    out = []
    for i, c in enumerate(prices):
        out.append({
            "time": f"2026-06-20T{i//12:02d}:{(i%12)*5:02d}:00+00:00",
            "open": c, "high": c + 5, "low": c - 5, "close": c, "volume": 100.0,
        })
    return out


def _levels(path):
    a = MarketStructureAnalyzer()
    cs = _candles(path)
    ms = a.analyze_market_structure(cs[:-1], cs[-1])
    return ms["support_levels"], ms["resistance_levels"], path[-1]


def test_support_below_resistance_above_after_pullback():
    """Rally then pull back: the stranded high swing lows must NOT be 'support'."""
    # up to ~64300 then back down to ~63900 (the logs(22) 16:30 shape)
    up = list(range(63100, 64300, 30))
    down = list(range(64300, 63900, -30))
    sup, res, price = _levels(up + down)
    assert all(s < price for s in sup), f"support above price: {sup} vs {price}"
    assert all(r > price for r in res), f"resistance below price: {res} vs {price}"


def test_breakout_to_new_high():
    """Break to a new high: old swing highs below price must NOT be 'resistance'."""
    base = [63000 + (i % 5) * 20 for i in range(40)]   # chop 63000-63080
    breakout = list(range(63080, 63600, 30))           # break upward
    sup, res, price = _levels(base + breakout)
    assert all(r > price for r in res), f"resistance below price: {res} vs {price}"
    assert all(s < price for s in sup), f"support above price: {sup} vs {price}"


def test_levels_are_real_swings_not_invented():
    path = list(range(63000, 64000, 25)) + list(range(64000, 63500, -25))
    sup, res, price = _levels(path)
    # every reported level must be on the correct side
    assert all(s < price for s in sup)
    assert all(r > price for r in res)
    # and at most 3 each
    assert len(sup) <= 3 and len(res) <= 3


def test_pivot_uses_window_range_not_single_candle():
    """Pivot range must reflect the whole window, not one 5m bar."""
    a = MarketStructureAnalyzer()
    cs = _candles(list(range(63000, 64000, 25)))
    piv = a._calculate_pivot_points(
        np.array([c["high"] for c in cs]),
        np.array([c["low"] for c in cs]),
        np.array([c["close"] for c in cs]),
    )
    # r1-s1 spread should span a big fraction of the ~1000pt window, not ~10pt
    assert (piv["r1"] - piv["s1"]) > 100


def test_price_action_uses_prior_bars_and_preserves_fx_price_precision():
    from packages.core.market_structure import analyze_price_action_patterns

    historical = [
        {"close": close, "high": close + 0.00002, "low": close - 0.00002}
        for close in (1.10001, 1.10002, 1.10003, 1.10004, 1.10005,
                      1.10006, 1.10007, 1.10008, 1.10009)
    ]
    result = analyze_price_action_patterns(
        historical, {"close": 1.10020, "high": 1.10021, "low": 1.10006}
    )
    assert result["breakout"]["direction"] == "up"
    assert result["breakout"]["confirmed"]
    assert "reversal" not in result
