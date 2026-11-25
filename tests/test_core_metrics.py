"""Unit tests for the in-memory agent-metrics registry (packages.core.metrics).

Pure, deterministic counters — no I/O. Covers every ``record_*`` mutation and
the win/loss sign branches.
"""

from packages.core import metrics as m


def setup_function():
    m.reset()


def test_record_run_increments_and_opens_slot():
    m.record_run("AAA")
    m.record_run("AAA")
    reg = m.metrics()["AAA"]
    assert reg.runs == 2
    assert list(reg.slot_decisions) == [0, 0]
    assert reg.last_run is not None


def test_record_decision_counts_into_current_slot():
    m.record_run("AAA")
    m.record_decision("AAA")
    m.record_decision("AAA")
    reg = m.metrics()["AAA"]
    assert reg.decisions == 2
    assert reg.slot_decisions[-1] == 2


def test_record_decision_without_slot_is_safe():
    # No prior record_run → slot_decisions empty; decisions still counts.
    m.record_decision("AAA")
    assert m.metrics()["AAA"].decisions == 1


def test_record_order_pnl_sign_drives_win_loss():
    m.record_order("AAA", 5.0)   # win
    m.record_order("AAA", -3.0)  # loss
    m.record_order("AAA", 0.0)   # neither
    reg = m.metrics()["AAA"]
    assert reg.orders == 3
    assert reg.pnl == 2.0
    assert reg.wins == 1 and reg.losses == 1


def test_record_order_explicit_win_overrides_sign():
    m.record_order("AAA", -1.0, win=True)
    reg = m.metrics()["AAA"]
    assert reg.wins == 1 and reg.losses == 0


def test_record_trade_open_and_close():
    m.record_trade_open("AAA")
    m.record_trade_open("AAA")
    reg = m.metrics()["AAA"]
    assert reg.open_trades == 2 and reg.orders == 2

    m.record_trade_close("AAA", 10.0)
    m.record_trade_close("AAA", -4.0)
    assert reg.open_trades == 0
    assert reg.pnl == 6.0
    assert reg.wins == 1 and reg.losses == 1


def test_record_trade_close_never_negative_open_count():
    m.record_trade_close("AAA", 1.0)  # close with no open → clamps at 0
    assert m.metrics()["AAA"].open_trades == 0


def test_record_latency_drift_budget_skip():
    m.record_latency("AAA", 12.0)
    m.record_drift("AAA", 3.0)
    m.record_budget_skip("AAA")
    reg = m.metrics()["AAA"]
    assert list(reg.latencies) == [12.0]
    assert list(reg.drift_ms) == [3.0]
    assert reg.budget_skips == 1


def test_reset_clears_registry():
    m.record_run("AAA")
    assert m.metrics()
    m.reset()
    assert dict(m.metrics()) == {}
