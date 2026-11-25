"""Better-price retest confirmations still pass all real-price risk gates.

The fixture illustrates the revised judge instructions, not a runtime rewrite
of an armed program. Both executors evaluate the same generated function.
"""

from datetime import datetime, timedelta, timezone

import pytest

from packages.core import code_sniper_executor as live_module
from packages.core.backtest_executor import BacktestExecutor
from packages.core.code_sniper_executor import CodeSniperExecutor
from packages.core.schemas_trade import CodeSniperSignal


BAR_TIME = datetime(2026, 11, 18, 9, 45, tzinfo=timezone.utc)
REVIEWED_RETEST = """def run(candles_5, now):
    e, sl, t1, t2, floor = 3779.5, 3790.2, 3766.0, 3754.7, 10.54
    c = candles_5[-1]
    ar = sum(b[2] - b[3] for b in candles_5[-10:]) / 10
    touched = any(e <= b[2] <= e + 2 * ar for b in candles_5[-3:])
    safe = max(b[2] for b in candles_5[-3:]) < sl
    close = c[4]
    final_sl = max(sl, close + floor)
    near = abs(close - e) <= 0.4 * (sl - e)
    rr = (close - t1) / (final_sl - close)
    side = 'sell' if touched and safe and close < c[1] and near and rr >= 1.2 else 'hold'
    return {'direction': side, 'entry': e, 'stop_loss': sl,
            'take_profit_1': t1, 'take_profit_2': t2,
            'confidence': 0.5, 'rationale': 'retest with directional confirmation'}
"""


class Funds:
    def available_funds(self):
        return 10000.


def bars(close=3781.496):
    return [{"time": (BAR_TIME - timedelta(minutes=5 * i)).isoformat(),
             "open": 3782.626, "high": max(3783.233, close),
             "low": min(3778.153, close), "close": close, "volume": 100.}
            for i in range(40, -1, -1)]


def signal(code):
    return CodeSniperSignal(code=code, ttl_minutes=180, confidence=.5,
                           metadata={"decision": "accepted", "strategy_type": "retest"})


def live_decision(monkeypatch, code, candle_window):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return BAR_TIME + timedelta(minutes=5, seconds=5)

    monkeypatch.setattr(live_module, "datetime", Clock)
    return CodeSniperExecutor().execute_strategy(
        signal(code), candle_window[-1], candle_window[:-1],
        {"sl_floor": 10.54, "risk_pct": .01}, "XAUUSD", "XAUUSD", Funds(),
    )


def test_better_short_price_reaches_live_sizing_with_floor_and_unchanged_target(monkeypatch):
    decision = live_decision(monkeypatch, REVIEWED_RETEST, bars())
    assert decision.quantity > 0
    assert decision.side.value == "sell"
    assert decision.entry == pytest.approx(3781.496)
    assert decision.sl == pytest.approx(3792.036)
    assert decision.tp1 == pytest.approx(3766.)
    assert (decision.entry - decision.tp1) / (decision.sl - decision.entry) >= 1.2
    # Gold is 100 ounces per lot: the approved loss stays below 1% of funds.
    assert decision.quantity * 100 * (decision.sl - decision.entry) <= 100.


@pytest.mark.parametrize("close", [3781.496, 3777.798])
def test_reviewed_generated_trigger_agrees_in_live_and_replay(monkeypatch, close):
    window = bars(close)
    live = live_decision(monkeypatch, REVIEWED_RETEST, window)
    replay = BacktestExecutor().simulate_code_execution(REVIEWED_RETEST, window, 40)
    assert not replay.get("error")
    assert (live.quantity > 0) == (close == 3781.496)
    assert replay["direction"] == ("short" if close == 3781.496 else "hold")


@pytest.mark.parametrize("close", [3777.798, 3768.])
def test_executor_rejects_weak_rr_or_excessive_drift_even_if_generated_code_ignores_them(monkeypatch, close):
    code = """def run(candles_5, now):
    return {'direction': 'sell', 'entry': 3779.5, 'stop_loss': 3790.2,
            'take_profit_1': 3766.0, 'take_profit_2': 3754.7,
            'confidence': 0.5, 'rationale': 'model omitted risk checks'}
"""
    decision = live_decision(monkeypatch, code, bars(close))
    assert decision.quantity == 0
