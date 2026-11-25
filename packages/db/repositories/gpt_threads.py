"""Repository for managing GPT thread persistence and reuse."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

from motor.motor_asyncio import AsyncIOMotorCollection
from pydantic import BaseModel, Field

from packages.db.client import get_client
from packages.core.config import MONGO_DB_NAME
from packages.core.logger import get_logger

logger = get_logger(__name__)

COLLECTION_NAME = "gpt_threads"


class GptThreadRecord(BaseModel):
    """GPT thread record for persistence."""
    
    asset: str
    agent_id: str
    thread_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc) + timedelta(days=7))
    
    model_config = {"extra": "forbid", "strict": True}


async def get_collection() -> AsyncIOMotorCollection:
    """Get the GPT threads collection."""
    client = get_client()
    db = client[MONGO_DB_NAME]
    return db[COLLECTION_NAME]


async def get_or_create_thread(asset: str, agent_id: str) -> Optional[str]:
    """
    Get existing thread ID for asset/agent combination or create a new one.
    
    Returns:
        Thread ID if found or created, None if creation failed
    """
    max_retries = 3
    retry_delay = 1.0  # seconds
    
    for attempt in range(max_retries):
        try:
            collection = await get_collection()
            
            # Try to find existing valid thread
            existing = await collection.find_one({
                "asset": asset,
                "agent_id": agent_id,
                "expires_at": {"$gt": datetime.now(timezone.utc)}
            })
            
            if existing:
                logger.info(
                    f"Reusing existing GPT thread for asset={asset}, agent={agent_id}, "
                    f"thread_id={existing['thread_id']}, created_at={existing['created_at']}"
                )
                return existing["thread_id"]
            
            # No valid thread found, will need to create new one
            logger.info(
                f"No valid GPT thread found for asset={asset}, agent={agent_id}, "
                "new thread will be created by caller"
            )
            return None
            
        except Exception as e:
            logger.warning(
                f"Error getting GPT thread for asset={asset}, agent={agent_id} "
                f"(attempt {attempt + 1}/{max_retries}): {e}"
            )
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_delay * (2 ** attempt))  # Exponential backoff
            else:
                logger.error(
                    f"Failed to get GPT thread after {max_retries} attempts: {e}",
                    exc_info=True
                )
                return None


async def store_thread(asset: str, agent_id: str, thread_id: str) -> bool:
    """
    Store a new GPT thread for the given asset/agent combination.
    
    Returns:
        True if stored successfully, False otherwise
    """
    max_retries = 3
    retry_delay = 1.0  # seconds
    
    for attempt in range(max_retries):
        try:
            collection = await get_collection()
            
            # Remove any existing threads for this asset/agent combination
            await collection.delete_many({
                "asset": asset,
                "agent_id": agent_id
            })
            
            # Create new thread record
            thread_record = GptThreadRecord(
                asset=asset,
                agent_id=agent_id,
                thread_id=thread_id
            )
            
            result = await collection.insert_one(thread_record.model_dump())
            
            if result.inserted_id:
                logger.info(
                    f"Stored new GPT thread for asset={asset}, agent={agent_id}, "
                    f"thread_id={thread_id}, expires_at={thread_record.expires_at}"
                )
                return True
            else:
                logger.error(f"Failed to store GPT thread for asset={asset}, agent={agent_id}")
                return False
                
        except Exception as e:
            logger.warning(
                f"Error storing GPT thread for asset={asset}, agent={agent_id} "
                f"(attempt {attempt + 1}/{max_retries}): {e}"
            )
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_delay * (2 ** attempt))  # Exponential backoff
            else:
                logger.error(
                    f"Failed to store GPT thread after {max_retries} attempts: {e}",
                    exc_info=True
                )
                return False


async def cleanup_expired_threads() -> int:
    """
    Manually cleanup expired threads (though TTL should handle this automatically).
    
    Returns:
        Number of threads cleaned up
    """
    try:
        collection = await get_collection()
        
        result = await collection.delete_many({
            "expires_at": {"$lt": datetime.now(timezone.utc)}
        })
        
        if result.deleted_count > 0:
            logger.info(f"Cleaned up {result.deleted_count} expired GPT threads")
        
        return result.deleted_count
        
    except Exception as e:
        logger.error(f"Error cleaning up expired GPT threads: {e}", exc_info=True)
        return 0


async def get_thread_stats() -> Dict[str, Any]:
    """Get statistics about stored GPT threads."""
    try:
        collection = await get_collection()
        
        total_count = await collection.count_documents({})
        active_count = await collection.count_documents({
            "expires_at": {"$gt": datetime.now(timezone.utc)}
        })
        expired_count = total_count - active_count
        
        # Get unique asset/agent combinations
        pipeline = [
            {"$group": {"_id": {"asset": "$asset", "agent_id": "$agent_id"}}},
            {"$count": "unique_combinations"}
        ]
        result = await collection.aggregate(pipeline).to_list(1)
        unique_combinations = result[0]["unique_combinations"] if result else 0
        
        return {
            "total_threads": total_count,
            "active_threads": active_count,
            "expired_threads": expired_count,
            "unique_asset_agent_combinations": unique_combinations
        }
        
    except Exception as e:
        logger.error(f"Error getting GPT thread stats: {e}", exc_info=True)
        return {
            "total_threads": 0,
            "active_threads": 0,
            "expired_threads": 0,
            "unique_asset_agent_combinations": 0
        }
