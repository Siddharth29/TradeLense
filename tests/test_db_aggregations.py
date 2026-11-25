import pytest
from datetime import datetime, timezone, timedelta

from packages.db.repositories import aggregations


class _FakeCursor:
    def __init__(self, docs):
        self.docs = docs

    async def to_list(self, length=None):
        return self.docs


class _FakeCollection:
    def __init__(self, docs):
        self.docs = docs
        self.last_pipeline = None

    def aggregate(self, pipeline):
        self.last_pipeline = pipeline
        match = pipeline[0]["$match"]
        start = match["time"]["$gte"]
        asset = match["asset"]
        rows = [d for d in self.docs if d["asset"] == asset and d["time"] >= start]
        pnl = sum(d["pnl"] for d in rows)
        wins = sum(1 for d in rows if d["pnl"] > 0)
        losses = sum(1 for d in rows if d["pnl"] < 0)
        rr = sum(d["risk_to_reward"] for d in rows) / len(rows) if rows else 0.0
        lat = sum(d["latency_ms"] for d in rows) / len(rows) if rows else None
        tokens = sum(d["token_spend"] for d in rows)
        out = [{
            "pnl": pnl,
            "wins": wins,
            "losses": losses,
            "risk_reward": rr,
            "latency": lat,
            "token_usage": tokens,
        }]
        return _FakeCursor(out)


@pytest.mark.asyncio
async def test_aggregate_agent_metrics(monkeypatch):
    docs = [
        {
            "asset": "AAA",
            "time": datetime(2024, 1, 1, tzinfo=timezone.utc),
            "pnl": 5.0,
            "risk_to_reward": 2.0,
            "latency_ms": 10.0,
            "token_spend": 100.0,
        },
        {
            "asset": "AAA",
            "time": datetime(2024, 1, 2, tzinfo=timezone.utc),
            "pnl": -2.0,
            "risk_to_reward": 1.0,
            "latency_ms": 20.0,
            "token_spend": 50.0,
        },
        {
            "asset": "BBB",
            "time": datetime(2024, 1, 2, tzinfo=timezone.utc),
            "pnl": 3.0,
            "risk_to_reward": 1.5,
            "latency_ms": 5.0,
            "token_spend": 10.0,
        },
    ]
    coll = _FakeCollection(docs)
    client = {aggregations.MONGO_DB_NAME: {"logs": coll}}
    monkeypatch.setattr(aggregations, "get_client", lambda: client)

    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2024, 1, 3, tzinfo=timezone.utc)

    monkeypatch.setattr(aggregations, "datetime", _Now)

    result = await aggregations.aggregate_agent_metrics("AAA", timedelta(days=2))
    assert result["pnl"] == pytest.approx(3.0)
    assert result["win_rate"] == pytest.approx(0.5)
    assert result["risk_reward"] == pytest.approx(1.5)
    assert result["latency"] == pytest.approx(15.0)
    assert result["token_usage"] == pytest.approx(150.0)
    # ensure pipeline used $match and $group
    assert coll.last_pipeline[0]["$match"]["asset"] == "AAA"
    assert "$group" in coll.last_pipeline[1]
