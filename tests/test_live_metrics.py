"""Tests for the live-metrics endpoints and supporting helpers.

Covers the additive dashboard work: corrected agent P&L (realized + unrealized),
the today-scoped ``/metrics`` assembly, and the small pure helpers. Mongo is not
required — repo functions are monkeypatched at their source module so the
endpoint wiring is exercised in isolation.
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import apps.backend.api as api
from apps.backend.routers import mt5 as mt5_router
from packages.core import metrics as core_metrics
from packages.db.repositories import comprehensive_logging as comp
from packages.db.repositories import equity_samples as eq
from packages.db.repositories import gpt_responses as gptr


def test_ist_day_start_is_utc_aware_and_past():
    start = api._ist_day_start_utc()
    assert start.tzinfo is not None
    assert start <= datetime.now(timezone.utc)


def test_position_asset_mapping():
    assets = {"BTCUSD", "XAUUSD"}
    assert api._position_asset("BTCUSD", assets) == "BTCUSD"
    assert api._position_asset("BTCUSDm", assets) == "BTCUSD"  # trial 'm' suffix
    assert api._position_asset("XAUUSD.r", assets) == "XAUUSD"  # startswith
    assert api._position_asset("EURUSD", assets) == "EURUSD"  # unknown → passthrough


def test_metrics_system_assembles_today_data(monkeypatch):
    now = datetime.now(timezone.utc)

    async def fake_samples(since, end=None):
        return [
            {"timestamp": now, "equity": 1000.0, "balance": 1000.0},
            {"timestamp": now, "equity": 1010.0, "balance": 1000.0},
        ]

    async def fake_curve(since):
        return [{"time": now, "pnl": 5.0}, {"time": now, "pnl": 8.0}]

    async def fake_agg(asset=None, since=None):
        return {"BTCUSD": {"realized_pnl": 8.0, "wins": 2, "losses": 1, "trades": 3}}

    async def fake_tokens(since=None):
        return 1234.0

    monkeypatch.setattr(eq, "get_equity_samples", fake_samples)
    monkeypatch.setattr(comp, "get_daily_pnl_curve", fake_curve)
    monkeypatch.setattr(comp, "aggregate_realized_pnl", fake_agg)
    monkeypatch.setattr(gptr, "aggregate_token_spend", fake_tokens)

    with TestClient(api.app) as c:
        r = c.get("/metrics")

    assert r.status_code == 200
    data = r.json()
    assert len(data["equity_curve"]) == 2
    assert data["equity_curve"][1]["equity"] == 1010.0
    assert len(data["daily_pnl"]) == 2
    assert data["daily_pnl"][-1]["pnl"] == 8.0
    assert data["win_rate"] == pytest.approx(2 / 3)
    assert data["token_spend"] == 1234.0


def test_metrics_agents_realized_plus_unrealized(monkeypatch):
    core_metrics.reset()

    async def fake_agg(asset=None, since=None):
        return {"BTCUSD": {"realized_pnl": 100.0, "wins": 3, "losses": 1, "trades": 4}}

    monkeypatch.setattr(comp, "aggregate_realized_pnl", fake_agg)
    # Live position with +25 unrealized on the trial-suffixed symbol.
    monkeypatch.setattr(
        mt5_router.adapter,
        "positions_raw",
        lambda: [{"symbol": "BTCUSDm", "profit": 25.0}],
    )
    monkeypatch.setitem(api.supervisor._cfg, "BTCUSD", {"agent_id": "a1", "intervalSec": 60})

    with TestClient(api.app) as c:  # triggers app startup
        r = c.get("/metrics/agents")

    assert r.status_code == 200
    btc = r.json()["BTCUSD"]
    assert btc["realized_pnl"] == 100.0
    assert btc["unrealized_pnl"] == 25.0
    assert btc["pnl"] == 125.0  # realized + unrealized, NOT the broken price-diff sum
    assert btc["wins"] == 3 and btc["losses"] == 1


def test_aggregate_realized_pnl_empty_when_persistence_disabled(monkeypatch):
    monkeypatch.setattr(comp, "analytics_persistence_enabled", lambda: False)
    import asyncio

    result = asyncio.run(comp.aggregate_realized_pnl())
    assert result == {}


def test_insert_equity_sample_logonly_is_noop_ok(monkeypatch):
    """In log-only mode the sampler still 'succeeds' (durable event only)."""
    monkeypatch.setattr(eq, "analytics_persistence_enabled", lambda: False)
    import asyncio

    ok = asyncio.run(eq.insert_equity_sample("mt5", 1000.0, 1000.0))
    assert ok is True


def test_trading_analysis_surfaces_sl_trail(monkeypatch):
    """The trading-analysis endpoint exposes the per-trade SL-trail progression."""
    at = datetime.now(timezone.utc)
    trade = {
        "_id": "abc123",
        "asset": "BTCUSD",
        "symbol": "BTCUSDm",
        "side": "buy",
        "entry_price": 100.0,
        "exit_price": 108.0,
        "stop_loss": 95.0,          # initial SL
        "take_profit": 110.0,
        "sl_trail": [               # the trailed progression added this session
            {"sl": 100.0, "label": "BE+0.5R", "at": at},
            {"sl": 104.0, "label": "trail-1", "at": at},
        ],
        "quantity": 0.01,
        "pnl": 8.0,
        "trade_status": "closed",
        "exit_reason": "MT5_SL",
        "trade_type": "live",
        # No times / cycle_id → no decision-flow lookup needed for this test.
        "trade_executed_at": None,
        "signal_received_at": None,
    }

    async def fake_all(start_time=None, end_time=None, limit=50):
        return [trade]

    monkeypatch.setattr(comp, "get_all_comprehensive_trades", fake_all)

    with TestClient(api.app) as c:
        r = c.get("/api/trading-analysis/trades")

    assert r.status_code == 200
    rows = r.json()["trades"]
    assert len(rows) == 1
    row = rows[0]
    assert row["stop_loss"] == 95.0  # initial SL preserved
    assert [s["sl"] for s in row["sl_trail"]] == [100.0, 104.0]
    assert row["sl_trail"][1]["label"] == "trail-1"
    assert row["sl_trail"][0]["at"] is not None  # datetime serialized to ISO string


def test_trading_analysis_passes_date_window_to_repo(monkeypatch):
    """start/end query params flow through to the repo as datetimes."""
    captured = {}

    async def fake_all(start_time=None, end_time=None, limit=50):
        captured["start"] = start_time
        captured["end"] = end_time
        return []

    monkeypatch.setattr(comp, "get_all_comprehensive_trades", fake_all)

    with TestClient(api.app) as c:
        r = c.get(
            "/api/trading-analysis/trades",
            params={"start": "2026-06-01T00:00:00Z", "end": "2026-06-02T00:00:00Z"},
        )

    assert r.status_code == 200
    assert isinstance(captured["start"], datetime)
    assert isinstance(captured["end"], datetime)
    assert captured["start"].year == 2026 and captured["start"].day == 1


def test_purge_all_trade_data_deletes_every_collection(monkeypatch):
    """The purge endpoint wipes every trade-lifecycle collection and totals them."""
    deleted_from = []

    class _FakeColl:
        def __init__(self, name):
            self.name = name

        async def delete_many(self, filt):
            assert filt == {}
            deleted_from.append(self.name)
            return type("R", (), {"deleted_count": 5})()

    class _FakeDB:
        def __getitem__(self, name):
            return _FakeColl(name)

    monkeypatch.setattr("packages.db.client.get_client", lambda: {api_module_db_name(): _FakeDB()})

    with TestClient(api.app) as c:
        r = c.delete("/api/trading-analysis/purge")

    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] is True
    assert body["deleted_count"] == 15  # 3 collections × 5
    assert set(body["by_collection"]) == {"comprehensive_trades", "multi_expert_cycles", "active_trades"}


def api_module_db_name() -> str:
    from packages.core.config import MONGO_DB_NAME
    return MONGO_DB_NAME


@pytest.mark.asyncio
async def test_equity_sampler_loop_samples_and_persists(monkeypatch):
    """One real iteration of the background sampler records equity (live mode)."""
    import asyncio as _asyncio
    from types import SimpleNamespace
    import apps.backend.routers.mt5 as mt5r

    app = SimpleNamespace(state=SimpleNamespace())
    monkeypatch.setattr(mt5r, "EQUITY_SAMPLE_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(mt5r.adapter, "is_connected", lambda: True)
    monkeypatch.setattr(mt5r.adapter, "balances", lambda: {"equity": 1000.0, "cash": 990.0})

    captured = []

    async def fake_insert(broker, equity, balance=None, ts=None):
        captured.append((broker, equity, balance))
        return True

    monkeypatch.setattr(
        "packages.db.repositories.equity_samples.insert_equity_sample", fake_insert
    )

    task = _asyncio.create_task(mt5r._equity_sampler_loop(app))
    await _asyncio.sleep(0.05)  # allow a couple of iterations
    task.cancel()
    try:
        await task
    except _asyncio.CancelledError:
        pass

    assert captured, "sampler should have recorded at least one equity sample"
    assert captured[0][1] == 1000.0  # equity value
    assert list(app.state.equity_samples_mem)  # in-memory ring buffer populated
