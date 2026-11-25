from __future__ import annotations

"""Repository helpers for GPT responses and trigger checks."""

import hashlib
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from motor.motor_asyncio import AsyncIOMotorCollection
from pymongo import ReadPreference, WriteConcern

from packages.core.config import MONGO_DB_NAME
from packages.db.client import get_client as _get_client
from packages.db.models import GptResponseDoc, TriggerCheckDoc, GptCallSummaryDoc
from packages.db.persistence_gate import (
    analytics_persistence_enabled,
    log_event,
    synthetic_id,
)

GPT_RESPONSES_COLLECTION = "gpt_responses"
TRIGGER_CHECKS_COLLECTION = "trigger_checks"
GPT_CALL_SUMMARIES_COLLECTION = "gpt_call_summaries"


async def get_gpt_responses_collection() -> AsyncIOMotorCollection:
    """Return the GPT responses collection from the shared client."""
    client = _get_client()
    db = client[MONGO_DB_NAME]
    return db[GPT_RESPONSES_COLLECTION]


async def get_trigger_checks_collection() -> AsyncIOMotorCollection:
    """Return the trigger checks collection from the shared client."""
    client = _get_client()
    db = client[MONGO_DB_NAME]
    return db[TRIGGER_CHECKS_COLLECTION]


async def get_gpt_call_summaries_collection() -> AsyncIOMotorCollection:
    """Return the GPT call summaries collection from the shared client."""
    client = _get_client()
    db = client[MONGO_DB_NAME]
    return db[GPT_CALL_SUMMARIES_COLLECTION]


async def aggregate_token_spend(since: Optional[datetime] = None) -> float:
    """Sum ``tokens_used`` across GPT responses since *since* (UTC).

    Read-only dashboard aggregation. Returns ``0.0`` when Mongo analytics
    persistence is disabled or on any error — never raises.
    """
    if not analytics_persistence_enabled():
        return 0.0
    try:
        coll = await get_gpt_responses_collection()
        match: dict[str, Any] = {}
        if since is not None:
            match["timestamp"] = {"$gte": since}
        pipeline = [
            {"$match": match},
            {"$group": {"_id": None, "tokens": {"$sum": {"$ifNull": ["$tokens_used", 0]}}}},
        ]
        docs = await coll.aggregate(pipeline).to_list(length=1)
        if not docs:
            return 0.0
        return float(docs[0].get("tokens", 0) or 0.0)
    except Exception:  # pragma: no cover - dashboard read must never crash
        return 0.0


def _generate_checksum(data: Mapping[str, Any]) -> str:
    """Generate a checksum for the given data."""
    # Create a stable string representation for hashing
    import json
    stable_str = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(stable_str.encode()).hexdigest()


async def store_gpt_response(
    asset: str,
    agent_id: str,
    model: str,
    prompt_hash: str,
    prompt_content: str,
    response_content: str,
    response_type: str,
    latency_ms: float,
    symbol: Optional[str] = None,
    tokens_used: Optional[int] = None,
    error_message: Optional[str] = None,
    parsed_decision: Optional[dict] = None,
    confidence: Optional[float] = None,
    pattern: Optional[str] = None,
    direction: Optional[str] = None,
    level: Optional[float] = None,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    quantity: Optional[float] = None,
    broker_type: Optional[str] = None,
    gpt_mode: Optional[str] = None,
    prompt_id: Optional[str] = None,
    # New fields for audit and TTL tracking (optional for legacy compatibility)
    ttl_minutes: Optional[int] = None,
    active_window_c5: Optional[int] = None,
    active_window_c15: Optional[int] = None,
    rationale: Optional[str] = None,
) -> str:
    """Store a GPT response in the database and return the document ID."""
    timestamp = datetime.now(timezone.utc)
    client_order_id = f"gpt_{asset}_{agent_id}_{timestamp.isoformat()}"
    
    # Create the document data
    doc_data = {
        "version": 1,
        "timestamp": timestamp,
        "client_order_id": client_order_id,
        "asset": asset,
        "symbol": symbol or asset,  # Use symbol if provided, otherwise use asset
        "agent_id": agent_id,
        "assistant_id": agent_id,  # Use agent_id as assistant_id for compatibility
        "model": model,
        "prompt_hash": prompt_hash,
        "prompt_content": prompt_content,
        "response_content": response_content,
        "response_type": response_type,
        "latency_ms": latency_ms,
        "tokens_used": tokens_used,
        "error_message": error_message,
        "parsed_decision": parsed_decision,
        "confidence": confidence,
        "pattern": pattern,
        "direction": direction,
        "level": level,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "quantity": quantity,
        "broker_type": broker_type,
        "gpt_mode": gpt_mode,
        # New fields for audit and TTL tracking
        "ttl_minutes": ttl_minutes,
        "active_window_c5": active_window_c5,
        "active_window_c15": active_window_c15,
        "rationale": rationale,
    }
    
    # Generate checksum
    doc_data["checksum"] = _generate_checksum(doc_data)
    
    # Validate with Pydantic model
    doc = GptResponseDoc(**doc_data)
    doc_dump = doc.model_dump(exclude_none=True, exclude={"_id"})

    # Durable structured event (prompt + response + parsed decision) — the
    # system-of-record in log-only mode.
    log_event(
        "gpt_response", doc_dump, asset=asset, agent_id=agent_id,
        gpt_mode=gpt_mode, prompt_hash=prompt_hash,
    )
    if not analytics_persistence_enabled():
        return synthetic_id()

    # Insert into database
    collection = await get_gpt_responses_collection()
    result = await collection.insert_one(doc_dump)
    return str(result.inserted_id)


async def store_trigger_check(
    asset: str,
    agent_id: str,
    prompt_hash: str,
    gpt_response_id: str,
    trigger_condition: str,
    trigger_type: str,
    trigger_met: bool,
    symbol: Optional[str] = None,
    level: Optional[float] = None,
    direction: Optional[str] = None,
    buffer: Optional[float] = None,
    trigger_time: Optional[datetime] = None,
    future_candles_count: Optional[int] = None,
    entry_price: Optional[float] = None,
    confidence: Optional[float] = None,
    pattern: Optional[str] = None,
) -> str:
    """Store a trigger check result in the database and return the document ID."""
    collection = await get_trigger_checks_collection()
    
    timestamp = datetime.now(timezone.utc)
    client_order_id = f"trigger_{asset}_{agent_id}_{timestamp.isoformat()}"
    
    # Create the document data
    doc_data = {
        "version": 1,
        "timestamp": timestamp,
        "client_order_id": client_order_id,
        "asset": asset,
        "symbol": symbol or asset,  # Use symbol if provided, otherwise use asset
        "agent_id": agent_id,
        "prompt_hash": prompt_hash,
        "gpt_response_id": gpt_response_id,
        "trigger_condition": trigger_condition,
        "trigger_type": trigger_type,
        "trigger_met": trigger_met,
        "level": level,
        "direction": direction,
        "buffer": buffer,
        "trigger_time": trigger_time,
        "future_candles_count": future_candles_count,
        "entry_price": entry_price,
        "confidence": confidence,
        "pattern": pattern,
    }
    
    # Generate checksum
    doc_data["checksum"] = _generate_checksum(doc_data)
    
    # Validate with Pydantic model
    doc = TriggerCheckDoc(**doc_data)
    
    # Insert into database
    result = await collection.insert_one(doc.model_dump(exclude_none=True, exclude={"_id"}))
    return str(result.inserted_id)


async def store_gpt_call_summary(
    asset: str,
    agent_id: str,
    prompt_hash: str,
    gpt_response_id: str,
    latency_ms: float,
    response_type: str,
    decision_made: bool,
    symbol: Optional[str] = None,
    order_placed: bool = False,
    trade_executed: bool = False,
    trigger_check_id: Optional[str] = None,
    tokens_used: Optional[int] = None,
    trigger_met: Optional[bool] = None,
    confidence: Optional[float] = None,
    pattern: Optional[str] = None,
    direction: Optional[str] = None,
    broker_type: Optional[str] = None,
    gpt_mode: Optional[str] = None,
    # New fields for audit and TTL tracking (optional for legacy compatibility)
    ttl_minutes: Optional[int] = None,
    active_window_c5: Optional[int] = None,
    active_window_c15: Optional[int] = None,
    rationale: Optional[str] = None,
) -> str:
    """Store a GPT call summary in the database and return the document ID."""
    collection = await get_gpt_call_summaries_collection()
    
    timestamp = datetime.now(timezone.utc)
    
    # Create the document data
    doc_data = {
        "asset": asset,
        "symbol": symbol or asset,  # Use symbol if provided, otherwise use asset
        "agent_id": agent_id,
        "timestamp": timestamp,
        "prompt_hash": prompt_hash,
        "gpt_response_id": gpt_response_id,
        "trigger_check_id": trigger_check_id,
        "latency_ms": latency_ms,
        "tokens_used": tokens_used,
        "response_type": response_type,
        "trigger_met": trigger_met,
        "decision_made": decision_made,
        "order_placed": order_placed,
        "trade_executed": trade_executed,
        "confidence": confidence,
        "pattern": pattern,
        "direction": direction,
        "broker_type": broker_type,
        "gpt_mode": gpt_mode,
        # New fields for audit and TTL tracking
        "ttl_minutes": ttl_minutes,
        "active_window_c5": active_window_c5,
        "active_window_c15": active_window_c15,
        "rationale": rationale,
    }
    
    # Validate with Pydantic model
    doc = GptCallSummaryDoc(**doc_data)
    
    # Insert into database
    result = await collection.insert_one(doc.model_dump(exclude_none=True, exclude={"_id"}))
    return str(result.inserted_id)


async def get_gpt_responses_by_asset(
    asset: str,
    limit: int = 100,
    skip: int = 0,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """Retrieve GPT responses for a specific asset."""
    collection = await get_gpt_responses_collection()
    
    # Build filter
    filter_dict = {"asset": asset}
    if start_time or end_time:
        time_filter = {}
        if start_time:
            time_filter["$gte"] = start_time
        if end_time:
            time_filter["$lte"] = end_time
        filter_dict["timestamp"] = time_filter
    
    # Query with sorting by timestamp descending
    cursor = collection.find(filter_dict).sort("timestamp", -1).skip(skip).limit(limit)
    docs = await cursor.to_list(length=None)
    
    # Convert ObjectIds to strings and ensure proper timestamp format
    responses = []
    for doc in docs:
        doc["_id"] = str(doc["_id"])
        # Ensure timestamp is properly formatted
        if 'timestamp' in doc and hasattr(doc['timestamp'], 'isoformat'):
            doc['timestamp'] = doc['timestamp'].isoformat()
        responses.append(doc)
    
    return responses


async def get_trigger_checks_by_asset(
    asset: str,
    limit: int = 100,
    skip: int = 0,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """Retrieve trigger checks for a specific asset."""
    collection = await get_trigger_checks_collection()
    
    # Build filter
    filter_dict = {"asset": asset}
    if start_time or end_time:
        time_filter = {}
        if start_time:
            time_filter["$gte"] = start_time
        if end_time:
            time_filter["$lte"] = end_time
        filter_dict["timestamp"] = time_filter
    
    # Query with sorting by timestamp descending
    cursor = collection.find(filter_dict).sort("timestamp", -1).skip(skip).limit(limit)
    docs = await cursor.to_list(length=None)
    
    # Convert ObjectIds to strings and ensure proper timestamp format
    responses = []
    for doc in docs:
        doc["_id"] = str(doc["_id"])
        # Ensure timestamp is properly formatted
        if 'timestamp' in doc and hasattr(doc['timestamp'], 'isoformat'):
            doc['timestamp'] = doc['timestamp'].isoformat()
        if 'trigger_time' in doc and doc['trigger_time'] and hasattr(doc['trigger_time'], 'isoformat'):
            doc['trigger_time'] = doc['trigger_time'].isoformat()
        responses.append(doc)
    
    return responses


async def get_gpt_call_summaries_by_asset(
    asset: str,
    limit: int = 100,
    skip: int = 0,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """Retrieve GPT call summaries for a specific asset."""
    collection = await get_gpt_call_summaries_collection()
    
    # Build filter
    filter_dict = {"asset": asset}
    if start_time or end_time:
        time_filter = {}
        if start_time:
            time_filter["$gte"] = start_time
        if end_time:
            time_filter["$lte"] = end_time
        filter_dict["timestamp"] = time_filter
    
    # Query with sorting by timestamp descending
    cursor = collection.find(filter_dict).sort("timestamp", -1).skip(skip).limit(limit)
    docs = await cursor.to_list(length=None)
    
    # Convert ObjectIds to strings and ensure proper timestamp format
    responses = []
    for doc in docs:
        doc["_id"] = str(doc["_id"])
        # Ensure timestamp is properly formatted
        if 'timestamp' in doc and hasattr(doc['timestamp'], 'isoformat'):
            doc['timestamp'] = doc['timestamp'].isoformat()
        responses.append(doc)
    
    return responses


async def get_all_gpt_responses(
    limit: int = 100,
    skip: int = 0,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """Retrieve GPT responses for all assets."""
    collection = await get_gpt_responses_collection()
    
    # Build filter
    filter_dict = {}
    if start_time or end_time:
        time_filter = {}
        if start_time:
            time_filter["$gte"] = start_time
        if end_time:
            time_filter["$lte"] = end_time
        filter_dict["timestamp"] = time_filter
    
    # Query with sorting by timestamp descending
    cursor = collection.find(filter_dict).sort("timestamp", -1).skip(skip).limit(limit)
    docs = await cursor.to_list(length=None)
    
    # Convert ObjectIds to strings and ensure proper timestamp format
    responses = []
    for doc in docs:
        doc["_id"] = str(doc["_id"])
        # Ensure timestamp is properly formatted
        if 'timestamp' in doc and hasattr(doc['timestamp'], 'isoformat'):
            doc['timestamp'] = doc['timestamp'].isoformat()
        responses.append(doc)
    
    return responses


async def get_all_trigger_checks(
    limit: int = 100,
    skip: int = 0,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """Retrieve trigger checks for all assets."""
    collection = await get_trigger_checks_collection()
    
    # Build filter
    filter_dict = {}
    if start_time or end_time:
        time_filter = {}
        if start_time:
            time_filter["$gte"] = start_time
        if end_time:
            time_filter["$lte"] = end_time
        filter_dict["timestamp"] = time_filter
    
    # Query with sorting by timestamp descending
    cursor = collection.find(filter_dict).sort("timestamp", -1).skip(skip).limit(limit)
    docs = await cursor.to_list(length=None)
    
    # Convert ObjectIds to strings and ensure proper timestamp format
    responses = []
    for doc in docs:
        doc["_id"] = str(doc["_id"])
        # Ensure timestamp is properly formatted
        if 'timestamp' in doc and hasattr(doc['timestamp'], 'isoformat'):
            doc['timestamp'] = doc['timestamp'].isoformat()
        responses.append(doc)
    
    return responses


async def get_all_gpt_call_summaries(
    limit: int = 100,
    skip: int = 0,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """Retrieve GPT call summaries for all assets."""
    collection = await get_gpt_call_summaries_collection()
    
    # Build filter
    filter_dict = {}
    if start_time or end_time:
        time_filter = {}
        if start_time:
            time_filter["$gte"] = start_time
        if end_time:
            time_filter["$lte"] = end_time
        filter_dict["timestamp"] = time_filter
    
    # Query with sorting by timestamp descending
    cursor = collection.find(filter_dict).sort("timestamp", -1).skip(skip).limit(limit)
    docs = await cursor.to_list(length=None)
    
    # Convert ObjectIds to strings and ensure proper timestamp format
    responses = []
    for doc in docs:
        doc["_id"] = str(doc["_id"])
        # Ensure timestamp is properly formatted
        if 'timestamp' in doc and hasattr(doc['timestamp'], 'isoformat'):
            doc['timestamp'] = doc['timestamp'].isoformat()
        responses.append(doc)
    
    return responses


async def get_gpt_response_by_id(response_id: str) -> Optional[dict[str, Any]]:
    """Retrieve a specific GPT response by ID."""
    collection = await get_gpt_responses_collection()
    return await collection.find_one({"_id": response_id})


async def get_trigger_check_by_id(trigger_id: str) -> Optional[dict[str, Any]]:
    """Retrieve a specific trigger check by ID."""
    collection = await get_trigger_checks_collection()
    return await collection.find_one({"_id": trigger_id})


async def get_unique_assets() -> list[str]:
    """Get unique assets from GPT responses collection."""
    collection = await get_gpt_responses_collection()
    pipeline = [
        {"$group": {"_id": "$asset"}},
        {"$sort": {"_id": 1}}
    ]
    cursor = collection.aggregate(pipeline)
    results = await cursor.to_list(length=None)
    return [result["_id"] for result in results]


async def get_unique_trigger_assets() -> list[str]:
    """Get unique assets from trigger checks collection."""
    collection = await get_trigger_checks_collection()
    pipeline = [
        {"$group": {"_id": "$asset"}},
        {"$sort": {"_id": 1}}
    ]
    cursor = collection.aggregate(pipeline)
    results = await cursor.to_list(length=None)
    return [result["_id"] for result in results]


async def delete_gpt_response(response_id: str) -> bool:
    """Delete a GPT response by ID."""
    from bson import ObjectId
    collection = await get_gpt_responses_collection()
    result = await collection.delete_one({"_id": ObjectId(response_id)})
    return result.deleted_count > 0


async def delete_trigger_check(trigger_id: str) -> bool:
    """Delete a trigger check by ID."""
    from bson import ObjectId
    collection = await get_trigger_checks_collection()
    result = await collection.delete_one({"_id": ObjectId(trigger_id)})
    return result.deleted_count > 0