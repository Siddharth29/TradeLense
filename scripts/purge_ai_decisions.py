#!/usr/bin/env python3
"""
Script to purge all AI decision and expert cycle data from MongoDB.
This includes:
- gpt_responses
- gpt_call_summaries
- multi_expert_cycles
- trigger_checks (related to GPT responses)
"""

import asyncio
import sys
from datetime import datetime
from pathlib import Path

# Add parent directory to path to import packages
sys.path.append(str(Path(__file__).parent.parent))

from packages.db.client import get_client
from packages.core.config import MONGO_DB_NAME
from packages.core.logger import get_logger

logger = get_logger(__name__)


async def purge_ai_decisions():
    """Purge all AI decision data from MongoDB."""
    client = get_client()
    db = client[MONGO_DB_NAME]
    
    collections_to_purge = [
        "gpt_responses",
        "gpt_call_summaries",
        "multi_expert_cycles",
        "trigger_checks",
        "slot_candle_matches",  # Also GPT-related
        "comprehensive_trades"  # Also contains GPT references
    ]
    
    total_deleted = 0
    
    print("\n" + "="*60)
    print("AI DECISION DATA PURGE SCRIPT")
    print("="*60)
    print(f"Database: {MONGO_DB_NAME}")
    print(f"Time: {datetime.now()}")
    print("\nCollections to purge:")
    for col in collections_to_purge:
        print(f"  - {col}")
    
    # Ask for confirmation
    print("\n" + "!"*60)
    print("WARNING: This will DELETE ALL data from the above collections!")
    print("This action cannot be undone!")
    print("!"*60)
    
    confirmation = input("\nType 'DELETE ALL' to confirm: ")
    if confirmation != "DELETE ALL":
        print("\nOperation cancelled.")
        return
    
    print("\nStarting purge...")
    
    for collection_name in collections_to_purge:
        try:
            collection = db[collection_name]
            
            # Count documents before deletion
            count_before = await collection.count_documents({})
            
            if count_before == 0:
                print(f"\n{collection_name}: No documents to delete")
                continue
            
            # Delete all documents
            result = await collection.delete_many({})
            deleted_count = result.deleted_count
            total_deleted += deleted_count
            
            print(f"\n{collection_name}:")
            print(f"  Documents before: {count_before}")
            print(f"  Documents deleted: {deleted_count}")
            print(f"  Documents remaining: {await collection.count_documents({})}")
            
            logger.info(f"Purged {deleted_count} documents from {collection_name}")
            
        except Exception as e:
            print(f"\nERROR in {collection_name}: {str(e)}")
            logger.error(f"Failed to purge {collection_name}: {str(e)}")
    
    print("\n" + "="*60)
    print(f"PURGE COMPLETE")
    print(f"Total documents deleted: {total_deleted}")
    print("="*60)
    
    # Additional cleanup for related data
    print("\nChecking for orphaned data...")
    
    # Clean up agent_runs that might reference deleted GPT responses
    try:
        agent_runs = db["agent_runs"]
        # Remove GPT response references from agent runs
        update_result = await agent_runs.update_many(
            {},
            {"$unset": {
                "gpt_response_id": "",
                "trigger_check_id": "",
                "gpt_call_summary_id": ""
            }}
        )
        if update_result.modified_count > 0:
            print(f"Cleaned {update_result.modified_count} agent_runs documents")
    except Exception as e:
        print(f"Error cleaning agent_runs: {str(e)}")
    
    print("\nPurge operation completed successfully!")


async def main():
    """Main entry point."""
    try:
        await purge_ai_decisions()
    except KeyboardInterrupt:
        print("\n\nOperation interrupted by user.")
    except Exception as e:
        print(f"\n\nFATAL ERROR: {str(e)}")
        logger.error(f"Fatal error in purge script: {str(e)}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
