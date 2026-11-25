from dataclasses import asdict
from datetime import datetime

import pandas as pd
import pytest

from packages.core import parse_prediction
from core.trade_sim import simulate_trade


def _make_future(closes):
    base = datetime(2024, 1, 1, 9, 20)
    rows = [{"open": c, "high": c + 1, "low": c - 1, "close": c} for c in closes]
    return pd.DataFrame(rows, index=[base + pd.Timedelta(minutes=5 * i) for i in range(len(rows))])


def _future_ohlc(vals):
    """Explicit OHLC bars — needed wherever the trigger's touch test matters."""
    base = datetime(2024, 1, 1, 9, 20)
    rows = [{"open": o, "high": h, "low": l, "close": c} for o, h, l, c in vals]
    return pd.DataFrame(rows, index=[base + pd.Timedelta(minutes=5 * i) for i in range(len(rows))])


# A retest only triggers when >= PATTERN_THRESHOLDS["min_touch_pct"] (30%) of the
# bar's range sits inside level +/- buffer. The old fixtures here used bars whose
# touch was exactly 25%, so every trigger-dependent test below was asserting on a
# "No Entry" result. Bars centred on the level (touch 50%) express what these
# tests actually mean.
_RETEST_BAR = (10, 11, 9, 10)   # level 10, buffer 0.5 -> 50% touch


def _parse(pred: dict) -> dict:
    model = parse_prediction(pred)
    assert model is not None
    out = asdict(model)
    # Triggers hard-fail on buffer==0.0 ("no fallbacks in production") —
    # simulations must supply an explicit buffer like live predictions do.
    out["buffer"] = pred.get("buffer", 0.5)
    return out


def test_no_entry():
    pred = _parse(
        {
            "direction": "long",
            "pattern": "retest",
            "level": 50,
            "stop": 45,
            "target": 55,
            "confidence": "medium",
        }
    )
    fut = pd.DataFrame(
        [{"low": 51, "high": 52, "close": 51.5}] * 12,
        index=pd.date_range("2024-01-01 10:00", periods=12, freq="5T"),
    )
    out = simulate_trade(pred, fut)
    assert out["result"] == "No Entry"
    assert out["initial_stop"] == 45


def test_tp_books_partials_and_leaves_a_runner():
    """TP2 no longer closes the whole position — it books a slice and trails.

    Live has booked 50% at TP1 and TP2_PARTIAL_FRACTION of the remainder at TP2
    since the runner shipped; the simulator now does the same, so reaching the
    final target leaves an open runner rather than a closed "TP".
    """
    fut = _future_ohlc([_RETEST_BAR, (11, 13.5, 10.5, 13), (14, 16.5, 13.5, 16)])
    pred = _parse(
        {
            "direction": "long",
            "pattern": "retest",
            "level": 10,
            "stop": 8,
            "target": 16,
            "confidence": "medium",
        }
    )
    out = simulate_trade(pred, fut)
    assert out["tp1_points"] > 0, "TP1 partial must be booked"
    # `profit` is booked once, at close. While the runner is still alive the row
    # is "Open" and realised legs are reported separately, so summing `profit`
    # across iterations can never double-count a partial.
    assert out["profit"] == 0.0
    assert out["result"] == "Open"
    # This legacy pred names a single `target`, so RR stays entry -> target
    # (|16-10| / |10-8| = 3). The entry->TP1 preference only applies when the
    # judge actually supplied a TP1, which is the code-sniper case; the midpoint
    # used for partial booking is an internal device, not a named first target.
    assert out["risk_reward"] == pytest.approx(3.0)


def test_sl():
    fut = _future_ohlc([_RETEST_BAR, (9, 9.5, 7.5, 8)])
    pred = _parse(
        {
            "direction": "long",
            "pattern": "retest",
            "level": 10,
            "stop": 8,
            "target": 16,
            "confidence": "medium",
        }
    )
    out = simulate_trade(pred, fut)
    assert out["result"] == "SL"
    # Full size lost from entry 10 to stop 8 (contract size 1 on the default).
    assert out["profit"] == -2
    assert out["risk_reward"] == 3


def test_timeout():
    """The monitor-expiry close reports the canonical "Timeout" label.

    It used to emit "Time Exit (Win)" / "(Loss)" / "(BE)", which no consumer
    recognised: the summary endpoint binned the first two as "other" (dropping
    them from win rate, profit factor and expectancy) and counted the third as a
    break-even. See packages/core/backtest_types.
    """
    from packages.core.config import TRADE_SIM_MAX_DURATION_HOURS

    bars = (TRADE_SIM_MAX_DURATION_HOURS * 60) // 5 + 2
    fut = _future_ohlc([_RETEST_BAR] + [(10, 10.4, 9.6, 10)] * bars)
    pred = _parse(
        {
            "direction": "long",
            "pattern": "retest",
            "level": 10,
            "stop": 5,
            "target": 20,
            "confidence": "medium",
        }
    )
    assert simulate_trade(pred, fut)["result"] == "Timeout"


def test_invalid_prediction_format():
    fut = _make_future([11, 12])
    raw = {
        "direction": "long",
        "pattern": "retest",
        "level": 10,
        "stop": 15,
        "target": 12,
        "confidence": "medium",
    }
    pred = parse_prediction(raw)
    assert pred is None
    out = simulate_trade(pred, fut)
    assert out["result"] == "No Entry"
    assert out["initial_stop"] is None


def test_empty_future(monkeypatch):
    pred = _parse(
        {
            "direction": "long",
            "pattern": "retest",
            "level": 50,
            "stop": 45,
            "target": 55,
            "confidence": "medium",
        }
    )
    fut = pd.DataFrame(columns=["open", "high", "low", "close"])
    monkeypatch.setattr("core.trade_sim.entry_trigger_met", lambda *a, **k: True)
    out = simulate_trade(pred, fut)
    assert out["result"] == "No Entry"
    assert out["entry_time"] is None

def test_be_after_tp1_long():
    base = datetime(2024, 1, 1, 9, 20)
    fut = pd.DataFrame(
        [
            {"open": 11, "high": 11.5, "low": 10, "close": 11.25},
            {"open": 11.25, "high": 11.5, "low": 9, "close": 10},
        ],
        index=[base, base + pd.Timedelta(minutes=5)],
    )
    pred = _parse(
        {
            "direction": "long",
            "pattern": "retest",
            "level": 10,
            "stop": 8,
            "target": 12,
            "confidence": "medium",
        }
    )
    out = simulate_trade(pred, fut)
    assert out["result"] == "BE"
    # Market partial at 11.25 books 0.5 * 1.25. The 11.0 stop is strictly
    # below that close, so it can actually be installed before the next bar.
    assert out["tp1_points"] == pytest.approx(0.625)
    assert out["initial_stop"] == 8


def test_be_after_tp1_short():
    base = datetime(2024, 1, 1, 9, 20)
    fut = pd.DataFrame(
        [
            {"open": 9, "high": 10, "low": 8.5, "close": 8.75},
            {"open": 8.75, "high": 11, "low": 8.5, "close": 10},
        ],
        index=[base, base + pd.Timedelta(minutes=5)],
    )
    pred = _parse(
        {
            "direction": "short",
            "pattern": "retest",
            "level": 10,
            "stop": 12,
            "target": 8,
            "confidence": "medium",
        }
    )
    out = simulate_trade(pred, fut)
    assert out["result"] == "BE"
    assert out["tp1_points"] == pytest.approx(0.625)
    assert out["initial_stop"] == 12


def _future_from_rows(vals):
    base = datetime(2024, 1, 1, 9, 20)
    rows = [{"open": o, "high": h, "low": l, "close": c} for o, h, l, c in vals]
    return pd.DataFrame(rows, index=[base + pd.Timedelta(minutes=5 * i) for i in range(len(rows))])


# Every case carries an explicit buffer: the trigger checks hard-fail on
# buffer == 0.0 ("no fallbacks in production"), and these preds bypass _parse,
# so without one they all returned False regardless of the price action.
_PATTERN_CASES = [
    (
        {"direction": "long", "pattern": "retest", "level": 10, "stop": 8, "target": 14, "buffer": 0.5},
        _future_from_rows([(10, 11, 9, 10)]),
    ),
    (
        {"direction": "long", "pattern": "orderblock", "level": 10, "stop": 8, "target": 14, "buffer": 0.5},
        _future_from_rows([(10, 11, 9, 10)]),
    ),
    (
        {"direction": "long", "pattern": "breakout", "level": 10, "stop": 8, "target": 12, "buffer": 0.5},
        _future_from_rows([(11, 12, 10, 11)]),
    ),
    (
        # A compression breakout also requires bullish momentum (close > open);
        # the old doji bar (11, 12, 10, 11) could never satisfy it.
        {"direction": "long", "pattern": "compression breakout", "level": 10, "stop": 8, "target": 13, "buffer": 0.5},
        _future_from_rows([(10.6, 12, 10.4, 11.5)]),
    ),
    (
        {"direction": "long", "pattern": "FVG fill", "level": 10, "stop": 8, "target": 14, "buffer": 0.5},
        _future_from_rows([(11, 12, 10, 11)]),
    ),
    (
        {"direction": "long", "pattern": "liquidity sweep", "level": 10, "stop": 8, "target": 14, "buffer": 0.5},
        _future_from_rows([(10, 12, 9, 11)]),
    ),
    (
        {"direction": "long", "pattern": "range breakout retest", "level": 10, "stop": 8, "target": 14, "buffer": 0.5},
        _future_from_rows([(10, 12, 9, 11), (11, 12, 9, 10)]),
    ),
    (
        # BOS needs swing structure: bos_span*2+2 bars, a swing high, a bar that
        # breaks it and a following bar that holds above. One bar cannot express
        # a break of structure, so the old single-bar fixture always returned False.
        {"direction": "long", "pattern": "BOS", "level": 10, "stop": 8, "target": 14, "buffer": 0.5},
        _future_from_rows([
            (10, 10.5, 9.8, 10.2),
            (10.2, 10.8, 10.0, 10.6),
            (10.6, 11.5, 10.4, 11.0),   # swing high
            (11.0, 11.2, 10.6, 10.8),
            (10.8, 11.0, 10.5, 10.9),
            (10.9, 12.4, 10.8, 12.2),   # break
            (12.2, 12.6, 12.0, 12.5),   # hold
        ]),
    ),
]


@pytest.mark.parametrize("pred,fut", _PATTERN_CASES)
def test_entry_trigger_patterns(pred, fut):
    out = simulate_trade(pred, fut)
    assert out["result"] != "No Entry"
    assert out["entry_time"] is not None

