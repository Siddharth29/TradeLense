from __future__ import annotations

"""Repository helpers for agent run persistence."""

from datetime import datetime
from typing import Any, Mapping

from packages.core.config import MONGO_DB_NAME
from packages.db.client import get_client
from packages.db.models import AgentRunDoc


async def insert_agent_run(run: AgentRunDoc) -> Any:
    """Insert *run* into the agent_runs collection."""
    client = get_client()
    coll = client[MONGO_DB_NAME]["agent_runs"]
    result = await coll.insert_one(run.model_dump())
    return result.inserted_id


async def get_agent_runs(
    asset: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 0,
) -> list[Mapping[str, Any]]:
    """Return agent run documents for *asset* within optional time range."""
    client = get_client()
    coll = client[MONGO_DB_NAME]["agent_runs"]
    filt: dict[str, Any] = {"asset": asset}
    if start or end:
        time_cond: dict[str, Any] = {}
        if start:
            time_cond["$gte"] = start
        if end:
            time_cond["$lt"] = end
        filt["time"] = time_cond
    cursor = coll.find(filt, {"_id": 0}).sort("time", 1)
    if limit:
        cursor = cursor.limit(limit)
    return await cursor.to_list(length=None)


__all__ = ["insert_agent_run", "get_agent_runs"]
