from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import asyncio

import pytest

from packages.core.operational_ledger import OperationalLedger
from packages.core.config import MT5_MAGIC_NUMBER
from packages.core.entry_policy import evaluate_entry_policy, tradable_minutes
from packages.core.typed_setup import compile_setup, conditional_compile, evaluate_setup, level_catalog, SetupSpec
from packages.core.schemas_trade import MultiExpertSignal
from packages.core.exit_policy import family_exit_rules, quote_action
from packages.core.management_feed import ManagementEventFeed

NOW = datetime(2026, 9, 10, 10, tzinfo=timezone.utc)


def deal(ticket, pid=1, *, entry=0, volume=1., profit=0., commission=0., swap=0., fee=0., magic=MT5_MAGIC_NUMBER, at=NOW):
    return dict(ticket=ticket, position_id=pid, entry=entry, volume=volume, profit=profit,
                commission=commission, swap=swap, fee=fee, magic=magic, time=at,
                symbol="XAUUSDm", type=0, price=2000.)


def test_whole_position_fees_manual_exit_idempotency_and_restart(tmp_path):
    path = tmp_path/"ledger.sqlite3"
    db = OperationalLedger(path)
    legs = [deal(1, commission=-2), deal(2, entry=1, volume=.5, profit=15, fee=-1, at=NOW+timedelta(minutes=5))]
    db.ingest("a", legs, magic=MT5_MAGIC_NUMBER)
    assert db.recent("a", "XAUUSD", NOW+timedelta(minutes=6)) == []
    assert db.position("a", 1)["remaining_quantity"] == .5
    legs.append(deal(3, entry=1, volume=.5, profit=-10, swap=-1, magic=0, at=NOW+timedelta(minutes=10)))
    db.ingest("a", legs, magic=MT5_MAGIC_NUMBER)
    db.ingest("a", list(reversed(legs)), magic=MT5_MAGIC_NUMBER)
    db.close()
    db = OperationalLedger(path)
    view = db.recent_views("a", "XAUUSD", NOW+timedelta(minutes=15))[0]
    assert view["pnl"] == 1 and view["result"] == "win"  # Last leg alone is a loss.
    assert view["bars_elapsed"] == 1
    assert db.throttle("a", "XAUUSD", NOW+timedelta(minutes=15), cap=1, loss_threshold=2, cooldown_minutes=30)[0]
    assert not db.throttle("b", "XAUUSD", NOW+timedelta(minutes=15), cap=1, loss_threshold=2, cooldown_minutes=30)[0]
    db.close()


def test_ledger_loss_streak_uses_net_and_causal_close_time(tmp_path):
    db = OperationalLedger(tmp_path/"state.sqlite3")
    for pid in (1, 2):
        db.ingest("a", [deal(pid*10, pid, commission=-3),
                        deal(pid*10+1, pid, entry=1, profit=2, at=NOW+timedelta(minutes=pid))], magic=MT5_MAGIC_NUMBER)
    assert not db.throttle("a", "XAUUSD", NOW, cap=0, loss_threshold=2, cooldown_minutes=30)[0]
    assert db.throttle("a", "XAUUSD", NOW+timedelta(minutes=3), cap=0, loss_threshold=2, cooldown_minutes=30)[0]
    assert not db.throttle("a", "XAUUSD", NOW+timedelta(minutes=32), cap=0, loss_threshold=2, cooldown_minutes=30)[0]
    db.close()


def test_ledger_transaction_rolls_back_changed_deal(tmp_path):
    db = OperationalLedger(tmp_path/"state.sqlite3")
    db.ingest("a", [deal(1)], magic=MT5_MAGIC_NUMBER)
    with pytest.raises(ValueError, match="identity changed"):
        db.ingest("a", [deal(2, 2), deal(1, profit=99)], magic=MT5_MAGIC_NUMBER)
    assert db.position("a", 2) is None
    db.close()


def test_manual_position_does_not_become_owned_on_app_exit(tmp_path):
    db = OperationalLedger(tmp_path/"state.sqlite3")
    db.ingest("a", [deal(1, magic=0), deal(2, entry=1)], magic=MT5_MAGIC_NUMBER)
    assert db.position("a", 1) is None
    db.close()


@pytest.mark.asyncio
async def test_analytics_disabled_does_not_disable_throttle(tmp_path, monkeypatch):
    monkeypatch.setenv("PERSIST_ANALYTICS", "false")
    monkeypatch.setenv("OPERATIONAL_LEDGER_PATH", str(tmp_path/"live.sqlite3"))
    import packages.core.risk.trade_throttle as throttle
    monkeypatch.setattr(throttle, "BACKTEST_MAX_TRADES_PER_DAY", 1)
    broker = SimpleNamespace(operational_snapshot=lambda now=None: ("a", [deal(1)]))
    assert (await throttle.trading_throttled("XAUUSD", broker=broker, now=NOW))[0]
    broker.operational_snapshot = lambda now=None: (_ for _ in ()).throw(RuntimeError("history unavailable"))
    assert (await throttle.trading_throttled("XAUUSD", broker=broker, now=NOW))[0]


def calendar(now=NOW):
    return {"verified": True, "source": "test-provider", "known_at": (now-timedelta(days=1)).isoformat(),
            "valid_from": (now-timedelta(hours=2)).isoformat(), "valid_until": (now+timedelta(hours=3)).isoformat(),
            "events": [{"id": "release", "at": (now+timedelta(minutes=30)).isoformat(),
                        "known_at": (now-timedelta(days=1)).isoformat(), "impact": "high", "currencies": ["USD"]}]}


@pytest.mark.parametrize("position,blocked", [("new", True), ("exposed", False), ("protected_runner", False)])
def test_event_horizon_distinguishes_position_risk(position, blocked):
    result = evaluate_entry_policy("XAUUSD", NOW, horizon_minutes=60, calendar=calendar(), position=position)
    assert result["blocked"] is blocked and result["events"] == ["release"]


@pytest.mark.parametrize("field,value", [("verified", False), ("known_at", "2027-01-01T00:00:00Z"), ("valid_until", "2025-01-01T00:00:00Z")])
def test_unreliable_calendar_retains_static_protection(field, value):
    data = calendar()
    data[field] = value
    result = evaluate_entry_policy("XAUUSD", NOW, calendar=data, static_policy=lambda *_: (True, "fallback"))
    assert result["blocked"] and result["calendar_source"] == "static_fallback"


def test_verified_calendar_replaces_static_slot_only_inside_coverage():
    data = calendar()
    data["events"] = []
    result = evaluate_entry_policy("XAUUSD", NOW, calendar=data, static_policy=lambda *_: (True, "slot"))
    assert not result["blocked"]


def test_multi_hour_entry_rejected_before_maintenance_runner_unaffected():
    now = NOW.replace(hour=19, minute=30)
    assert tradable_minutes("XAUUSDm", now) == 80  # Existing force-close buffer is excluded.
    assert evaluate_entry_policy("XAUUSD", now, horizon_minutes=120)["blocked"]
    assert not evaluate_entry_policy("XAUUSD", now, horizon_minutes=120, position="protected_runner")["blocked"]


@pytest.mark.parametrize("horizon", [float("nan"), float("inf"), -1])
def test_invalid_horizon_rejected(horizon):
    with pytest.raises(ValueError):
        evaluate_entry_policy("XAUUSD", NOW, horizon_minutes=horizon)


def setup_case(short=False):
    highs = [95, 97, 100, 96, 98, 120, 97, 96]
    c15 = [dict(time=NOW-timedelta(minutes=(8-i)*15), open=90., close=90., high=h, low=80.+i*.1) for i, h in enumerate(highs)]
    if short:
        c15 = [{**c, "open": 200-c["open"], "close": 200-c["close"], "high": 200-c["low"], "low": 200-c["high"]} for c in c15]
    catalog = level_catalog(c15, NOW)
    target = 80. if short else 120.
    spec = dict(family="continuation_retest", side="short" if short else "long",
                level_id=next(x["id"] for x in catalog if x["price"] == 100),
                target_level_id=next(x["id"] for x in catalog if x["price"] == target),
                entry=100., stop=110. if short else 90., tp1=target, tp2=70. if short else 130.,
                tolerance=2., expected_horizon_minutes=30, max_position_age_minutes=60)
    p = MultiExpertSignal(signal="sell" if short else "buy", entry=100, stop=spec["stop"], target=target,
                          ttl_minutes=90, confidence=.9, setup=spec)
    return p, c15


def bar(minute, close, low=None, high=None):
    return dict(time=NOW+timedelta(minutes=minute), close=close, open=close,
                low=close-.5 if low is None else low, high=close+.5 if high is None else high)


@pytest.mark.parametrize("short", [False, True])
def test_typed_compile_and_live_dry_run_first_trigger_parity(short):
    from packages.core.code_sniper_executor import CodeSniperExecutor
    p, c15 = setup_case(short)
    signal = compile_setup(p, [], c15, {"as_of": NOW})
    first, trigger = (bar(0, 97), bar(5, 99, high=100.5)) if short else (bar(0, 103), bar(5, 101, low=99.5))
    assert evaluate_setup(signal.metadata, [], first)["direction"] == "hold"
    result = evaluate_setup(signal.metadata, [first], trigger)
    assert result["direction"] == ("short" if short else "long")
    assert CodeSniperExecutor().dry_run_direction(signal, trigger, [first]) == p.signal
    later = bar(10, 99 if short else 101)
    assert evaluate_setup(signal.metadata, [first, trigger], later)["typed_status"] == "missed_trigger"


@pytest.mark.parametrize("status,current", [("invalidated", bar(0, 89)), ("expired", bar(90, 101))])
def test_typed_invalidated_and_expired_never_promoted(status, current):
    p, c15 = setup_case()
    signal = compile_setup(p, [], c15, {"as_of": NOW})
    assert evaluate_setup(signal.metadata, [], current)["typed_status"] == status


def test_conditional_adjudication_only_one_qualified_candidate():
    p, c15 = setup_case()
    cfg = {"as_of": NOW, "typed_setup_mode": "prefer"}
    assert conditional_compile([p], [], c15, cfg) is not None
    assert conditional_compile([p, p], [], c15, cfg) is None
    cfg = {"as_of": NOW, "typed_setup_mode": "shadow"}
    assert conditional_compile([p], [], c15, cfg) is None
    assert cfg["typed_compilation"]["metadata"]["snapshot_id"]
    bad = p.model_copy(deep=True)
    bad.setup["target_level_id"] = "invented"
    assert conditional_compile([bad], [], c15, {**cfg, "typed_setup_mode": "prefer"}) is None


def test_future_confirming_bar_cannot_contribute_level():
    _, c15 = setup_case()
    assert not any(x["price"] == 120 for x in level_catalog(c15, NOW-timedelta(minutes=15)))


@pytest.mark.parametrize("key,value", [("stop", 105), ("tp1", 105), ("tolerance", 5), ("tp2", float("nan"))])
def test_typed_geometry_rejects_invalid_intent(key, value):
    p, _ = setup_case()
    with pytest.raises(ValueError):
        SetupSpec.model_validate({**p.setup, key: value})


@pytest.mark.parametrize("direction,price", [("long", 121.), ("short", 79.)])
def test_quote_targets_do_not_increment_structural_confirmation(direction, price):
    state = {"direction": direction, "trail_consec": 1}
    rules = {"tp1": 110 if direction == "long" else 90, "tp": 120 if direction == "long" else 80, "runner_enabled": True}
    assert quote_action(state, rules, price) == "PARTIAL_TP2"
    assert state == {"direction": direction, "trail_consec": 1}


def test_range_family_has_no_invented_runner_and_legacy_is_unchanged():
    rules = {"tp1": 110, "tp": 120, "runner_enabled": True}
    assert family_exit_rules(None, rules) == rules
    range_rules = family_exit_rules("range_reclaim", rules, max_age_minutes=30)
    assert range_rules["tp"] == 110 and not range_rules["runner_enabled"]
    assert range_rules["hard_max_age_minutes"] == 30


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,action", [("shadow", None), ("live", "MOVE_SL_TO_BE")])
async def test_fast_feed_single_bar_task_and_actual_quote(mode, action):
    release = asyncio.Event()
    count = 0
    async def source():
        nonlocal count
        count += 1
        await release.wait()
        return bar(0, 99)
    broker = SimpleNamespace(executable_quote=lambda *_: {"price": 111, "time": datetime.now(timezone.utc)})
    records = []
    feed = ManagementEventFeed(broker, "XAUUSD", source, mode=mode, interval=.05, record=lambda k, v: records.append((k, v)))
    state = {"direction": "long", "entry": 100, "sl": 90, "initial_stop": 90, "quantity": 1}
    rules = {"tp1": 110, "tp": 120}
    try:
        event = await feed.next(state, rules)
        assert event["source"] == "quote" and event["quote_action"] == action
        await feed.next(state, rules)
        assert count == 1 and records[0][1]["quote_at"]
        release.set()
        assert (await feed.next(state, rules))["close"] == 99
    finally:
        await feed.close()


@pytest.mark.asyncio
async def test_stale_quote_does_not_cancel_bar_or_create_action():
    async def source():
        await asyncio.Event().wait()
    broker = SimpleNamespace(executable_quote=lambda *_: {"price": 111, "time": NOW})
    feed = ManagementEventFeed(broker, "XAUUSD", source, mode="live", interval=.05)
    try:
        assert (await feed.next({"direction": "long"}, {}))["source"] == "heartbeat"
        assert not feed.bar_task.done()
    finally:
        await feed.close()
    assert feed.bar_task is None


def test_uncertain_partial_reconciles_without_second_send(tmp_path, monkeypatch):
    from packages.adapters.mt5_adapter import OrderReconciliationRequired
    from packages.core.operational_execution import managed_partial, reconcile_management, ExitNotConfirmed
    monkeypatch.setenv("OPERATIONAL_LEDGER_PATH", str(tmp_path/"ledger.sqlite3"))
    rows = [deal(1)]
    calls = []
    def partial(*args, **kwargs):
        calls.append((args, kwargs))
        raise OrderReconciliationRequired("timeout")
    broker = SimpleNamespace(operational_snapshot=lambda now=None: ("a", rows), partial_close=partial)
    with pytest.raises(OrderReconciliationRequired):
        managed_partial(broker, 1, "XAUUSD", .5, "tp1")
    with pytest.raises(ExitNotConfirmed):
        managed_partial(broker, 1, "XAUUSD", .5, "tp1")
    with pytest.raises(ExitNotConfirmed):
        reconcile_management(broker, 1, {})
    rows.append(deal(2, entry=1, volume=.5, profit=10, at=NOW+timedelta(minutes=1)))
    patch = reconcile_management(broker, 1, {})
    assert patch == {"tp1_hit": True, "quantity": .5}
    assert len(calls) == 1 and calls[0][1]["position_ticket"] == 1


def test_replay_ledger_matches_all_booked_legs(tmp_path):
    from packages.core.backtest_account import PositionSimulator
    state = {"direction": "long", "entry": 2000., "sl": 1990., "initial_stop": 1990., "quantity": .1}
    sim = PositionSimulator("XAUUSD", state, {"tp1": 2020., "tp": 2040., "runner_enabled": True}, .1)
    position = SimpleNamespace(prediction={}, asset="XAUUSD", entry_time=NOW, trade_state=state,
                               simulator_for_replay=lambda: sim)
    db = OperationalLedger(tmp_path/"replay.sqlite3")
    db.record_simulation("replay", position)
    sim.step_quote(2021., NOW+timedelta(minutes=1))
    db.record_simulation("replay", position)
    assert not db.recent("replay", "XAUUSD", NOW+timedelta(minutes=2))
    sim.force_close(2006., NOW+timedelta(minutes=3))
    db.record_simulation("replay", position)
    assert db.recent("replay", "XAUUSD", NOW+timedelta(minutes=4))[0]["pnl"] == pytest.approx(sim.result.pnl)
    db.close()


@pytest.mark.parametrize("short", [False, True])
def test_typed_trigger_runs_through_real_replay_dispatch(monkeypatch, short):
    from packages.core.backtest_runner import BacktestRunner
    from packages.core.backtest_armed_strategy import ArmedStrategy
    p, c15 = setup_case(short)
    signal = compile_setup(p, [], c15, {"as_of": NOW})
    first, trigger = (bar(0, 97), bar(5, 99, high=100.5)) if short else (bar(0, 103), bar(5, 101, low=99.5))
    runner = BacktestRunner.__new__(BacktestRunner)
    runner.asset, runner._cycle_context = "XAUUSD", {}
    runner._strategy_context_before = lambda _: [first]
    armed = ArmedStrategy(signal.code, signal.model_dump(), {}, NOW, NOW+timedelta(minutes=90), 1)
    result = runner._run_armed_strategy_on_bar(armed, trigger, 1)
    assert result["direction"] == ("short" if short else "long")
    assert result["triggered_candle_time"] == NOW+timedelta(minutes=10)


@pytest.mark.asyncio
async def test_fast_live_owner_never_repeats_partial_or_counts_quotes_as_bars(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock
    import packages.core.services.trade_monitor as module
    for name in ("upsert_monitor_state", "update_monitor_state", "clear_monitor_state"):
        monkeypatch.setattr(module, name, AsyncMock())
    monkeypatch.setattr("packages.db.repositories.comprehensive_logging.store_comprehensive_trade", AsyncMock())
    monkeypatch.setattr(module, "_is_market_closed", lambda *a, **k: (False, 0))
    monkeypatch.setattr(module, "_is_market_closing_soon", lambda *a, **k: (False, ""))
    extend = MagicMock(side_effect=AssertionError("Quotes must not advance the bar-based TTL policy"))
    monkeypatch.setattr(module, "maybe_extend_expiry", extend)
    broker = SimpleNamespace(partial_close=MagicMock(return_value=.5),
                             modify_sl=MagicMock(side_effect=RuntimeError("Invalid stops")))
    monitor = module.TradeMonitor(SimpleNamespace(), broker)
    state = {"direction": "long", "entry": 100., "sl": 90., "initial_stop": 90., "quantity": 1., "trail_consec": 0}
    rules = {"tp1": 110., "tp": 120., "runner_enabled": True, "trail_config": []}
    n = 0
    async def next_event(state, rules):
        nonlocal n
        n += 1
        if n > 3:
            monitor._shutdown = True
            return {"source": "heartbeat"}
        return {"source": "quote", "time": datetime.now(timezone.utc), "close": 111., "high": 111., "low": 111.,
                "quote_action": quote_action(state, rules, 111.)}
    item = {"asset": "BTCUSD", "trade_state": state, "rules": rules,
            "expiry": datetime.now(timezone.utc)+timedelta(minutes=10), "prompt_hash": "test", "gpt_mode": "test", "latency_ms": 0,
            "_event_feed": SimpleNamespace(mode="live", next=next_event, record=None)}
    await asyncio.wait_for(monitor._monitor_impl(item), timeout=3)
    assert broker.partial_close.call_count == 1
    assert state["quantity"] == .5 and state["tp1_hit"]
    assert state["sl"] == 90 and state["trail_consec"] == 0
    extend.assert_not_called()


def test_actual_ioc_partial_receipt_is_used_not_requested_size():
    from tests.test_partial_fill_volume_2026_08_27 import _adapter
    adapter, fake = _adapter(.10)
    fake.order_send = lambda request: {"retcode": 10010, "volume": .02}
    assert adapter.partial_close("XAUUSD", .5) == .02


def test_missing_partial_receipt_requires_reconciliation():
    from tests.test_partial_fill_volume_2026_08_27 import _adapter
    from packages.adapters.mt5_adapter import OrderReconciliationRequired
    adapter, fake = _adapter(.10)
    fake.order_send = lambda request: {"retcode": 10009}
    with pytest.raises(OrderReconciliationRequired):
        adapter.partial_close("XAUUSD", .5)


def test_exact_ticket_close_read_failure_is_not_success():
    from packages.adapters.mt5_adapter import MT5Adapter
    adapter = MT5Adapter.__new__(MT5Adapter)
    adapter._reconnect_if_needed = lambda: None
    adapter._mt5 = SimpleNamespace(positions_get=lambda **_: None)
    with pytest.raises(RuntimeError, match="snapshot unavailable"):
        adapter.close_position_by_ticket(99)


def test_rolling_input_does_not_forget_early_break_or_invalidation():
    from packages.core.typed_setup import evaluate_observed_setup
    p, c15 = setup_case()
    p.ttl_minutes = 240
    signal = compile_setup(p, [], c15, {"as_of": NOW})
    observed, context = [], {}
    for i in range(46):
        current = bar(i*5, 103 if i == 0 else 106)
        result = evaluate_observed_setup(signal.metadata, observed[-40:], current, context)
        observed.append(current)
        assert result["direction"] == "hold"
    trigger = bar(230, 101, low=99.5)
    assert evaluate_observed_setup(signal.metadata, observed[-40:], trigger, context)["typed_status"] == "triggered"
    assert evaluate_setup(signal.metadata, observed[-40:], trigger)["typed_status"] == "history_unavailable"


def test_broker_ticket_alias_and_manual_final_leg_resolve_to_net_parent(tmp_path, monkeypatch):
    from packages.adapters.mt5_adapter import MT5Adapter
    monkeypatch.setenv("OPERATIONAL_LEDGER_PATH", str(tmp_path/"ledger.sqlite3"))
    rows = [deal(1, commission=-2), deal(2, entry=1, volume=.5, profit=10),
            {**deal(3, entry=1, volume=.5, profit=-5, magic=0), "reason": 4, "time_msc": int(NOW.timestamp()*1000)+321}]
    a = MT5Adapter.__new__(MT5Adapter)
    calls = []
    def history(**kwargs):
        calls.append(kwargs)
        return rows
    a._mt5 = SimpleNamespace(history_deals_get=history)
    a._credentials = {"login": 123}
    a._normalize_symbol = lambda _: "XAUUSDm"
    a.operational_snapshot = lambda now=None: ("demo", rows)
    a._operational_position_aliases = {999: 1}
    result = a.get_last_exit_deal("XAUUSD", 999)
    assert result["profit"] == 3 and result["position_id"] == 1 and result["reason"] == "SL"
    assert result["time"].microsecond == 321000
    assert calls == [{"position": 1}]


def test_quote_conversion_report_uses_net_legs_and_missing_r_stays_unknown(tmp_path):
    from scripts.audit.audit_operational_ledger import report
    path = tmp_path/"ledger.sqlite3"
    db = OperationalLedger(path)
    db.ingest("a", [deal(1, commission=-2), deal(2, entry=1, profit=5)], magic=MT5_MAGIC_NUMBER)
    db.observe("a", 1, "stop_receipt", {"error": "Invalid stops"})
    db.close()
    result = report(path)["positions"][0]
    assert result["net"] == 3 and result["signed_fees"] == -2
    assert result["net_r"] is None and result["stop_modification_failures"] == 1


def test_frozen_snapshot_bank():
    from scripts.audit.verify_setup_bank import verify
    result = verify("tests/fixtures/typed_setup_bank_v1.json")
    assert result["passed"] and len(result["cases"]) == 4
