import pytest

from core.risk import calc_quantity
from core.risk.position_sizing import _round_down


def test_fixed_risk():
    qty = calc_quantity(
        balance=1000,
        risk=100,
        entry=200,
        stop=190,
        tick_size=1,
        lot_size=1,
        min_qty=1,
        max_qty=1000,
    )
    assert qty == 10


def test_percent_risk():
    qty = calc_quantity(
        balance=1000,
        risk=0.02,
        entry=50,
        stop=40,
        tick_size=1,
        lot_size=1,
        min_qty=1,
        max_qty=1000,
        risk_is_pct=True,
    )
    assert qty == 2


def test_rounding_behavior():
    qty = calc_quantity(
        balance=1000,
        risk=100,
        entry=101,
        stop=100,
        tick_size=0.5,
        lot_size=0.6,
        min_qty=0.1,
        max_qty=1000,
    )
    assert qty == 99.5


def test_min_clamping():
    qty = calc_quantity(
        balance=1000,
        risk=10,
        entry=100,
        stop=90,
        tick_size=1,
        lot_size=1,
        min_qty=5,
        max_qty=1000,
    )
    assert qty == 5


def test_max_clamping():
    qty = calc_quantity(
        balance=1000,
        risk=1000,
        entry=100,
        stop=99,
        tick_size=1,
        lot_size=1,
        min_qty=1,
        max_qty=100,
    )
    assert qty == 100


def test_zero_stop_distance():
    with pytest.raises(ValueError):
        calc_quantity(
            balance=1000,
            risk=100,
            entry=100,
            stop=100,
            tick_size=1,
            lot_size=1,
            min_qty=1,
            max_qty=100,
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "balance": -1,
            "risk": 10,
            "entry": 100,
            "stop": 90,
            "tick_size": 1,
            "lot_size": 1,
            "min_qty": 1,
            "max_qty": 100,
        },
        {
            "balance": 1000,
            "risk": 0,
            "entry": 100,
            "stop": 90,
            "tick_size": 1,
            "lot_size": 1,
            "min_qty": 1,
            "max_qty": 100,
        },
        {
            "balance": 1000,
            "risk": 10,
            "entry": 100,
            "stop": 90,
            "tick_size": 0,
            "lot_size": 1,
            "min_qty": 1,
            "max_qty": 100,
        },
        {
            "balance": 1000,
            "risk": 10,
            "entry": 100,
            "stop": 90,
            "tick_size": 1,
            "lot_size": 0,
            "min_qty": 1,
            "max_qty": 100,
        },
        {
            "balance": 1000,
            "risk": 10,
            "entry": 100,
            "stop": 90,
            "tick_size": 1,
            "lot_size": 1,
            "min_qty": 0,
            "max_qty": 100,
        },
        {
            "balance": 1000,
            "risk": 10,
            "entry": 100,
            "stop": 90,
            "tick_size": 1,
            "lot_size": 1,
            "min_qty": 1,
            "max_qty": 0,
        },
        {
            "balance": 1000,
            "risk": 10,
            "entry": 100,
            "stop": 90,
            "tick_size": 1,
            "lot_size": 1,
            "min_qty": 10,
            "max_qty": 5,
        },
    ],
)
def test_invalid_inputs(kwargs):
    with pytest.raises(ValueError):
        calc_quantity(**kwargs)


def test_round_down_invalid_step():
    with pytest.raises(ValueError):
        _round_down(1, 0)
