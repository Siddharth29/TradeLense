"""Offline broker-boundary checks: quote qualification and confirmed fills."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from packages.adapters.mt5_adapter import MT5Adapter
from packages.core.models import Decision, PriceType, Side


def fixture_adapter(quotes, result=None):
    broker = MT5Adapter.__new__(MT5Adapter)
    broker._risk = SimpleNamespace(ensure_margin=Mock())
    broker._reconnect_if_needed = lambda: None
    broker._normalize_symbol = lambda s: s
    broker._mt5 = SimpleNamespace(
        symbol_select=Mock(return_value=True),
        symbol_info_tick=Mock(side_effect=quotes),
        symbol_info=Mock(return_value={"volume_step": .01, "volume_min": .01}),
        positions_get=Mock(return_value=[]),
        order_send=Mock(return_value=result or {
            "retcode": 10009, "order": 123, "price": 100., "volume": .1,
        }),
    )
    return broker


def decision(side=Side.BUY, tp1=None):
    sign = 1 if side == Side.BUY else -1
    return Decision(asset="BTCUSD", symbol="BTCUSDm", side=side,
                    price_type=PriceType.MARKET, entry=100., quantity=.1,
                    sl=100. - sign, tp1=tp1 or 100. + 1.2 * sign, tp=None)


@pytest.mark.parametrize("side,quote", [(Side.BUY, 100.3), (Side.SELL, 99.7)])
def test_worse_quote_cannot_destroy_approved_tp1_rr(side, quote):
    broker = fixture_adapter([SimpleNamespace(ask=quote, bid=quote)])
    with pytest.raises(ValueError, match="RR to TP1"):
        broker.place(decision(side))
    broker._mt5.order_send.assert_not_called()


@pytest.mark.parametrize("tick", [None, SimpleNamespace(ask=0),
                                  SimpleNamespace(ask=float("nan")),
                                  SimpleNamespace(ask=float("inf"))])
def test_missing_or_invalid_tick_never_falls_back_to_decision_price(tick):
    broker = fixture_adapter([tick])
    with pytest.raises(ValueError, match="market quote"):
        broker.place(decision())
    broker._mt5.order_send.assert_not_called()


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
def test_valid_runner_bracket_preserves_levels_and_volume(side):
    broker = fixture_adapter([SimpleNamespace(ask=100., bid=100.)])
    plan = decision(side)
    order, _ = broker.place(plan)
    sent = broker._mt5.order_send.call_args.args[0]
    assert sent["price"] == 100.
    assert sent["sl"] == plan.sl
    assert sent["tp"] == 0.  # Native SL, application-managed runner.
    assert sent["volume"] == pytest.approx(.1)
    assert order.average_price == 100.


def test_retry_revalidates_quote_before_resubmission(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    broker = fixture_adapter([SimpleNamespace(ask=100.), SimpleNamespace(ask=100.3)])
    broker._mt5.order_send.return_value = None
    with pytest.raises(ValueError, match="RR to TP1"):
        broker.place(decision())
    assert broker._mt5.order_send.call_count == 1


def test_partial_success_is_managed_at_confirmed_volume():
    broker = fixture_adapter([SimpleNamespace(ask=100.)], {
        "retcode": 10010, "order": 123, "price": 100.02, "volume": .04,
    })
    order, fills = broker.place(decision())
    assert order.status == "partially_filled"
    assert order.filled_quantity == .04
    assert fills[0].quantity == .04
    broker._mt5.order_send.assert_called_once()


def test_missing_execution_price_remains_unknown_for_reconciliation():
    broker = fixture_adapter([SimpleNamespace(ask=100.)], {
        "retcode": 10009, "order": 123, "volume": .1,
    })
    order, fills = broker.place(decision())
    assert order.average_price == 0.
    assert fills[0].price == 0.


@pytest.mark.parametrize("side,quote,tp1", [(Side.BUY, 100.41, 103.), (Side.SELL, 99.59, 97.)])
def test_drift_gate_holds_even_when_rr_remains_good(side, quote, tp1):
    broker = fixture_adapter([SimpleNamespace(ask=quote, bid=quote)])
    with pytest.raises(ValueError, match="deviates"):
        broker.place(decision(side, tp1))
    broker._mt5.order_send.assert_not_called()


@pytest.mark.parametrize("side,quote", [(Side.BUY, 98.9), (Side.SELL, 101.1)])
def test_price_beyond_stop_cannot_become_new_entry(side, quote):
    broker = fixture_adapter([SimpleNamespace(ask=quote, bid=quote)])
    with pytest.raises(ValueError, match="crossed SL"):
        broker.place(decision(side))
    broker._mt5.order_send.assert_not_called()


@pytest.mark.parametrize("side,first_send", [(Side.BUY, None), (Side.SELL, RuntimeError("IPC failure"))])
def test_retry_preserves_bracket_and_uses_new_side_quote(monkeypatch, side, first_send):
    monkeypatch.setattr("time.sleep", lambda _: None)
    sign = 1 if side == Side.BUY else -1
    next_price = 100. - .1 * sign  # Improved entry; only correct side quote is usable.
    broker = fixture_adapter([
        SimpleNamespace(ask=100., bid=100.),
        SimpleNamespace(ask=next_price if sign == 1 else 0., bid=next_price if sign == -1 else 0.),
    ])
    sent = []
    def send(request):
        sent.append(dict(request))
        if len(sent) == 1:
            if isinstance(first_send, Exception):
                raise first_send
            return first_send
        return {"retcode": 10009, "order": 123, "price": next_price, "volume": .1}
    broker._mt5.order_send.side_effect = send
    order, _ = broker.place(decision(side))
    assert [r["price"] for r in sent] == [100., next_price]
    assert sent[0]["sl"] == sent[1]["sl"] == 100. - sign
    assert sent[0]["volume"] == sent[1]["volume"] == .1
    assert order.average_price == next_price


@pytest.mark.parametrize("read_failure", [None, RuntimeError("IPC unavailable")])
def test_failed_initial_position_read_prevents_submission(read_failure):
    broker = fixture_adapter([])
    broker._mt5.positions_get.side_effect = [read_failure]
    with pytest.raises(RuntimeError, match="position snapshot"):
        broker.place(decision())
    broker._mt5.order_send.assert_not_called()


@pytest.mark.parametrize("read_failure", [None, RuntimeError("IPC unavailable")])
def test_unknown_position_after_ambiguous_send_never_retries(monkeypatch, read_failure):
    from packages.adapters.mt5_adapter import OrderReconciliationRequired
    monkeypatch.setattr("time.sleep", lambda _: None)
    broker = fixture_adapter([SimpleNamespace(ask=100.)])
    broker._mt5.order_send.return_value = None
    broker._mt5.positions_get.side_effect = [[], read_failure]
    with pytest.raises(OrderReconciliationRequired, match="no resend"):
        broker.place(decision())
    broker._mt5.order_send.assert_called_once()


def test_recovered_position_uses_single_authoritative_snapshot(monkeypatch):
    from packages.core.config import MT5_MAGIC_NUMBER
    monkeypatch.setattr("time.sleep", lambda _: None)
    broker = fixture_adapter([SimpleNamespace(ask=100.)])
    broker._mt5.order_send.return_value = None
    broker._mt5.positions_get.side_effect = [[], [
        {"ticket": 777, "magic": 0, "price_open": 105., "volume": .8},
        {"ticket": 123, "magic": MT5_MAGIC_NUMBER, "price_open": 100.01, "volume": .04},
    ]]
    order, _ = broker.place(decision())
    assert (order.id, order.average_price, order.filled_quantity) == ("123", 100.01, .04)
    broker._mt5.order_send.assert_called_once()
    assert broker._mt5.positions_get.call_count == 2


@pytest.mark.parametrize("quantity", [.001, 0., float("nan"), float("inf")])
def test_quantity_cannot_be_invalid_or_rounded_up_to_minimum(quantity):
    broker = fixture_adapter([SimpleNamespace(ask=100.)])
    plan = decision()
    plan.quantity = quantity
    with pytest.raises(ValueError):
        broker.place(plan)
    broker._mt5.order_send.assert_not_called()


def test_decimal_lot_step_does_not_drop_a_valid_lot():
    broker = fixture_adapter([SimpleNamespace(ask=100.)])
    plan = decision()
    plan.quantity = .29
    broker.place(plan)
    assert broker._mt5.order_send.call_args.args[0]["volume"] == .29


@pytest.mark.parametrize("metadata", [
    None, {}, {"volume_step": 0., "volume_min": .01},
    {"volume_step": float("nan"), "volume_min": .01},
    {"volume_step": .01, "volume_min": float("inf")},
    {"volume_step": .01, "volume_min": .1, "volume_max": .05},
    RuntimeError("metadata read failed"),
])
def test_lot_metadata_failure_cannot_bypass_quantity_validation(metadata):
    broker = fixture_adapter([])
    broker._mt5.symbol_info.side_effect = [metadata]
    with pytest.raises(ValueError, match="Cannot qualify order volume"):
        broker.place(decision())
    broker._mt5.order_send.assert_not_called()
