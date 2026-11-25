from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from mongomock_motor import AsyncMongoMockClient

from packages.adapters.mt5_adapter import MT5Adapter
from packages.db.repositories import comprehensive_logging as trades


def test_account_history_uses_unique_postings_and_ist_boundary():
    midnight = datetime(2026, 11, 23, 18, 30, tzinfo=timezone.utc)
    account = SimpleNamespace(server="Demo", login=42, trade_mode=0, profit=4, currency="USD")

    def deal(ticket, seconds, kind, profit=0, commission=0):
        return SimpleNamespace(ticket=ticket, time_msc=int((midnight.timestamp() + seconds) * 1000),
                               type=kind, profit=profit, commission=commission, swap=0, fee=0)

    opening = deal(2, -1, 0, commission=-1)
    history = [deal(1, -5, 2, profit=1000), opening, deal(3, 0, 1, profit=7),
               deal(4, 4, 1, profit=3), opening, deal(5, 5, 7, profit=-1)]
    adapter = MT5Adapter()
    adapter._report_account = ("demo", 42, 0)
    adapter._mt5 = SimpleNamespace(
        account_info=lambda: account, history_deals_get=lambda *_: history,
        DEAL_TYPE_BUY=0, DEAL_TYPE_SELL=1, DEAL_TYPE_COMMISSION=7,
        DEAL_DIVIDEND=17, DEAL_DIVIDEND_FRANKED=18,
    )
    report = adapter.account_pnl_report(midnight)
    assert report["all_time"] == {"gross": 10, "charges": -2, "net": 8}
    assert report["today"] == {"gross": 10, "charges": -1, "net": 9}
    assert [p["pnl"] for p in report["daily_curve"]] == [7, 10, 9]
    assert report["floating"] == 4
    account.login = 43
    with pytest.raises(RuntimeError, match="account unavailable"):
        adapter.account_pnl_report(midnight)


@pytest.mark.asyncio
async def test_reconciled_final_absorbs_linked_partial_across_midnight(monkeypatch):
    db = AsyncMongoMockClient()
    monkeypatch.setattr(trades, "get_client", lambda: db)
    monkeypatch.setattr(trades, "analytics_persistence_enabled", lambda: True)
    start = datetime.now(timezone.utc)
    common = {"asset": "BTCUSD", "trade_status": "closed", "cycle_id": "cycle", "position_ticket": "123"}
    parent = {**common, "_id": "parent", "trade_closed_at": start, "pnl": 9,
              "broker_cost": {"source": "broker_deals", "status": "final"}}
    partial = {**common, "_id": "partial", "trade_closed_at": start - timedelta(seconds=1),
               "exit_reason": "TP1_PARTIAL", "pnl": 4}
    other = {**partial, "_id": "other", "cycle_id": "different", "pnl": 2}
    await db[trades.MONGO_DB_NAME]["comprehensive_trades"].insert_many([parent, partial, other])
    assert trades.covered_partials([parent], [partial, other]) == {"partial": parent}
    totals = await trades.aggregate_realized_pnl()
    assert totals["BTCUSD"] == {"realized_pnl": 11, "wins": 1, "losses": 0, "trades": 1}
    today = await trades.aggregate_realized_pnl(since=start)
    assert today["BTCUSD"]["realized_pnl"] == 5
    curve = await trades.get_daily_pnl_curve(start)
    assert [point["pnl"] for point in curve] == [5]
