from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from packages.core.config import MONGO_DB_NAME
from packages.db.client import get_client


async def aggregate_agent_metrics(asset: str, period: timedelta) -> Mapping[str, Any]:
    """Return aggregated metrics for *asset* over *period*.

    The aggregation uses a MongoDB pipeline with ``$match`` and ``$group`` to
    compute summary statistics such as total PnL and win rate.  ``period`` is
    measured backwards from the current UTC time.
    """

    start = datetime.now(timezone.utc) - period
    client = get_client()
    coll = client[MONGO_DB_NAME]["logs"]

    pipeline = [
        {"$match": {"asset": asset, "time": {"$gte": start}}},
        {
            "$group": {
                "_id": None,
                "pnl": {"$sum": "$pnl"},
                "wins": {"$sum": {"$cond": [{"$gt": ["$pnl", 0]}, 1, 0]}},
                "losses": {"$sum": {"$cond": [{"$lt": ["$pnl", 0]}, 1, 0]}},
                "risk_reward": {"$avg": "$risk_to_reward"},
                "latency": {"$avg": "$latency_ms"},
                "token_usage": {"$sum": "$token_spend"},
            }
        },
    ]

    cursor = coll.aggregate(pipeline)
    docs = await cursor.to_list(length=1)
    if not docs:
        return {"pnl": 0.0, "win_rate": 0.0, "risk_reward": 0.0, "latency": None, "token_usage": 0.0}

    doc = docs[0]
    wins = doc.get("wins", 0) or 0
    losses = doc.get("losses", 0) or 0
    total = wins + losses
    win_rate = wins / total if total else 0.0
    return {
        "pnl": doc.get("pnl", 0.0) or 0.0,
        "win_rate": win_rate,
        "risk_reward": doc.get("risk_reward", 0.0) or 0.0,
        "latency": doc.get("latency"),
        "token_usage": doc.get("token_usage", 0.0) or 0.0,
    }


__all__ = ["aggregate_agent_metrics"]
