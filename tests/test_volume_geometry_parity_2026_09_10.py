from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from packages.core.volume import floor_volume
from packages.core.models import Side
from packages.core.code_sniper_executor import CodeSniperExecutor
from packages.core.backtest_runner import BacktestRunner
from packages.core.backtest_account import size_position
from packages.core.risk import CONTRACT_SPECS, calc_lot_size_by_risk


@pytest.mark.parametrize("value,step,minimum,maximum,expected", [
    (.29, .01, .01, 5., .29), (.299, .01, .01, 5., .29),
    (.3, .1, .1, 5., .3), (.007, .001, .001, 5., .007),
    (.875, .25, .25, 5., .75), (2., .01, .01, .295, .29),
])
def test_volume_is_on_grid_without_exceeding_approved_size(value, step, minimum, maximum, expected):
    assert floor_volume(value, step, minimum, maximum) == expected
    assert expected <= value


@pytest.mark.parametrize("value,step,minimum,maximum", [
    (.009, .01, .01, 5.), (0., .01, .01, 5.), (-.1, .01, .01, 5.),
    (float("nan"), .01, .01, 5.), (float("inf"), .01, .01, 5.),
    (True, .01, .01, 5.), (.29, 0., .01, 5.), (.29, float("nan"), .01, 5.),
    (.29, .01, None, 5.), (.29, .01, .01, float("inf")), (.29, .01, .1, .05),
])
def test_bad_volume_or_contract_is_rejected(value, step, minimum, maximum):
    with pytest.raises(ValueError):
        floor_volume(value, step, minimum, maximum)


def test_risk_sizing_live_and_replay_preserve_point_29_lots():
    expected = calc_lot_size_by_risk(29., .01, 1., "BTCUSD", CONTRACT_SPECS)
    replay = size_position("BTCUSD", 29., 100., 99., .01)
    risk = SimpleNamespace(_broker=None, available_funds=lambda: 29.)
    live = CodeSniperExecutor()._calculate_position_size(
        entry=100., stop_loss=99., context={"risk_pct": .01}, risk_service=risk,
        asset="BTCUSD", symbol="BTCUSDm",
    )
    assert expected == replay.lot == live == .29


@pytest.mark.parametrize("parameter", ["balance", "risk_pct", "sl_points"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0., -1.])
def test_risk_sizing_rejects_invalid_numeric_inputs(parameter, value):
    args = dict(balance=29., risk_pct=.01, sl_points=1., symbol="BTCUSD", specs=CONTRACT_SPECS)
    args[parameter] = value
    with pytest.raises(ValueError):
        calc_lot_size_by_risk(**args)


@pytest.mark.parametrize("side,stop,tp1,tp2", [
    (Side.BUY, 100.1, 102., 103.), (Side.SELL, 99.9, 98., 97.),
])
def test_wrong_side_stop_is_rejected_before_floor_in_live_and_replay(side, stop, tp1, tp2):
    now = datetime(2026, 7, 14, tzinfo=timezone.utc)
    replay = BacktestRunner("BTCUSD", now, now + timedelta(days=1))
    replay._cycle_context = {"sl_floor": 1.}
    result = replay._apply_execution_gates("long" if side == Side.BUY else "short",
                                           100., stop, tp1, tp2, 100., None)
    assert result["ok"] is False
    assert "not protective" in result["reason"]
    live = CodeSniperExecutor()._convert_to_decision(
        {"direction": side.value, "entry": 100., "stop_loss": stop,
         "take_profit_1": tp1, "take_profit_2": tp2, "confidence": .8},
        "BTCUSD", "BTCUSDm", Mock(), {"sl_floor": 1.}, risk_service=Mock(),
    )
    assert live.quantity == 0.
    assert "not protective" in live.notes


@pytest.mark.parametrize("side,stop,tp1,tp2", [
    (Side.BUY, 1.099, 1.1012, 1.103), (Side.SELL, 1.101, 1.0988, 1.097),
])
def test_exact_minimum_rr_passes_both_live_and_replay(monkeypatch, side, stop, tp1, tp2):
    now = datetime(2026, 7, 14, tzinfo=timezone.utc)
    replay = BacktestRunner("EURUSD", now, now + timedelta(days=1))
    monkeypatch.setattr(replay, "_min_target_distances", lambda: (0., 0.))
    live = CodeSniperExecutor()
    monkeypatch.setattr(live, "_validate_target_distances", lambda *args: True)
    assert live._validate_trade_levels(1.1, stop, tp1, tp2, side, "EURUSD", "EURUSD")[0]
    assert replay._validate_trade_levels(1.1, stop, tp1, tp2, side == Side.BUY)[0]
