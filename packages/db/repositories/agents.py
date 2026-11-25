from __future__ import annotations

"""Repository helpers for agent records.

Agents are stored in a dedicated MongoDB collection and validated with
``AgentRecord``. Only basic identifying information is stored to allow the
frontend to render saved agents even when no runtime task is active.
"""

from typing import List

from motor.motor_asyncio import AsyncIOMotorCollection
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from packages.core.config import MONGO_DB_NAME
from packages.db.client import get_client as _get_client

AGENTS_COLLECTION = "agents"


async def get_collection() -> AsyncIOMotorCollection:
    """Return the agents collection from the shared client."""
    client = _get_client()
    db = client[MONGO_DB_NAME]
    return db[AGENTS_COLLECTION]


class AgentRecord(BaseModel):
    """Persisted agent info used for listing."""

    id: str = Field(alias="_id")
    symbol: str
    candle_count_5m: int = Field(ge=1)
    candle_count_15m: int = Field(ge=1)
    broker: str
    expiry_date: str | None = None
    risk_percent: float = Field(ge=0, le=100)
    run_interval: str
    prompt_id: str = Field(min_length=1, description="Prompt ID is required for agent execution")

    model_config = ConfigDict(populate_by_name=True, extra="forbid", strict=True)


async def list_agents() -> List[AgentRecord]:
    coll = await get_collection()
    cur = coll.find(
        {},
        {
            "_id": 1,
            "symbol": 1,
            "candle_count_5m": 1,
            "candle_count_15m": 1,
            "broker": 1,
            "expiry_date": 1,
            "risk_percent": 1,
            "run_interval": 1,
            "prompt_id": 1,
        },
    )
    docs = await cur.to_list(length=None)
    out: List[AgentRecord] = []
    for doc in docs:
        try:
            rec = AgentRecord.model_validate(doc, strict=True)
            out.append(rec)
        except ValidationError:
            # Skip malformed records instead of raising an exception
            continue
    return out


async def get_agent(agent_id: str) -> AgentRecord | None:
    coll = await get_collection()
    doc = await coll.find_one(
        {"_id": agent_id},
        {
            "_id": 1,
            "symbol": 1,
            "candle_count_5m": 1,
            "candle_count_15m": 1,
            "broker": 1,
            "expiry_date": 1,
            "risk_percent": 1,
            "run_interval": 1,
            "prompt_id": 1,
        },
    )
    if not doc:
        return None
    try:
        return AgentRecord.model_validate(doc, strict=True)
    except ValidationError:
        return None


async def upsert_agent(agent: AgentRecord) -> None:
    coll = await get_collection()
    doc = agent.model_dump(by_alias=True)
    await coll.replace_one({"_id": agent.id}, doc, upsert=True)


async def delete_agent(agent_id: str) -> None:
    coll = await get_collection()
    await coll.delete_one({"_id": agent_id})


__all__ = [
    "AgentRecord",
    "list_agents",
    "get_agent",
    "upsert_agent",
    "delete_agent",
    "get_collection",
]
