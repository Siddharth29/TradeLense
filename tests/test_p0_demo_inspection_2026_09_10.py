from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.audit.verify_p0_demo import inspect_demo


def sdk_fixture(mode=0, login=123):
    return SimpleNamespace(
        ACCOUNT_TRADE_MODE_DEMO=0,
        account_info=Mock(return_value=SimpleNamespace(trade_mode=mode, login=login, server="Demo")),
        positions_get=Mock(return_value=[SimpleNamespace(ticket=7, symbol="BTCUSDm", price_open=100., sl=99., tp=102., volume=.03)]),
        symbol_info_tick=Mock(return_value=SimpleNamespace(bid=100., ask=100.01, time_msc=datetime.now(timezone.utc).timestamp()*1000)),
        order_send=Mock(side_effect=AssertionError("Read-only inspection must never trade")),
    )


def test_inspects_exact_existing_demo_fill_stop_and_quantity():
    sdk = sdk_fixture()
    result = inspect_demo(sdk, 123, "BTCUSDm", ticket=7, entry=100., sl=99., quantity=.03)
    assert result["fill_validation"] == "existing_position_matches_expected_state"
    sdk.positions_get.assert_called_once_with(ticket=7)
    sdk.order_send.assert_not_called()


@pytest.mark.parametrize("mode,login", [(2, 123), (0, 999)])
def test_wrong_account_never_reaches_position_or_quote_inspection(mode, login):
    sdk = sdk_fixture(mode, login)
    with pytest.raises(RuntimeError):
        inspect_demo(sdk, 123, "BTCUSDm")
    sdk.positions_get.assert_not_called()
    sdk.symbol_info_tick.assert_not_called()
    sdk.order_send.assert_not_called()


def test_absent_position_is_not_a_successful_fill_inspection():
    sdk = sdk_fixture()
    sdk.positions_get.return_value = []
    with pytest.raises(RuntimeError, match="absent"):
        inspect_demo(sdk, 123, "BTCUSDm", ticket=7, entry=100., sl=99., quantity=.03)


def test_stale_quote_does_not_pass_demo_readiness():
    sdk = sdk_fixture()
    sdk.symbol_info_tick.return_value.time_msc -= 120000
    with pytest.raises(RuntimeError, match="stale"):
        inspect_demo(sdk, 123, "BTCUSDm")


def test_mismatched_fill_quantity_is_reported():
    sdk = sdk_fixture()
    with pytest.raises(RuntimeError, match="quantity differs"):
        inspect_demo(sdk, 123, "BTCUSDm", ticket=7, entry=100., sl=99., quantity=.04)
