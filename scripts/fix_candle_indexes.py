"""Script to fix MongoDB indexes for candles_5m and candles_15m collections.

This script:
1. Drops incorrect indexes (client_order_id unique index)
2. Creates correct indexes: [("asset", 1), ("time", 1)] without unique constraint
3. Can be run standalone or as part of bootstrap

Usage:
    python scripts/fix_candle_indexes.py
"""

import asyncio
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from packages.core.config import MONGO_DB_NAME
from packages.db.client import get_client
from packages.core.logger import get_logger

logger = get_logger(__name__)

COLLECTIONS = ["candles_5m", "candles_15m"]


async def fix_candle_indexes():
    """Fix indexes for historical candle collections."""
    client = get_client()
    db = client[MONGO_DB_NAME]
    
    print("=" * 70)
    print("Fixing Candle Collection Indexes")
    print("=" * 70)
    print()
    
    for collection_name in COLLECTIONS:
        print(f"Processing collection: {collection_name}")
        
        try:
            coll = db[collection_name]
            
            # List existing indexes
            existing_indexes = await coll.list_indexes().to_list(length=None)
            index_names = [idx["name"] for idx in existing_indexes]
            
            print(f"  Existing indexes: {', '.join(index_names)}")
            
            # Drop incorrect indexes
            indexes_to_drop = []
            
            # Drop client_order_id unique index if it exists
            if "client_order_id_1" in index_names:
                indexes_to_drop.append("client_order_id_1")
                print(f"  Will drop: client_order_id_1 (unique index)")
            
            # Drop any other client_order_id indexes
            for idx in existing_indexes:
                if "client_order_id" in idx.get("key", {}):
                    idx_name = idx["name"]
                    if idx_name not in indexes_to_drop:
                        indexes_to_drop.append(idx_name)
                        print(f"  Will drop: {idx_name}")
            
            # Drop incorrect indexes
            for idx_name in indexes_to_drop:
                try:
                    await coll.drop_index(idx_name)
                    print(f"  [OK] Dropped index: {idx_name}")
                except Exception as e:
                    print(f"  [FAIL] Failed to drop index {idx_name}: {e}")
            
            # Create correct compound index if it doesn't exist
            correct_index_name = "asset_1_time_1"
            if correct_index_name not in index_names:
                try:
                    await coll.create_index([("asset", 1), ("time", 1)])
                    print(f"  [OK] Created index: asset_1_time_1")
                except Exception as e:
                    print(f"  [FAIL] Failed to create index: {e}")
            else:
                print(f"  [OK] Correct index already exists: asset_1_time_1")
            
            # Verify final state
            final_indexes = await coll.list_indexes().to_list(length=None)
            final_index_names = [idx["name"] for idx in final_indexes]
            print(f"  Final indexes: {', '.join(final_index_names)}")
            print()
            
        except Exception as e:
            logger.error(f"Error processing collection {collection_name}: {e}", exc_info=True)
            print(f"  [FAIL] Error: {e}")
            print()
    
    print("=" * 70)
    print("[SUCCESS] Index fix completed!")
    print("=" * 70)


def main():
    """Main entry point."""
    try:
        asyncio.run(fix_candle_indexes())
        return 0
    except Exception as e:
        logger.error(f"Failed to fix indexes: {e}", exc_info=True)
        print(f"\nError: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

