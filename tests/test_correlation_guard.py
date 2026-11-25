"""Correlated-instrument coherence guard (2026-07-22).

Motivation: on 2026-07-21 the app held XAUUSD sell + XAGUSD buy twice in one
session, and the user closed the silver leg manually both times (gold won both).
The pre-existing pre-trade guard only looked for positions in the SAME symbol,
so a cross-symbol contradiction was invisible to it.

Covers the pure helpers and the end-to-end block in
``CodeSniperMonitor._execute_trade``, including the traps: unsigned volume
(side is unrecoverable from ``Position``), ``bool`` being an ``int`` subclass,
pending-order type codes, suffix-safe symbols, same-side pairs that must NOT be
blocked, fail-open on broker error, and the single-round-trip latency contract.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from packages.core.models import Decision, Position, PriceType, Side
from packages.core.schemas_trade import CodeSniperSignal
from packages.core.services.code_sniper_monitor import (
    CodeSniperMonitor,
    _CORRELATION_PEERS,
    _opposing_correlated_positions,
    _position_side,
    _position_symbol,
)


@pytest.fixture(autouse=True)
def _outside_news_blackout(monkeypatch):
    """These tests are about the correlation guard, not the news blackout.

    The doubles below build candles from ``datetime.now(timezone.utc)`` and
    ``_execute_trade`` consults ``news_blackout_active()`` on the same wall clock,
    so every test here failed for 75 minutes a day — inside 12:15-12:40,
    13:15-13:40 and 14:15-14:40 UTC (config.NEWS_BLACKOUT_WINDOWS_UTC). A suite
    that only passes outside three windows cannot be used to verify anything.
    The blackout has its own tests; here it is held open.
    """
    monkeypatch.setattr(
        "packages.core.services.code_sniper_monitor.news_blackout_active",
        lambda asset: (False, ""),
    )


# --------------------------------------------------------------------- doubles

class DummyCandleService:
    def __init__(self):
        self._now = datetime.now(timezone.utc)

    async def fetch_candles(self, tf: str, limit: int):
        base = self._now

        class C:
            def __init__(self, t):
                self.time = t
                self.open = 100.0
                self.high = 101.0
                self.low = 99.5
                self.close = 100.5
                self.volume = 100

        return [C(base - timedelta(minutes=5 * (limit - i - 1))) for i in range(limit)]


class DummyRisk:
    def available_funds(self) -> float:
        return 100000.0


class BrokerWithRawPositions:
    """Broker exposing positions_raw() — MT5-shaped dicts carrying ``type``.

    Counts fetches so a test can assert both guards share ONE round-trip.
    """

    def __init__(self, raw_positions):
        self._raw = list(raw_positions)
        self.placed = []
        self.fetch_calls = 0

    def positions_raw(self):
        self.fetch_calls += 1
        return list(self._raw)

    def positions(self):  # pragma: no cover — must never be preferred over raw
        raise AssertionError("positions() must not be used when positions_raw() exists")

    def place(self, decision):
        self.placed.append(decision)

        class Order:
            id = "1"
            status = "filled"
            average_price = decision.entry

        return Order(), []


class LegacyBroker(BrokerWithRawPositions):
    """Older adapter shape: only positions() -> List[Position], no raw accessor."""

    def __init__(self, positions):
        super().__init__([])
        self._positions = list(positions)

    positions_raw = None  # attribute exists but is falsy -> falls back

    def positions(self):
        self.fetch_calls += 1
        return list(self._positions)


def _mt5_pos(symbol, type_code, volume=0.01):
    """One MT5 positions_get() row as positions_raw() returns it (0=BUY, 1=SELL)."""
    return {"symbol": symbol, "type": type_code, "volume": volume,
            "price_open": 4000.0, "magic": 234000, "ticket": 1}


def _hold_signal():
    return CodeSniperSignal(
        code=('def run(candles_5, now):\n'
              '    return {"direction": "hold", "entry": 0, "stop_loss": 0,'
              ' "take_profit_1": 0, "take_profit_2": 0, "confidence": 0.1,'
              ' "rationale": "x"}'),
        input_schema={},
        output_schema={},
        metadata={},
        ttl_minutes=90,
        confidence=0.1,
    )


def _decision(asset, symbol, side, entry=58.0):
    return Decision(
        asset=asset, symbol=symbol, side=side, price_type=PriceType.MARKET,
        entry=entry, quantity=0.01, sl=entry + 0.3, tp1=entry - 0.4, tp=entry - 0.6,
    )


def _monitor(broker, asset, symbol, sid):
    m = CodeSniperMonitor(DummyCandleService(), broker, DummyRisk())
    m.add_strategy(sid, _hold_signal(), asset=asset, symbol=symbol, context={})
    return m, m._strategies[sid]


# ---------------------------------------------------------------- pure helpers

def test_correlation_peer_index_is_symmetric_and_scoped():
    assert _CORRELATION_PEERS["XAUUSD"] == ("XAGUSD",)
    assert _CORRELATION_PEERS["XAGUSD"] == ("XAUUSD",)
    assert "BTCUSD" not in _CORRELATION_PEERS


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({"type": 0}, "buy"),
        ({"type": 1}, "sell"),
        ({"type": 2}, None),                      # pending BUY_LIMIT, not a position
        ({"type": 5}, None),                      # pending stop order
        ({"type": True}, None),                   # bool is an int subclass
        ({"type": "POSITION_TYPE_SELL"}, "sell"),
        ({"side": "buy"}, "buy"),
        ({"side": Side.SELL}, "sell"),
        ({}, None),
    ],
)
def test_position_side_decoding(raw, expected):
    assert _position_side(raw) == expected


def test_side_is_unknowable_from_plain_position_object():
    """Position carries an UNSIGNED volume, so side must come back None.

    Guards the trap: a ``quantity < 0`` implementation compiles, ships, and
    never fires once.
    """
    pos = Position(symbol="XAUUSDm", quantity=0.01, average_price=4000.0)
    assert _position_side(pos) is None
    assert _position_symbol(pos) == "XAUUSDm"


def test_detects_gold_sell_against_silver_buy():
    assert _opposing_correlated_positions("XAGUSD", Side.BUY, [_mt5_pos("XAUUSDm", 1)])


def test_detects_gold_buy_against_silver_sell():
    assert _opposing_correlated_positions("XAGUSD", Side.SELL, [_mt5_pos("XAUUSDm", 0)])


def test_same_side_pair_is_not_a_conflict():
    """Same-side metals are concentration, not contradiction — deliberately allowed."""
    assert _opposing_correlated_positions("XAGUSD", Side.SELL, [_mt5_pos("XAUUSDm", 1)]) == []


@pytest.mark.parametrize("asset,side", [("BTCUSD", Side.BUY), ("", Side.BUY), ("XAGUSD", None)])
def test_no_conflict_for_uncorrelated_or_missing_inputs(asset, side):
    assert _opposing_correlated_positions(asset, side, [_mt5_pos("XAUUSDm", 1)]) == []


@pytest.mark.parametrize("symbol", ["XAUUSD", "XAUUSDm", "XAUUSD.raw"])
def test_conflict_matching_is_suffix_safe(symbol):
    assert _opposing_correlated_positions("XAGUSD", Side.BUY, [_mt5_pos(symbol, 1)])


def test_unknown_side_positions_are_skipped_not_guessed():
    pos = Position(symbol="XAUUSDm", quantity=0.01, average_price=4000.0)
    assert _opposing_correlated_positions("XAGUSD", Side.BUY, [pos]) == []


def test_conflict_scan_ignores_unrelated_symbols_in_the_book():
    book = [_mt5_pos("BTCUSDm", 1), _mt5_pos("USTECm", 1), _mt5_pos("XAUUSDm", 1)]
    conflicts = _opposing_correlated_positions("XAGUSD", Side.BUY, book)
    assert [c["symbol"] for c in conflicts] == ["XAUUSDm"]


# ------------------------------------------------------------ guard end-to-end

@pytest.mark.asyncio
async def test_blocks_silver_buy_against_open_gold_sell():
    """The exact 2026-07-21 11:35 case, and the single-round-trip contract."""
    broker = BrokerWithRawPositions([_mt5_pos("XAUUSDm", 1)])   # gold SELL live
    monitor, strat = _monitor(broker, "XAGUSD", "XAGUSDm", "s_corr")

    await monitor._execute_trade(strat, _decision("XAGUSD", "XAGUSDm", Side.BUY))

    assert broker.placed == [], "opposing correlated entry must be blocked"
    assert "s_corr" in monitor._strategies, "strategy stays registered to retry in TTL"
    assert broker.fetch_calls == 1, "both guards must share ONE broker round-trip"


@pytest.mark.asyncio
async def test_blocks_gold_buy_against_open_silver_sell():
    """Symmetry: the guard is not gold-first."""
    broker = BrokerWithRawPositions([_mt5_pos("XAGUSDm", 1)])   # silver SELL live
    monitor, strat = _monitor(broker, "XAUUSD", "XAUUSDm", "s_rev")

    await monitor._execute_trade(strat, _decision("XAUUSD", "XAUUSDm", Side.BUY, entry=4000.0))

    assert broker.placed == [], "opposing correlated entry must be blocked both ways"


@pytest.mark.asyncio
async def test_allows_silver_sell_alongside_gold_sell():
    broker = BrokerWithRawPositions([_mt5_pos("XAUUSDm", 1)])   # gold SELL live
    monitor, strat = _monitor(broker, "XAGUSD", "XAGUSDm", "s_same")

    await monitor._execute_trade(strat, _decision("XAGUSD", "XAGUSDm", Side.SELL))

    assert len(broker.placed) == 1, "same-side correlated entry must proceed"


@pytest.mark.asyncio
async def test_allows_uncorrelated_asset_against_open_gold():
    broker = BrokerWithRawPositions([_mt5_pos("XAUUSDm", 1)])
    monitor, strat = _monitor(broker, "BTCUSD", "BTCUSDm", "s_btc")

    await monitor._execute_trade(strat, _decision("BTCUSD", "BTCUSDm", Side.BUY, entry=66985.0))

    assert len(broker.placed) == 1, "uncorrelated asset must be unaffected"


@pytest.mark.asyncio
async def test_allows_entry_when_book_is_flat():
    broker = BrokerWithRawPositions([])
    monitor, strat = _monitor(broker, "XAGUSD", "XAGUSDm", "s_flat")

    await monitor._execute_trade(strat, _decision("XAGUSD", "XAGUSDm", Side.BUY))

    assert len(broker.placed) == 1


@pytest.mark.asyncio
async def test_same_asset_guard_still_fires_first():
    """Regression: the pre-existing same-symbol guard must keep working."""
    broker = BrokerWithRawPositions([_mt5_pos("XAGUSDm", 1)])   # silver already open
    monitor, strat = _monitor(broker, "XAGUSD", "XAGUSDm", "s_dup")

    await monitor._execute_trade(strat, _decision("XAGUSD", "XAGUSDm", Side.BUY))

    assert broker.placed == [], "same-asset guard must still block"


@pytest.mark.asyncio
async def test_degrades_gracefully_when_broker_lacks_positions_raw():
    """Legacy adapter: no crash and no FALSE block (side is unknowable)."""
    broker = LegacyBroker([Position(symbol="XAUUSDm", quantity=0.01, average_price=4000.0)])
    monitor, strat = _monitor(broker, "XAGUSD", "XAGUSDm", "s_legacy")

    await monitor._execute_trade(strat, _decision("XAGUSD", "XAGUSDm", Side.BUY))

    assert len(broker.placed) == 1, "degradation, not a false block"
    assert broker.fetch_calls == 1


@pytest.mark.asyncio
async def test_guard_fails_open_when_position_fetch_raises():
    """A transient MT5 hiccup must not freeze trading (pre-existing contract)."""

    class ExplodingBroker(BrokerWithRawPositions):
        def positions_raw(self):
            raise RuntimeError("MT5 IPC hiccup")

    broker = ExplodingBroker([])
    monitor, strat = _monitor(broker, "XAGUSD", "XAGUSDm", "s_boom")

    await monitor._execute_trade(strat, _decision("XAGUSD", "XAGUSDm", Side.BUY))

    assert len(broker.placed) == 1, "guard must fail open, not block"
