"""Initialize MongoDB collections with proper indexes for comprehensive logging."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List

from packages.core.config import MONGO_DB_NAME
from packages.db.client import get_client
from packages.core.logger import get_logger

logger = get_logger(__name__)

# Collection names
COLLECTION_SLOT_CANDLE_MATCH = "slot_candle_matches"
COLLECTION_COMPREHENSIVE_TRADES = "comprehensive_trades"
COLLECTION_HOURLY_CANDLES = "hourly_candles"
COLLECTION_GPT_RESPONSES = "gpt_responses"
COLLECTION_TRIGGER_CHECKS = "trigger_checks"
COLLECTION_GPT_CALL_SUMMARIES = "gpt_call_summaries"
COLLECTION_GPT_THREADS = "gpt_threads"
COLLECTION_ACTIVE_TRADES = "active_trades"
COLLECTION_MULTI_EXPERT_CYCLES = "multi_expert_cycles"


async def create_indexes() -> None:
    """Create indexes for all collections to optimize queries."""
    
    client = get_client()
    db = client[MONGO_DB_NAME]
    
    try:
        # Slot-Candle Match Collection Indexes
        slot_candle_collection = db[COLLECTION_SLOT_CANDLE_MATCH]
        await slot_candle_collection.create_index([
            ("asset", 1),
            ("slot_timestamp", -1)
        ])
        await slot_candle_collection.create_index([
            ("asset", 1),
            ("slot_id", 1)
        ])
        await slot_candle_collection.create_index([
            ("gpt_response_id", 1)
        ])
        await slot_candle_collection.create_index([
            ("trigger_met", 1),
            ("trade_executed", 1)
        ])
        await slot_candle_collection.create_index([
            ("level_drawn_at", -1)
        ])
        await slot_candle_collection.create_index([
            ("trigger_timestamp", -1)
        ])
        logger.info("Created indexes for slot_candle_matches collection")
        
        # Comprehensive Trades Collection Indexes
        trades_collection = db[COLLECTION_COMPREHENSIVE_TRADES]
        await trades_collection.create_index([
            ("asset", 1),
            ("trade_executed_at", -1)
        ])
        await trades_collection.create_index([
            ("symbol", 1),
            ("trade_executed_at", -1)
        ])
        await trades_collection.create_index([
            ("trade_type", 1),
            ("trade_status", 1)
        ])
        await trades_collection.create_index([
            ("agent_id", 1),
            ("trade_executed_at", -1)
        ])
        await trades_collection.create_index([
            ("gpt_response_id", 1)
        ])
        await trades_collection.create_index([
            ("slot_candle_match_id", 1)
        ])
        await trades_collection.create_index([
            ("signal_received_at", -1)
        ])
        await trades_collection.create_index([
            ("trade_closed_at", -1)
        ])
        logger.info("Created indexes for comprehensive_trades collection")
        
        # Hourly Candles Collection Indexes
        hourly_candles_collection = db[COLLECTION_HOURLY_CANDLES]
        await hourly_candles_collection.create_index([
            ("asset", 1),
            ("slot_timestamp", -1)
        ])
        await hourly_candles_collection.create_index([
            ("symbol", 1),
            ("slot_timestamp", -1)
        ])
        await hourly_candles_collection.create_index([
            ("slot_id", 1)
        ])
        await hourly_candles_collection.create_index([
            ("candle_timestamp", -1)
        ])
        await hourly_candles_collection.create_index([
            ("market_session", 1),
            ("slot_timestamp", -1)
        ])
        logger.info("Created indexes for hourly_candles collection")
        
        # GPT Responses Collection Indexes (enhanced)
        gpt_responses_collection = db[COLLECTION_GPT_RESPONSES]
        await gpt_responses_collection.create_index([
            ("asset", 1),
            ("timestamp", -1)
        ])
        await gpt_responses_collection.create_index([
            ("symbol", 1),
            ("timestamp", -1)
        ])
        await gpt_responses_collection.create_index([
            ("agent_id", 1),
            ("timestamp", -1)
        ])
        await gpt_responses_collection.create_index([
            ("prompt_hash", 1)
        ])
        await gpt_responses_collection.create_index([
            ("response_type", 1),
            ("timestamp", -1)
        ])
        await gpt_responses_collection.create_index([
            ("model", 1),
            ("timestamp", -1)
        ])
        logger.info("Created indexes for gpt_responses collection")
        
        # Trigger Checks Collection Indexes (enhanced)
        trigger_checks_collection = db[COLLECTION_TRIGGER_CHECKS]
        await trigger_checks_collection.create_index([
            ("asset", 1),
            ("trigger_time", -1)
        ])
        await trigger_checks_collection.create_index([
            ("symbol", 1),
            ("trigger_time", -1)
        ])
        await trigger_checks_collection.create_index([
            ("gpt_response_id", 1)
        ])
        await trigger_checks_collection.create_index([
            ("trigger_met", 1),
            ("trigger_type", 1)
        ])
        await trigger_checks_collection.create_index([
            ("agent_id", 1),
            ("trigger_time", -1)
        ])
        logger.info("Created indexes for trigger_checks collection")
        
        # GPT Call Summaries Collection Indexes (enhanced)
        summaries_collection = db[COLLECTION_GPT_CALL_SUMMARIES]
        await summaries_collection.create_index([
            ("asset", 1),
            ("timestamp", -1)
        ])
        await summaries_collection.create_index([
            ("symbol", 1),
            ("timestamp", -1)
        ])
        await summaries_collection.create_index([
            ("agent_id", 1),
            ("timestamp", -1)
        ])
        await summaries_collection.create_index([
            ("gpt_response_id", 1)
        ])
        await summaries_collection.create_index([
            ("trigger_check_id", 1)
        ])
        await summaries_collection.create_index([
            ("trade_executed", 1),
            ("timestamp", -1)
        ])
        await summaries_collection.create_index([
            ("decision_made", 1),
            ("order_placed", 1),
            ("trade_executed", 1)
        ])
        logger.info("Created indexes for gpt_call_summaries collection")
        
        # GPT Threads Collection Indexes
        threads_collection = db[COLLECTION_GPT_THREADS]
        await threads_collection.create_index([
            ("asset", 1),
            ("agent_id", 1)
        ], unique=True)
        await threads_collection.create_index([
            ("created_at", 1)
        ], expireAfterSeconds=7*24*60*60)  # 7 days TTL
        await threads_collection.create_index([
            ("thread_id", 1)
        ])
        logger.info("Created indexes for gpt_threads collection")
        
        # Active Trades Collection Indexes
        active_trades_collection = db[COLLECTION_ACTIVE_TRADES]
        await active_trades_collection.create_index([
            ("agent_id", 1),
            ("status", 1),
            ("created_at", -1)
        ])
        await active_trades_collection.create_index([
            ("symbol", 1),
            ("status", 1)
        ])
        await active_trades_collection.create_index([
            ("agent_id", 1),
            ("symbol", 1),
            ("status", 1)
        ], unique=True)
        await active_trades_collection.create_index([
            ("status", 1),
            ("closed_at", 1)
        ])
        logger.info("Created indexes for active_trades collection")
        
        # Multi-Expert Cycles Collection Indexes
        multi_expert_cycles_collection = db[COLLECTION_MULTI_EXPERT_CYCLES]
        await multi_expert_cycles_collection.create_index([
            ("started_at", -1)
        ])
        await multi_expert_cycles_collection.create_index([
            ("agent_id", 1),
            ("started_at", -1)
        ])
        await multi_expert_cycles_collection.create_index([
            ("mode", 1),
            ("started_at", -1)
        ])
        await multi_expert_cycles_collection.create_index([
            ("asset", 1),
            ("started_at", -1)
        ])
        await multi_expert_cycles_collection.create_index([
            ("status", 1),
            ("started_at", -1)
        ])
        await multi_expert_cycles_collection.create_index([
            ("cycle_id", 1)
        ], unique=True)
        logger.info("Created indexes for multi_expert_cycles collection")
        
        logger.info("Successfully created all collection indexes")
        
    except Exception as e:
        logger.error(f"Failed to create indexes: {e}")
        raise


async def create_collections() -> None:
    """Create collections with validation rules."""
    
    client = get_client()
    db = client[MONGO_DB_NAME]
    
    try:
        # Create collections if they don't exist
        collections_to_create = [
            COLLECTION_SLOT_CANDLE_MATCH,
            COLLECTION_COMPREHENSIVE_TRADES,
            COLLECTION_HOURLY_CANDLES,
            COLLECTION_GPT_RESPONSES,
            COLLECTION_TRIGGER_CHECKS,
            COLLECTION_GPT_CALL_SUMMARIES,
            COLLECTION_GPT_THREADS,
            COLLECTION_ACTIVE_TRADES,
            COLLECTION_MULTI_EXPERT_CYCLES,
        ]
        
        existing_collections = await db.list_collection_names()
        
        for collection_name in collections_to_create:
            if collection_name not in existing_collections:
                await db.create_collection(collection_name)
                logger.info(f"Created collection: {collection_name}")
            else:
                logger.info(f"Collection already exists: {collection_name}")
        
        logger.info("Successfully created all collections")
        
    except Exception as e:
        logger.error(f"Failed to create collections: {e}")
        raise


async def initialize_database() -> None:
    """Initialize the database with collections and indexes."""
    
    logger.info("Initializing database with comprehensive logging collections...")
    
    try:
        # Create collections first
        await create_collections()
        
        # Create indexes
        await create_indexes()
        
        logger.info("Database initialization completed successfully")
        
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")
        raise


async def get_collection_stats() -> Dict[str, Any]:
    """Get statistics for all collections."""
    
    client = get_client()
    db = client[MONGO_DB_NAME]
    
    stats = {}
    
    try:
        collections = [
            COLLECTION_SLOT_CANDLE_MATCH,
            COLLECTION_COMPREHENSIVE_TRADES,
            COLLECTION_HOURLY_CANDLES,
            COLLECTION_GPT_RESPONSES,
            COLLECTION_TRIGGER_CHECKS,
            COLLECTION_GPT_CALL_SUMMARIES,
            COLLECTION_ACTIVE_TRADES,
            COLLECTION_MULTI_EXPERT_CYCLES,
        ]
        
        for collection_name in collections:
            try:
                collection = db[collection_name]
                count = await collection.count_documents({})
                stats[collection_name] = {
                    "count": count,
                    "exists": True
                }
            except Exception as e:
                stats[collection_name] = {
                    "count": 0,
                    "exists": False,
                    "error": str(e)
                }
        
        return stats
        
    except Exception as e:
        logger.error(f"Failed to get collection stats: {e}")
        return {}


if __name__ == "__main__":
    # Run initialization if called directly
    asyncio.run(initialize_database())
