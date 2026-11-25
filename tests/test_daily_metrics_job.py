from datetime import date, datetime, timezone, timedelta
from typing import Any

import pytest

from apps.backend.jobs import daily_metrics


@pytest.mark.asyncio
async def test_daily_metrics_job_idempotent(monkeypatch):
    async def fake_aggregate(asset, period):
        assert asset == "AAA"
        assert period == timedelta(days=1)
        return {
            "pnl": 1.0,
            "win_rate": 0.5,
            "risk_reward": 2.0,
            "latency": 10.0,
            "token_usage": 3.0,
        }

    store: dict[str, Any] = {}

    async def fake_insert(docs):
        for d in docs:
            store[d.client_order_id] = d

    monkeypatch.setattr(daily_metrics, "aggregate_agent_metrics", fake_aggregate)
    monkeypatch.setattr(daily_metrics, "insert_daily_metrics", fake_insert)

    day = date(2024, 1, 1)
    await daily_metrics.run(day=day, assets=["AAA"])
    assert len(store) == 5
    for doc in store.values():
        assert doc.timestamp == datetime(2024, 1, 1, tzinfo=timezone.utc)
        assert doc.metric.startswith("AAA.")

    # Running again should not add new keys
    await daily_metrics.run(day=day, assets=["AAA"])
    assert len(store) == 5
