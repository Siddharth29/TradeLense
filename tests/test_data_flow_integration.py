"""End-to-end data-flow integration tests against a real (mock) Mongo engine.

Unlike the unit tests (which fake the collection surface), these run the real
MongoDB aggregation pipelines and write→read round-trips through
``mongomock_motor`` — proving the planned storage/retrieval flow is intact:

  * store_comprehensive_trade  → get_comprehensive_trades
  * update_comprehensive_trade_exit → closes the doc
  * append_sl_trail            → sl_trail persisted on the doc
  * aggregate_realized_pnl     → realized money + parent-only win/loss counts
  * get_daily_pnl_curve        → cumulative realized curve
  * insert_equity_sample       → get_equity_samples
  * aggregate_token_spend      → summed tokens
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from mongomock_motor import AsyncMongoMockClient

from packages.core.config import MONGO_DB_NAME
from packages.db.repositories import comprehensive_logging as cl
from packages.db.repositories import equity_samples as eq
from packages.db.repositories import gpt_responses as gptr


@pytest.fixture
def mongo(monkeypatch):
    client = AsyncMongoMockClient()
    # Point every repo at the same in-memory Mongo and force the persistence gate on.
    monkeypatch.setattr(cl, "get_client", lambda: client)
    monkeypatch.setattr(cl, "analytics_persistence_enabled", lambda: True)
    monkeypatch.setattr(eq, "get_client", lambda: client)
    monkeypatch.setattr(eq, "analytics_persistence_enabled", lambda: True)
    monkeypatch.setattr(eq, "_index_ensured", False)
    monkeypatch.setattr(gptr, "_get_client", lambda: client)
    monkeypatch.setattr(gptr, "analytics_persistence_enabled", lambda: True)
    return client


# --------------------------------------------------------------------------
# Realized-P&L aggregation (the core P&L fix)
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_realized_pnl_aggregation_money_and_counts(mongo):
    db = mongo[MONGO_DB_NAME]
    now = datetime.now(timezone.utc)
    await db["comprehensive_trades"].insert_many([
        {"asset": "BTCUSD", "trade_status": "closed", "pnl": 10.0, "parent_trade_id": None, "trade_closed_at": now},
        {"asset": "BTCUSD", "trade_status": "closed", "pnl": -4.0, "parent_trade_id": None, "trade_closed_at": now},
        # Partial child leg: adds to money total but must NOT count as a decision.
        {"asset": "BTCUSD", "trade_status": "closed", "pnl": 3.0, "parent_trade_id": "p1", "trade_closed_at": now},
        # Open trade: excluded entirely.
        {"asset": "XAUUSD", "trade_status": "open", "pnl": None, "parent_trade_id": None, "trade_closed_at": None},
    ])

    out = await cl.aggregate_realized_pnl()

    assert out["BTCUSD"]["realized_pnl"] == 9.0   # 10 - 4 + 3 (every closed leg)
    assert out["BTCUSD"]["wins"] == 1             # parents only
    assert out["BTCUSD"]["losses"] == 1
    assert out["BTCUSD"]["trades"] == 2
    assert "XAUUSD" not in out                     # open trade not aggregated


@pytest.mark.asyncio
async def test_realized_pnl_since_filter(mongo):
    db = mongo[MONGO_DB_NAME]
    now = datetime.now(timezone.utc)
    await db["comprehensive_trades"].insert_many([
        {"asset": "BTCUSD", "trade_status": "closed", "pnl": 5.0, "parent_trade_id": None, "trade_closed_at": now},
        {"asset": "BTCUSD", "trade_status": "closed", "pnl": 99.0, "parent_trade_id": None,
         "trade_closed_at": now - timedelta(days=3)},  # outside window
    ])
    out = await cl.aggregate_realized_pnl(since=now - timedelta(hours=1))
    assert out["BTCUSD"]["realized_pnl"] == 5.0


# --------------------------------------------------------------------------
# Daily-P&L curve
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_daily_pnl_curve_cumulative(mongo):
    db = mongo[MONGO_DB_NAME]
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    await db["comprehensive_trades"].insert_many([
        {"trade_status": "closed", "pnl": 5.0, "trade_closed_at": base},
        {"trade_status": "closed", "pnl": -2.0, "trade_closed_at": base + timedelta(minutes=5)},
        {"trade_status": "closed", "pnl": 4.0, "trade_closed_at": base + timedelta(minutes=10)},
    ])
    pts = await cl.get_daily_pnl_curve(base - timedelta(minutes=1))
    assert [round(p["pnl"], 2) for p in pts] == [5.0, 3.0, 7.0]


# --------------------------------------------------------------------------
# Equity samples write → read
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_equity_samples_write_read(mongo):
    now = datetime.now(timezone.utc)
    await eq.insert_equity_sample("mt5", 1000.0, balance=1000.0, ts=now)
    await eq.insert_equity_sample("mt5", 1012.5, balance=1000.0, ts=now + timedelta(minutes=1))

    rows = await eq.get_equity_samples(now - timedelta(hours=1))
    assert [r["equity"] for r in rows] == [1000.0, 1012.5]   # ordered by time
    assert rows[1]["balance"] == 1000.0


# --------------------------------------------------------------------------
# Token-spend aggregation
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_token_spend_aggregation(mongo):
    db = mongo[MONGO_DB_NAME]
    now = datetime.now(timezone.utc)
    await db["gpt_responses"].insert_many([
        {"tokens_used": 100, "timestamp": now},
        {"tokens_used": 250, "timestamp": now},
        {"tokens_used": None, "timestamp": now},   # null tolerated → 0
    ])
    total = await gptr.aggregate_token_spend(since=now - timedelta(hours=1))
    assert total == 350.0


# --------------------------------------------------------------------------
# Trade store → exit → trail round-trips
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_store_then_get_comprehensive_trade(mongo):
    tid = await cl.store_comprehensive_trade(
        symbol="BTCUSDm", asset="BTCUSD", quantity=0.01, price=65000.0, entry_price=65000.0,
        agent_id="a1", prompt_hash="h1",
        signal_received_at=datetime.now(timezone.utc), trade_executed_at=datetime.now(timezone.utc),
        side="buy", position_ticket="T1",
    )
    assert tid
    rows = await cl.get_comprehensive_trades("BTCUSD")
    assert len(rows) == 1 and rows[0]["symbol"] == "BTCUSDm"
    assert rows[0]["trade_status"] == "open"


@pytest.mark.asyncio
async def test_exit_then_trail_round_trip(mongo):
    db = mongo[MONGO_DB_NAME]
    # Seed an open trade with a known ticket.
    await db["comprehensive_trades"].insert_one({
        "symbol": "BTCUSDm", "asset": "BTCUSD", "position_ticket": "T9",
        "entry_price": 100.0, "stop_loss": 95.0, "trade_status": "open",
        "trade_executed_at": datetime.now(timezone.utc),
    })

    # Trail twice → sl_trail grows, initial stop_loss untouched.
    assert await cl.append_sl_trail("BTCUSDm", 98.0, label="BE", position_ticket="T9") is True
    assert await cl.append_sl_trail("BTCUSDm", 101.0, label="trail-1", position_ticket="T9") is True

    doc = await db["comprehensive_trades"].find_one({"position_ticket": "T9"})
    assert doc["stop_loss"] == 95.0                       # initial SL preserved
    assert [s["sl"] for s in doc["sl_trail"]] == [98.0, 101.0]

    # Close it → aggregation now sees the realized P&L.
    ok = await cl.update_comprehensive_trade_exit(
        symbol="BTCUSDm", exit_price=101.0, exit_reason="MT5_SL", pnl=1.0, position_ticket="T9",
    )
    assert ok is True
    closed = await db["comprehensive_trades"].find_one({"position_ticket": "T9"})
    assert closed["trade_status"] == "closed" and closed["pnl"] == 1.0

    agg = await cl.aggregate_realized_pnl()
    assert agg["BTCUSD"]["realized_pnl"] == 1.0
