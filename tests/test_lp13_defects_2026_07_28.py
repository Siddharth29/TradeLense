"""Tests for the three defects found in the 2026-07-27 LP-13 ship's first live run.

Evidence: docs/trading_insights/daily/AUDIT_2026-07-27.md §6, log
results/logs/backtest_2026-07-27.log.

BUG-1 — the SYMBOL fallback in ``update_comprehensive_trade_exit`` closed a
        17-day-old USTEC doc (entry 29845.65, ticket 2194353981) with that day's
        +3.12 / 28534.98, because a duplicate exit arrived after the real doc had
        already been closed by ticket.
BUG-2 — the eager monitor init was guarded on ``_trade_monitor_initialized``,
        which api.py resets every cycle by building a fresh AgentRunner; the
        process-global "is this asset already managed" question is the right one.
BUG-3 — ``get_last_exit_deal`` raised on the legacy composite ticket
        "XAUUSD:4517.5", and the caller's except swallowed it back to the
        pre-fix bare clear.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import packages.core.services.trade_monitor as tm
import packages.db.repositories.comprehensive_logging as cl
from packages.adapters.mt5_adapter import MT5Adapter


# ---------------------------------------------------------------- BUG-1 ----
# Minimal async Mongo doubles: find().sort().limit() → to_list().

class _FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    async def to_list(self, length=None):
        return list(self._docs[: (length or len(self._docs))])


class _FakeCollection:
    def __init__(self, docs):
        self.docs = docs
        self.updates: list[tuple] = []

    def find(self, query):
        out = []
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                out.append(d)
        return _FakeCursor(out)

    async def update_one(self, flt, update):
        self.updates.append((flt, update))
        for d in self.docs:
            if d["_id"] == flt["_id"]:
                d.update(update["$set"])


def _wire_mongo(monkeypatch, docs):
    coll = _FakeCollection(docs)
    monkeypatch.setattr(cl, "analytics_persistence_enabled", lambda: True)
    monkeypatch.setattr(
        cl, "get_client",
        lambda: {cl.MONGO_DB_NAME: {cl._COLLECTION_COMPREHENSIVE_TRADES: coll}},
    )
    monkeypatch.setattr(cl, "_EXIT_MATCH_RETRY_SECONDS", 0)
    events: list = []
    monkeypatch.setattr(cl, "log_event", lambda *a, **k: events.append((a, k)))
    return coll, events


# The real 07-27 shapes.
_STALE_USTEC = {
    "_id": "6a512af1b0eb589e38fa519d",
    "symbol": "USTEC", "asset": "USTEC", "side": "sell",
    "entry_price": 29845.65, "position_ticket": "2194353981",
    "trade_status": "open",
    "trade_executed_at": datetime(2026, 7, 10, 17, 25, tzinfo=timezone.utc),
}


@pytest.mark.asyncio
async def test_symbol_fallback_refuses_doc_pinned_to_another_ticket(monkeypatch):
    """The exact 07-27 corruption must no longer be possible."""
    coll, events = _wire_mongo(monkeypatch, [dict(_STALE_USTEC)])

    ok = await cl.update_comprehensive_trade_exit(
        symbol="USTEC",
        exit_price=28534.98,
        exit_reason="TIMEOUT",
        pnl=3.12,
        trade_closed_at=datetime(2026, 7, 27, 12, 38, 5, tzinfo=timezone.utc),
        position_ticket="2211564328",          # today's position, not the doc's
    )

    assert ok is False
    assert coll.updates == []                   # nothing written
    assert coll.docs[0]["trade_status"] == "open"
    assert coll.docs[0]["entry_price"] == 29845.65
    assert "exit_price" not in coll.docs[0]
    assert events == []                         # no phantom trade_exit event


@pytest.mark.asyncio
async def test_symbol_fallback_refuses_when_closer_has_no_ticket(monkeypatch):
    """A ticket-less close must not adopt a doc that IS pinned to a position."""
    coll, events = _wire_mongo(monkeypatch, [dict(_STALE_USTEC)])

    ok = await cl.update_comprehensive_trade_exit(
        symbol="USTEC", exit_price=28534.98, exit_reason="TIMEOUT", pnl=3.12,
    )

    assert ok is False
    assert coll.updates == []
    assert events == []


@pytest.mark.asyncio
async def test_symbol_fallback_still_closes_an_unpinned_doc(monkeypatch):
    """The fallback must keep working for docs with no position_ticket."""
    doc = dict(_STALE_USTEC, _id="unpinned", position_ticket="")
    coll, events = _wire_mongo(monkeypatch, [doc])

    ok = await cl.update_comprehensive_trade_exit(
        symbol="USTEC", exit_price=29800.0, exit_reason="TIMEOUT", pnl=1.0,
        position_ticket="2211564328",
    )

    assert ok is True
    assert coll.docs[0]["trade_status"] == "closed"
    assert len(events) == 1


@pytest.mark.asyncio
async def test_exact_ticket_match_is_unaffected(monkeypatch):
    """Ticket-matched closes must not be touched by the new guard."""
    doc = dict(_STALE_USTEC, _id="live", position_ticket="2211564328",
               entry_price=28579.57)
    coll, _ = _wire_mongo(monkeypatch, [doc])

    ok = await cl.update_comprehensive_trade_exit(
        symbol="USTEC", exit_price=28534.98, exit_reason="MT5_SL", pnl=3.12,
        position_ticket="2211564328",
    )

    assert ok is True
    assert coll.docs[0]["exit_price"] == 28534.98


# ---------------------------------------------------------------- BUG-2 ----

def test_active_asset_registry_tracks_and_releases():
    tm._ACTIVE_ASSETS.clear()
    assert tm.is_asset_managed("USTEC") is False

    tm._ACTIVE_ASSETS[tm._asset_key("USTEC")] = "2211564328"
    assert tm.is_asset_managed("USTEC") is True
    assert tm.managed_ticket_for("USTEC") == "2211564328"
    # broker suffixes must resolve to the same key
    assert tm.is_asset_managed("USTECm") is True

    tm._ACTIVE_ASSETS.clear()
    assert tm.is_asset_managed("USTEC") is False


def test_asset_key_normalisation():
    assert tm._asset_key("xauusd") == "XAUUSD"
    assert tm._asset_key("XAUUSDm") == "XAUUSD"
    assert tm._asset_key("XAUUSD.raw") == "XAUUSD"
    assert tm._asset_key("USTECm") == "USTEC"
    assert tm._asset_key("USOILm") == "USOIL"
    assert tm._asset_key(None) == ""
    # no base symbol in the roster ends in M, so none is mangled
    for base in ("XAUUSD", "XAGUSD", "BTCUSD", "EURUSD", "GBPUSD", "USTEC", "USOIL"):
        assert tm._asset_key(base) == base
        assert tm._asset_key(base.lower() + "m") == base


@pytest.mark.asyncio
async def test_queued_trade_registers_asset_then_releases():
    """The real path: queue_trade → _run marks the asset → task end clears it."""
    tm._ACTIVE_TICKETS.clear()
    tm._ACTIVE_ASSETS.clear()

    mon = tm.TradeMonitor.__new__(tm.TradeMonitor)
    mon._queue = asyncio.Queue()
    mon._monitor_tasks = set()
    mon._shutdown = False
    mon._shutdown_event = asyncio.Event()

    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_monitor(item):
        started.set()
        await release.wait()

    mon._monitor = fake_monitor
    mon._task = asyncio.create_task(mon._run())

    mon.queue_trade(
        symbol="USTEC", trade_state={"asset": "USTEC", "entry": 28579.57},
        rules={}, expiry=datetime.now(timezone.utc) + timedelta(hours=4),
        position_ticket="2211564328",
    )
    await asyncio.wait_for(started.wait(), timeout=2)

    assert tm.is_asset_managed("USTEC") is True
    assert tm.managed_ticket_for("USTEC") == "2211564328"

    release.set()
    await asyncio.sleep(0.05)
    assert tm.is_asset_managed("USTEC") is False   # released on task end

    mon._shutdown = True
    mon._task.cancel()
    try:
        await mon._task
    except asyncio.CancelledError:
        pass
    tm._ACTIVE_TICKETS.clear()
    tm._ACTIVE_ASSETS.clear()


def test_runner_lp13_branch_uses_the_process_global_guard_and_detaches():
    """Structural pin for the BUG-2 site.

    `AgentRunner.run` is ~2300 lines, so the LP-13 branch cannot be unit-called.
    What actually broke on 07-27 was the *guard expression* and the missing
    detach, and both are checkable on the AST — cheaply, and without pinning
    unrelated wording.
    """
    import ast
    import pathlib

    src = pathlib.Path("apps/backend/agents/runner.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    branch = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        names = {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
        if "is_asset_managed" in names:
            branch = node
            break

    assert branch is not None, (
        "the LP-13 eager-init must be guarded by is_asset_managed() — a fresh "
        "AgentRunner is built per cycle, so _trade_monitor_initialized is "
        "always False and can never gate it (07-27: 7 spurious monitors)"
    )

    body = ast.dump(ast.Module(body=branch.body, type_ignores=[]))
    assert "_initialize_trade_monitor" in body
    # Must detach, or cleanup_resources() (runs after EVERY cycle) cancels the
    # recovery task and re-orphans the position ~5s later.
    assert "_trade_monitor" in body and "_trade_monitor_initialized" in body

    attrs = {
        t.attr
        for stmt in branch.body
        for n in ast.walk(stmt)
        if isinstance(n, ast.Assign)
        for t in n.targets
        if isinstance(t, ast.Attribute)
    }
    assert {"_trade_monitor", "_trade_monitor_initialized"} <= attrs, (
        "the branch must null out _trade_monitor and reset the flag after init"
    )


# ---------------------------------------------------------------- BUG-3 ----

def _deal(position_id, magic=234000, ts=None, price=4517.5, profit=-3.0):
    return SimpleNamespace(
        _asdict=lambda: {
            "entry": 1, "position_id": position_id, "magic": magic,
            "reason": 4, "price": price, "volume": 0.01, "profit": profit,
            "time": (ts or datetime(2026, 7, 27, 4, 0, tzinfo=timezone.utc)).timestamp(),
        }
    )


def _broker_with_deals(deals):
    broker = MT5Adapter.__new__(MT5Adapter)
    broker._mt5 = SimpleNamespace(history_deals_get=lambda a, b, group=None: deals)
    broker._normalize_symbol = lambda s: s
    return broker


def test_get_last_exit_deal_survives_non_numeric_ticket():
    """The literal 07-27 crash input must resolve instead of raising."""
    broker = _broker_with_deals([_deal(999)])

    out = broker.get_last_exit_deal("XAUUSD", "XAUUSD:4517.5", 96.0)

    assert out is not None
    assert out["position_id"] == 999
    assert out["magic"] == 234000


def test_get_last_exit_deal_magic_and_time_filters():
    manual = _deal(111, magic=0)
    app_old = _deal(222, ts=datetime(2026, 7, 20, 1, 0, tzinfo=timezone.utc))
    app_new = _deal(333, ts=datetime(2026, 7, 27, 5, 0, tzinfo=timezone.utc))
    broker = _broker_with_deals([app_old, app_new, manual])

    # magic filter skips the manual close even though it is the most recent
    assert broker.get_last_exit_deal("XAUUSD", None, 96.0, magic=234000)["position_id"] == 333
    # not_before skips app_old
    got = broker.get_last_exit_deal(
        "XAUUSD", None, 96.0, magic=234000,
        not_before=datetime(2026, 7, 25, tzinfo=timezone.utc),
    )
    assert got["position_id"] == 333
    # everything filtered out ⇒ None, never a wrong deal
    assert broker.get_last_exit_deal(
        "XAUUSD", None, 96.0, magic=234000,
        not_before=datetime(2026, 7, 28, tzinfo=timezone.utc),
    ) is None


@pytest.mark.asyncio
async def test_recovery_with_legacy_ticket_records_exit_by_broker_position(monkeypatch):
    """The XAUUSD:4517.5 row must now produce a real synthetic exit."""
    broker = MT5Adapter.__new__(MT5Adapter)
    broker._mt5 = SimpleNamespace(positions_get=lambda symbol=None: [])
    broker._normalize_symbol = lambda s: s

    seen = {}

    def fake_deal(sym, ticket=None, lookback_hours=24.0, magic=None, not_before=None):
        seen.update(ticket=ticket, magic=magic, not_before=not_before,
                    lookback=lookback_hours)
        return {"price": 4517.5, "profit": -3.04, "reason": "SL",
                "time": datetime(2026, 7, 24, 20, 0, tzinfo=timezone.utc),
                "position_id": 2209999999, "magic": 234000}

    broker.get_last_exit_deal = fake_deal

    mon = tm.TradeMonitor.__new__(tm.TradeMonitor)
    mon.broker = broker
    mon._owner_asset = "XAUUSD"

    row = {
        "asset": "XAUUSD", "symbol": "XAUUSD",
        "position_ticket": "XAUUSD:4517.5",     # the legacy composite key
        "trade_id": "legacy1",
        "created_at": datetime(2026, 7, 24, 17, 35, tzinfo=timezone.utc),
        "trade_state": {"entry": 4517.5, "direction": "long"},
    }

    cleared, calls = [], {}

    async def fake_states():
        return [dict(row)]

    async def fake_clear(ticket, reason):
        cleared.append((ticket, reason))

    async def fake_update(**kw):
        calls.update(kw)
        return True

    monkeypatch.setattr(tm, "get_open_monitor_states", fake_states)
    monkeypatch.setattr(tm, "clear_monitor_state", fake_clear)
    monkeypatch.setattr(cl, "update_comprehensive_trade_exit", fake_update)
    tm._RECOVERED_ASSETS.discard("XAUUSD")

    await mon._recover_open_trades()

    # no crash, and the exit was actually recorded
    assert calls["exit_reason"] == "RECOVERED_GONE"
    assert calls["pnl"] == -3.04
    # the non-numeric key was dropped and the lookup constrained instead
    assert seen["ticket"] is None
    assert seen["magic"] == 234000
    assert seen["not_before"] == row["created_at"]
    # linkage uses the ticket the BROKER reported, so the guarded symbol
    # fallback is never needed
    assert calls["position_ticket"] == "2209999999"
    assert ("XAUUSD:4517.5", "RECOVERED_GONE") in cleared
    tm._RECOVERED_ASSETS.discard("XAUUSD")


@pytest.mark.asyncio
async def test_numeric_ticket_still_uses_exact_position_match(monkeypatch):
    """Regression guard: normal rows must keep exact ticket linkage."""
    broker = MT5Adapter.__new__(MT5Adapter)
    broker._mt5 = SimpleNamespace(positions_get=lambda symbol=None: [])
    broker._normalize_symbol = lambda s: s

    seen = {}

    def fake_deal(sym, ticket=None, lookback_hours=24.0, magic=None, not_before=None):
        seen.update(ticket=ticket, magic=magic, not_before=not_before)
        return {"price": 1.1393, "profit": -6.12, "reason": "SL",
                "time": datetime(2026, 7, 24, 7, 9, tzinfo=timezone.utc),
                "position_id": 2209415879}

    broker.get_last_exit_deal = fake_deal

    mon = tm.TradeMonitor.__new__(tm.TradeMonitor)
    mon.broker = broker
    mon._owner_asset = "EURUSD"

    async def fake_states():
        return [{"asset": "EURUSD", "symbol": "EURUSD",
                 "position_ticket": "2209415879", "trade_id": "abc",
                 "trade_state": {"entry": 1.13828}}]

    async def fake_clear(t, r):
        return None

    calls = {}

    async def fake_update(**kw):
        calls.update(kw)
        return True

    monkeypatch.setattr(tm, "get_open_monitor_states", fake_states)
    monkeypatch.setattr(tm, "clear_monitor_state", fake_clear)
    monkeypatch.setattr(cl, "update_comprehensive_trade_exit", fake_update)
    tm._RECOVERED_ASSETS.discard("EURUSD")

    await mon._recover_open_trades()

    assert seen["ticket"] == "2209415879"
    # exact ticket ⇒ plain positional call, no fuzzy narrowing kwargs at all
    assert seen["magic"] is None
    assert seen["not_before"] is None
    assert calls["position_ticket"] == "2209415879"
    tm._RECOVERED_ASSETS.discard("EURUSD")
