"""Tests for the ghost-position guardian (packages/core/risk/ghost_guard.py).

Covers the ownership decision (magic-based), symbol scoping, and the sweep
behaviour: dry-run never closes, armed closes ghosts by ticket, app positions are
never touched, and a single close failure does not block the rest.
"""

import pytest

from packages.core.config import MT5_MAGIC_NUMBER
from packages.core.risk import ghost_guard
from packages.core.risk.ghost_guard import (
    is_ghost,
    find_ghosts,
    sweep_ghosts,
    allowed_magics,
)

APP = int(MT5_MAGIC_NUMBER)
MANUAL = 0          # MetaTrader stamps manual trades with magic 0
OTHER_BOT = 999999


def _pos(ticket, symbol, magic, volume=0.10, type_=0):
    return {"ticket": ticket, "symbol": symbol, "magic": magic, "volume": volume, "type": type_}


# ── ownership decision ───────────────────────────────────────────────────────

def test_app_position_is_never_ghost():
    assert is_ghost(_pos(1, "BTCUSD", APP), {APP}) is False


def test_manual_trade_is_ghost():
    assert is_ghost(_pos(2, "EURUSD", MANUAL), {APP}) is True


def test_other_bot_is_ghost():
    assert is_ghost(_pos(3, "XAUUSD", OTHER_BOT), {APP}) is True


def test_missing_magic_is_spared():
    # Ambiguous → never kill (fails toward sparing our own).
    pos = {"ticket": 4, "symbol": "BTCUSD", "volume": 0.1}
    assert is_ghost(pos, {APP}) is False


def test_unparseable_magic_is_spared():
    assert is_ghost(_pos(5, "BTCUSD", "not-a-number"), {APP}) is False


def test_extra_allowed_magic_is_spared():
    assert is_ghost(_pos(6, "BTCUSD", OTHER_BOT), {APP, OTHER_BOT}) is False


# ── symbol scoping ───────────────────────────────────────────────────────────

def test_symbol_scope_limits_kills():
    pos = _pos(7, "EURUSD", MANUAL)
    # Foreign trade, but EURUSD not in the policed scope → spared.
    assert is_ghost(pos, {APP}, symbols={"BTCUSD", "XAUUSD"}) is False
    # In scope → ghost.
    assert is_ghost(_pos(8, "BTCUSD", MANUAL), {APP}, symbols={"BTCUSD"}) is True


def test_scope_is_case_insensitive():
    assert is_ghost(_pos(9, "btcusd", MANUAL), {APP}, symbols={"BTCUSD"}) is True


def test_find_ghosts_filters_mixed_list():
    positions = [
        _pos(1, "BTCUSD", APP),       # ours
        _pos(2, "EURUSD", MANUAL),    # ghost
        _pos(3, "XAUUSD", OTHER_BOT), # ghost
        _pos(4, "USOIL", APP),        # ours
    ]
    ghosts = find_ghosts(positions, {APP})
    assert sorted(g["ticket"] for g in ghosts) == [2, 3]


def test_allowed_magics_always_includes_app_magic():
    assert APP in allowed_magics()


# ── sweep behaviour ──────────────────────────────────────────────────────────

class FakeAdapter:
    def __init__(self, positions, fail_tickets=None):
        self._positions = positions
        self.closed = []
        self._fail = set(fail_tickets or [])

    def positions_raw(self):
        return list(self._positions)

    def close_position_by_ticket(self, ticket):
        if ticket in self._fail:
            raise RuntimeError(f"broker rejected close for {ticket}")
        self.closed.append(ticket)
        return True


@pytest.mark.asyncio
async def test_sweep_dry_run_detects_but_never_closes():
    adapter = FakeAdapter([
        _pos(1, "BTCUSD", APP),
        _pos(2, "EURUSD", MANUAL),
    ])
    acted = await sweep_ghosts(adapter, dry_run=True)
    assert [a["ticket"] for a in acted] == [2]
    assert adapter.closed == []  # dry-run: nothing actually closed


@pytest.mark.asyncio
async def test_sweep_armed_closes_ghost_and_spares_app():
    adapter = FakeAdapter([
        _pos(1, "BTCUSD", APP),       # must NOT be closed
        _pos(2, "EURUSD", MANUAL),    # must be closed
        _pos(3, "XAUUSD", OTHER_BOT), # must be closed
    ])
    acted = await sweep_ghosts(adapter, dry_run=False)
    assert sorted(adapter.closed) == [2, 3]
    assert 1 not in adapter.closed
    assert sorted(a["ticket"] for a in acted) == [2, 3]


@pytest.mark.asyncio
async def test_sweep_one_failure_does_not_block_others():
    adapter = FakeAdapter(
        [
            _pos(2, "EURUSD", MANUAL),
            _pos(3, "XAUUSD", MANUAL),
        ],
        fail_tickets={2},
    )
    acted = await sweep_ghosts(adapter, dry_run=False)
    # Ticket 2 failed to close, but 3 still got killed.
    assert adapter.closed == [3]
    assert [a["ticket"] for a in acted] == [3]


@pytest.mark.asyncio
async def test_sweep_handles_positions_fetch_error():
    class Broken:
        def positions_raw(self):
            raise RuntimeError("link down")

    acted = await sweep_ghosts(Broken(), dry_run=False)
    assert acted == []  # never raises, no action


@pytest.mark.asyncio
async def test_sweep_noop_when_no_positions():
    adapter = FakeAdapter([_pos(1, "BTCUSD", APP)])
    acted = await sweep_ghosts(adapter, dry_run=False)
    assert acted == []
    assert adapter.closed == []
