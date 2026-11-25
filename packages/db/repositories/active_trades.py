"""Repository for managing active trades state persistence."""

from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from packages.db.client import get_client
from packages.core.config import MONGO_DB_NAME


def get_db():
    """Get database instance."""
    client = get_client()
    return client[MONGO_DB_NAME]


async def store_active_trade(
    agent_id: str,
    symbol: str,
    trade_data: Dict[str, Any],
    created_at: Optional[datetime] = None
) -> str:
    """
    Store an active trade in the database.
    
    Args:
        agent_id: Unique agent identifier
        symbol: Trading symbol (e.g., "BTCUSD")
        trade_data: Complete trade information including entry, stop, target, order_id
        created_at: Trade creation timestamp (defaults to now)
    
    Returns:
        Document ID of stored trade
    """
    db = get_db()
    collection = db["active_trades"]
    
    if created_at is None:
        created_at = datetime.now(timezone.utc)
    
    document = {
        "agent_id": agent_id,
        "symbol": symbol,
        "trade_data": trade_data,
        "created_at": created_at,
        "updated_at": created_at,
        "status": "active",
        "version": 1
    }
    
    result = await collection.insert_one(document)
    return str(result.inserted_id)


async def get_active_trades_for_agent(agent_id: str) -> List[Dict[str, Any]]:
    """
    Retrieve all active trades for a specific agent.
    
    Args:
        agent_id: Unique agent identifier
    
    Returns:
        List of active trade documents, sorted by creation time (newest first)
    """
    db = get_db()
    collection = db["active_trades"]
    
    cursor = collection.find(
        {"agent_id": agent_id, "status": "active"}
    ).sort("created_at", -1)
    
    trades = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        trades.append(doc)
    
    return trades


async def get_active_trade_by_symbol(agent_id: str, symbol: str) -> Optional[Dict[str, Any]]:
    """
    Get active trade for specific agent and symbol.
    
    Args:
        agent_id: Unique agent identifier
        symbol: Trading symbol
    
    Returns:
        Active trade document or None if not found
    """
    db = get_db()
    collection = db["active_trades"]
    
    doc = await collection.find_one({
        "agent_id": agent_id,
        "symbol": symbol,
        "status": "active"
    })
    
    if doc:
        doc["_id"] = str(doc["_id"])
    
    return doc


async def update_active_trade(
    agent_id: str,
    symbol: str,
    updates: Dict[str, Any]
) -> bool:
    """
    Update an active trade with new information.
    
    Args:
        agent_id: Unique agent identifier
        symbol: Trading symbol
        updates: Fields to update in trade_data
    
    Returns:
        True if update successful, False otherwise
    """
    db = get_db()
    collection = db["active_trades"]
    
    result = await collection.update_one(
        {"agent_id": agent_id, "symbol": symbol, "status": "active"},
        {
            "$set": {
                "trade_data": updates,
                "updated_at": datetime.now(timezone.utc)
            }
        }
    )
    
    return result.modified_count > 0


async def clear_active_trade(agent_id: str, symbol: str, reason: str = "manual_clear") -> bool:
    """
    Mark an active trade as closed/cleared.
    
    Args:
        agent_id: Unique agent identifier
        symbol: Trading symbol
        reason: Reason for clearing (e.g., "trade_completed", "manual_clear", "stop_loss", "target_hit")
    
    Returns:
        True if cleared successfully, False otherwise
    """
    db = get_db()
    collection = db["active_trades"]
    
    result = await collection.update_one(
        {"agent_id": agent_id, "symbol": symbol, "status": "active"},
        {
            "$set": {
                "status": "closed",
                "closed_at": datetime.now(timezone.utc),
                "close_reason": reason
            }
        }
    )
    
    return result.modified_count > 0


async def clear_all_active_trades_for_agent(agent_id: str) -> int:
    """
    Clear all active trades for an agent (emergency cleanup).
    
    Args:
        agent_id: Unique agent identifier
    
    Returns:
        Number of trades cleared
    """
    db = get_db()
    collection = db["active_trades"]
    
    result = await collection.update_many(
        {"agent_id": agent_id, "status": "active"},
        {
            "$set": {
                "status": "closed",
                "closed_at": datetime.now(timezone.utc),
                "close_reason": "agent_shutdown"
            }
        }
    )
    
    return result.modified_count


async def get_all_active_trades() -> List[Dict[str, Any]]:
    """
    Get all active trades across all agents (for monitoring/debugging).
    
    Returns:
        List of all active trade documents
    """
    db = get_db()
    collection = db["active_trades"]
    
    cursor = collection.find({"status": "active"}).sort("created_at", -1)
    
    trades = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        trades.append(doc)
    
    return trades


async def cleanup_old_trades(days: int = 30) -> int:
    """
    Remove closed trades older than specified days.
    
    Args:
        days: Number of days to keep closed trades
    
    Returns:
        Number of trades deleted
    """
    from datetime import timedelta
    
    db = get_db()
    collection = db["active_trades"]
    
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)
    
    result = await collection.delete_many({
        "status": "closed",
        "closed_at": {"$lt": cutoff_date}
    })
    
    return result.deleted_count

