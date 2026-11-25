"""A cap on the runner target must not reduce the validated first payout."""

import math

import pytest

from packages.core.code_sniper_executor import CodeSniperExecutor
from packages.core.models import Side
from packages.core.trade_geometry import cap_structural_targets


@pytest.mark.parametrize("side,stop,tp1,tp2,expected_tp2", [
    (Side.BUY, 99.0, 101.2, 112.0, 106.0),
    (Side.SELL, 101.0, 98.8, 88.0, 94.0),
])
def test_live_cap_preserves_validated_first_payout(monkeypatch, side, stop, tp1, tp2, expected_tp2):
    import packages.core.code_sniper_executor as executor_module
    monkeypatch.setattr(executor_module, "MAX_RR_RATIO", 6.0)
    monkeypatch.setattr(executor_module, "MIN_RR_RATIO_TP1", 1.2)
    ok, final_tp1, final_tp2 = CodeSniperExecutor()._validate_trade_levels(
        100.0, stop, tp1, tp2, side, "BTCUSD", "BTCUSD"
    )
    assert ok
    assert final_tp1 == tp1
    assert final_tp2 == expected_tp2
    assert abs(final_tp1 - 100.0) / abs(stop - 100.0) >= 1.2


@pytest.mark.parametrize("side,stop,tp1,tp2", [
    (Side.BUY, 99.0, 107.0, 112.0),
    (Side.SELL, 101.0, 93.0, 88.0),
])
def test_cap_rejects_first_target_beyond_allowed_runner(side, stop, tp1, tp2):
    with pytest.raises(ValueError, match="before the structural TP1"):
        cap_structural_targets(100.0, stop, tp1, tp2, side, 6.0)


@pytest.mark.parametrize("stop,tp1,tp2", [
    (101.0, 102.0, 103.0),  # stop on the profit side
    (99.0, 98.0, 103.0),   # first target on the loss side
    (99.0, 103.0, 102.0),  # targets reversed
    (99.0, math.nan, 103.0),
    (99.0, 102.0, math.inf),
])
def test_shared_geometry_rejects_invalid_buy_levels(stop, tp1, tp2):
    with pytest.raises(ValueError):
        cap_structural_targets(100.0, stop, tp1, tp2, Side.BUY, 6.0)


def test_valid_uncapped_targets_are_exactly_preserved():
    assert cap_structural_targets(100.0, 99.0, 101.2, 103.0, Side.BUY, 6.0) == (101.2, 103.0)


@pytest.mark.parametrize("side,stop,tp1,tp2", [
    (Side.BUY, 1.199, 1.202, 1.205),
    (Side.SELL, 1.201, 1.198, 1.195),
])
def test_exact_rr_cap_boundary_preserves_first_target(side, stop, tp1, tp2):
    assert cap_structural_targets(1.2, stop, tp1, tp2, side, 2.) == (tp1, tp1)


@pytest.mark.parametrize("entry,stop,tp1,tp2", [
    (-100., -101., -98., -97.), (100., 0., 200., 300.),
    (100., 99., float("nan"), 103.), (True, .5, 1.5, 2.),
])
def test_nonpositive_or_nonfinite_prices_are_not_valid_geometry(entry, stop, tp1, tp2):
    with pytest.raises(ValueError):
        cap_structural_targets(entry, stop, tp1, tp2, Side.BUY, 6.)
