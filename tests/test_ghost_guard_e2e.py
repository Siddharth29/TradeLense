"""End-to-end tests for the ghost-position guardian.

Unlike test_ghost_guard.py (pure detection + a stub adapter), these drive the
REAL ``MT5Adapter.positions_raw()`` / ``close_position_by_ticket()`` against a
fake MetaTrader5 module, and run the real ``sweep_ghosts()`` / ``ghost_guard_loop()``
on top — so the full path is covered: loop → arming gate → sweep → adapter →
broker ``order_send``. A manual/foreign position must be force-closed; an app
position (our magic) must never be touched; and nothing fires unless armed.
"""

import asyncio

import pytest

from packages.adapters.mt5_adapter import MT5Adapter
from packages.core.config import MT5_MAGIC_NUMBER
from packages.core.risk import ghost_guard
from packages.core.risk.ghost_guard import sweep_ghosts, ghost_guard_loop

APP = int(MT5_MAGIC_NUMBER)
MANUAL = 0          # MetaTrader stamps manual trades with magic 0
DONE = 10009        # TRADE_RETCODE_DONE


class _Rec:
    """Stand-in for an MT5 namedtuple — exposes ``_asdict()`` like real structs."""

    def __init__(self, **d):
        self._d = d

    def _asdict(self):
        return dict(self._d)


class FakeMT5:
    """Minimal fake of the MetaTrader5 module surface MT5Adapter touches."""

    TRADE_ACTION_DEAL = 1
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_IOC = 0

    def __init__(self, positions):
        self._positions = {int(p["ticket"]): dict(p) for p in positions}
        self.sent = []  # recorded order_send requests

    def positions_get(self, symbol=None, ticket=None):
        if ticket is not None:
            p = self._positions.get(int(ticket))
            return [_Rec(**p)] if p else []
        return [_Rec(**p) for p in self._positions.values()]

    def symbol_info_tick(self, symbol):
        return _Rec(bid=100.0, ask=100.5)

    def order_send(self, request):
        self.sent.append(dict(request))
        # Simulate the broker filling the close: the position disappears.
        self._positions.pop(int(request.get("position", 0)), None)
        return _Rec(retcode=DONE, comment="done")


def _adapter(positions):
    a = MT5Adapter()
    a._mt5 = FakeMT5(positions)
    a._reconnect_if_needed = lambda: None  # already "connected"
    a.is_connected = lambda: True
    return a


# ── real adapter methods ─────────────────────────────────────────────────────

def test_positions_raw_exposes_magic_and_ticket():
    a = _adapter([
        {"ticket": 1, "symbol": "BTCUSD", "magic": APP, "volume": 0.1, "type": 1},
        {"ticket": 2, "symbol": "EURUSD", "magic": MANUAL, "volume": 0.2, "type": 0},
    ])
    by_ticket = {p["ticket"]: p for p in a.positions_raw()}
    assert by_ticket[1]["magic"] == APP
    assert by_ticket[2]["magic"] == MANUAL


def test_close_by_ticket_sends_counter_order():
    a = _adapter([
        {"ticket": 555, "symbol": "EURUSD", "magic": MANUAL, "volume": 0.2, "type": 1},  # SELL
    ])
    assert a.close_position_by_ticket(555) is True
    sent = a._mt5.sent
    assert len(sent) == 1
    req = sent[0]
    assert req["position"] == 555
    assert req["volume"] == 0.2
    assert req["type"] == FakeMT5.ORDER_TYPE_BUY        # counter of a SELL
    assert req["action"] == FakeMT5.TRADE_ACTION_DEAL


def test_close_by_ticket_already_gone_is_ok():
    a = _adapter([])  # nothing open
    assert a.close_position_by_ticket(999) is True
    assert a._mt5.sent == []


# ── full sweep through the real adapter ──────────────────────────────────────

@pytest.mark.asyncio
async def test_sweep_kills_manual_spares_app_through_real_adapter():
    a = _adapter([
        {"ticket": 1, "symbol": "BTCUSD", "magic": APP, "volume": 0.1, "type": 1},     # ours
        {"ticket": 2, "symbol": "EURUSD", "magic": MANUAL, "volume": 0.2, "type": 0},  # ghost
    ])
    acted = await sweep_ghosts(a, dry_run=False)
    closed = [r["position"] for r in a._mt5.sent]
    assert closed == [2]                 # only the manual position
    assert [x["ticket"] for x in acted] == [2]
    assert 1 in a._mt5._positions        # app survives
    assert 2 not in a._mt5._positions    # ghost gone


@pytest.mark.asyncio
async def test_sweep_dry_run_through_real_adapter_closes_nothing():
    a = _adapter([
        {"ticket": 2, "symbol": "EURUSD", "magic": MANUAL, "volume": 0.2, "type": 0},
    ])
    acted = await sweep_ghosts(a, dry_run=True)
    assert a._mt5.sent == []             # nothing sent to broker
    assert 2 in a._mt5._positions        # still open
    assert [x["ticket"] for x in acted] == [2]


# ── full loop with the live-arming gate ──────────────────────────────────────

@pytest.mark.asyncio
async def test_loop_inert_when_not_armed(monkeypatch):
    a = _adapter([
        {"ticket": 2, "symbol": "EURUSD", "magic": MANUAL, "volume": 0.2, "type": 0},
    ])
    monkeypatch.setattr(ghost_guard, "live_trading_allowed", lambda: False)
    monkeypatch.setattr(ghost_guard, "MT5_GHOST_KILL_STARTUP_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(ghost_guard, "MT5_GHOST_KILL_INTERVAL_SECONDS", 0.2)

    task = asyncio.create_task(ghost_guard_loop(a))
    await asyncio.sleep(1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert a._mt5.sent == []             # unarmed → nothing killed
    assert 2 in a._mt5._positions


@pytest.mark.asyncio
async def test_loop_kills_when_armed(monkeypatch):
    a = _adapter([
        {"ticket": 1, "symbol": "BTCUSD", "magic": APP, "volume": 0.1, "type": 1},
        {"ticket": 2, "symbol": "EURUSD", "magic": MANUAL, "volume": 0.2, "type": 0},
    ])
    monkeypatch.setattr(ghost_guard, "live_trading_allowed", lambda: True)
    monkeypatch.setattr(ghost_guard, "MT5_GHOST_KILL_STARTUP_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(ghost_guard, "MT5_GHOST_KILL_INTERVAL_SECONDS", 0.2)

    task = asyncio.create_task(ghost_guard_loop(a))
    for _ in range(60):                  # wait (≤3s) until the ghost is closed
        await asyncio.sleep(0.05)
        if a._mt5.sent:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    closed = [r["position"] for r in a._mt5.sent]
    assert 2 in closed                   # manual killed
    assert 1 not in closed               # app spared
    assert 2 not in a._mt5._positions
    assert 1 in a._mt5._positions
