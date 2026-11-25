from __future__ import annotations

"""Async helpers for persisting and fetching candle data."""

from datetime import datetime
from typing import Any, Iterable, Mapping

from packages.core.config import MONGO_DB_NAME
from packages.db.client import get_client

_COLLECTION = "candles"
_COLLECTION_5M = "candles_5m"
_COLLECTION_15M = "candles_15m"


async def _insert_many(
    collection: str, candles: Iterable[Mapping[str, Any]], ts_field: str
) -> None:
    """Bulk upsert *candles* into ``collection``.

    Documents are matched either on ``{"asset", "slot_id"}`` when ``slot_id`` is
    present or on ``{"asset", ts_field}`` as a fallback.  This allows callers to
    provide deterministic identifiers independent of the timestamp to ensure
    idempotent writes when upstream feeds may resend the same candle multiple
    times.
    """

    docs = list(candles)
    if not docs:
        return
    
    client = get_client()
    coll = client[MONGO_DB_NAME][collection]
    
    # Use bulk operations for better performance
    from pymongo import UpdateOne
    from packages.core.logger import get_logger
    logger = get_logger(__name__)
    
    operations = []
    for doc in docs:
        # Validate required fields
        if "asset" not in doc:
            logger.warning(f"Skipping document without asset field: {doc}")
            continue  # Skip invalid documents
        if ts_field not in doc:
            logger.warning(f"Skipping document without {ts_field} field: {doc}")
            continue  # Skip documents without timestamp
        
        # Remove client_order_id if None to avoid unique index conflicts
        # Historical candles don't have client_order_id, but collection may have unique index
        doc_copy = dict(doc)
        # For historical candle collections (candles_5m, candles_15m), remove client_order_id entirely
        if collection in (_COLLECTION_5M, _COLLECTION_15M):
            doc_copy.pop("client_order_id", None)
        elif "client_order_id" in doc_copy and doc_copy["client_order_id"] is None:
            del doc_copy["client_order_id"]
            
        # Create filter for upsert
        if "slot_id" in doc_copy:
            filter_doc = {"asset": doc_copy["asset"], "slot_id": doc_copy["slot_id"]}
        elif "backtest_id" in doc_copy:
            # Include backtest_id in filter to preserve it during upserts
            # This ensures candles with different backtest_ids are stored separately
            filter_doc = {
                "asset": doc_copy["asset"], 
                ts_field: doc_copy[ts_field],
                "backtest_id": doc_copy["backtest_id"]
            }
        else:
            filter_doc = {"asset": doc_copy["asset"], ts_field: doc_copy[ts_field]}
        
        # Create proper PyMongo UpdateOne operation
        operations.append(
            UpdateOne(
                filter=filter_doc,
                update={"$set": doc_copy},
                upsert=True
            )
        )
    
    # Execute bulk operations if we have any
    if operations:
        import asyncio
        
        # Process in chunks to prevent timeouts on large batches
        # Maximum batch size: 1000 operations per chunk
        BATCH_SIZE = 1000
        total_ops = len(operations)
        
        # Track cumulative results across all batches
        total_inserted = 0
        total_matched = 0
        total_modified = 0
        total_upserted = 0
        
        # Process operations in batches
        for batch_start in range(0, total_ops, BATCH_SIZE):
            batch_end = min(batch_start + BATCH_SIZE, total_ops)
            batch_operations = operations[batch_start:batch_end]
            batch_num = (batch_start // BATCH_SIZE) + 1
            total_batches = (total_ops + BATCH_SIZE - 1) // BATCH_SIZE
            
            logger.info(
                f"Processing batch {batch_num}/{total_batches} for collection {collection}: "
                f"{len(batch_operations)} operations (total: {total_ops})"
            )
            
            try:
                # Execute batch with timeout
                result = await asyncio.wait_for(
                    coll.bulk_write(batch_operations, ordered=False),
                    timeout=30.0  # 30 second timeout per batch
                )
                
                # Accumulate results
                batch_inserted = result.bulk_api_result.get('nInserted', 0)
                batch_matched = result.bulk_api_result.get('nMatched', 0)
                batch_modified = result.bulk_api_result.get('nModified', 0)
                batch_upserted = result.bulk_api_result.get('nUpserted', 0)
                
                total_inserted += batch_inserted
                total_matched += batch_matched
                total_modified += batch_modified
                total_upserted += batch_upserted
                
                logger.debug(
                    f"Batch {batch_num} completed: inserted={batch_inserted}, matched={batch_matched}, "
                    f"modified={batch_modified}, upserted={batch_upserted}"
                )
                
            except asyncio.TimeoutError:
                logger.error(
                    f"Batch {batch_num}/{total_batches} timed out for collection {collection} after 30 seconds. "
                    f"Falling back to individual operations for this batch."
                )
                # Fallback to individual operations for this batch
                for operation in batch_operations:
                    try:
                        await asyncio.wait_for(
                            coll.update_one(
                                operation._filter, 
                                operation._doc, 
                                upsert=operation._upsert
                            ),
                            timeout=5.0  # 5 second timeout per individual operation
                        )
                        total_upserted += 1  # Approximate count
                    except asyncio.TimeoutError:
                        logger.warning(f"Individual insert timed out for operation: {operation._filter}")
                    except Exception as individual_error:
                        logger.warning(f"Individual insert failed: {individual_error}")
            except Exception as e:
                logger.error(
                    f"Batch {batch_num}/{total_batches} failed for collection {collection}: {e}. "
                    f"Falling back to individual operations for this batch."
                )
                # Fallback to individual operations for this batch
                for operation in batch_operations:
                    try:
                        await asyncio.wait_for(
                            coll.update_one(
                                operation._filter, 
                                operation._doc, 
                                upsert=operation._upsert
                            ),
                            timeout=5.0  # 5 second timeout per individual operation
                        )
                        total_upserted += 1  # Approximate count
                    except asyncio.TimeoutError:
                        logger.warning(f"Individual insert timed out for operation: {operation._filter}")
                    except Exception as individual_error:
                        logger.warning(f"Individual insert failed: {individual_error}")
        
        # Log final summary
        logger.info(
            f"Bulk upsert completed for collection {collection}: "
            f"inserted={total_inserted}, matched={total_matched}, modified={total_modified}, "
            f"upserted={total_upserted}, total_operations={total_ops}"
        )
        
        # Log warning if no new data was inserted (all matched)
        if total_inserted == 0 and total_upserted == 0 and total_matched > 0:
            logger.warning(
                f"All {total_matched} documents were matched (no new data) in collection {collection} - "
                f"data may be stale or duplicate"
            )


async def _get_many(
    collection: str,
    asset: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 0,
    ts_field: str,
) -> list[Mapping[str, Any]]:
    """Return candle documents for *asset* from ``collection``."""
    from packages.core.logger import get_logger
    logger = get_logger(__name__)

    filt: dict[str, Any] = {"asset": asset}
    if start or end:
        time_cond: dict[str, Any] = {}
        if start:
            time_cond["$gte"] = start
        if end:
            time_cond["$lte"] = end  # Changed from $lt to $lte to include end date
        filt[ts_field] = time_cond

    client = get_client()
    coll = client[MONGO_DB_NAME][collection]
    cursor = coll.find(filt, {"_id": 0}).sort(ts_field, 1)
    if limit:
        cursor = cursor.limit(limit)
    
    # Add timeout to prevent hanging on database read operations
    import asyncio
    try:
        return await asyncio.wait_for(
            cursor.to_list(length=None),
            timeout=15.0  # 15 second timeout for read operations
        )
    except asyncio.TimeoutError:
        logger.error(f"Database read timed out for collection {collection} after 15 seconds")
        return []  # Return empty list on timeout


async def insert_many(candles: Iterable[Mapping[str, Any]]) -> None:
    """Bulk upsert minute candles into the ``candles`` collection."""

    await _insert_many(_COLLECTION, candles, "ts")


async def get_many(
    asset: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 0,
) -> list[Mapping[str, Any]]:
    """Return minute candle documents for *asset* within an optional time range."""

    return await _get_many(
        _COLLECTION, asset, start=start, end=end, limit=limit, ts_field="ts"
    )


async def insert_many_5m(candles: Iterable[Mapping[str, Any]]) -> None:
    """Bulk upsert 5‑minute candles into the ``candles_5m`` collection."""

    await _insert_many(_COLLECTION_5M, candles, "time")


async def get_many_5m(
    asset: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 0,
) -> list[Mapping[str, Any]]:
    """Return 5‑minute candles for *asset* within an optional time range."""

    return await _get_many(
        _COLLECTION_5M, asset, start=start, end=end, limit=limit, ts_field="time"
    )


async def insert_many_15m(candles: Iterable[Mapping[str, Any]]) -> None:
    """Bulk upsert 15‑minute candles into the ``candles_15m`` collection."""

    await _insert_many(_COLLECTION_15M, candles, "time")


async def get_many_15m(
    asset: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 0,
) -> list[Mapping[str, Any]]:
    """Return 15‑minute candles for *asset* within an optional time range."""

    return await _get_many(
        _COLLECTION_15M, asset, start=start, end=end, limit=limit, ts_field="time"
    )


async def count_5m(
    asset: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> int:
    """Count 5-minute candles for *asset* within an optional time range."""
    client = get_client()
    coll = client[MONGO_DB_NAME][_COLLECTION_5M]
    
    filt: dict[str, Any] = {"asset": asset}
    if start or end:
        time_cond: dict[str, Any] = {}
        if start:
            time_cond["$gte"] = start
        if end:
            time_cond["$lte"] = end
        filt["time"] = time_cond
    
    return await coll.count_documents(filt)


async def count_15m(
    asset: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> int:
    """Count 15-minute candles for *asset* within an optional time range."""
    client = get_client()
    coll = client[MONGO_DB_NAME][_COLLECTION_15M]
    
    filt: dict[str, Any] = {"asset": asset}
    if start or end:
        time_cond: dict[str, Any] = {}
        if start:
            time_cond["$gte"] = start
        if end:
            time_cond["$lte"] = end
        filt["time"] = time_cond
    
    return await coll.count_documents(filt)


async def delete_many_5m(
    asset: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> int:
    """Delete 5-minute candles for *asset* within an optional time range.
    
    Returns
    -------
    int
        Number of documents deleted
    """
    client = get_client()
    coll = client[MONGO_DB_NAME][_COLLECTION_5M]
    
    filt: dict[str, Any] = {"asset": asset}
    if start or end:
        time_cond: dict[str, Any] = {}
        if start:
            time_cond["$gte"] = start
        if end:
            time_cond["$lte"] = end
        filt["time"] = time_cond
    
    result = await coll.delete_many(filt)
    return result.deleted_count


async def delete_many_15m(
    asset: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> int:
    """Delete 15-minute candles for *asset* within an optional time range.
    
    Returns
    -------
    int
        Number of documents deleted
    """
    client = get_client()
    coll = client[MONGO_DB_NAME][_COLLECTION_15M]
    
    filt: dict[str, Any] = {"asset": asset}
    if start or end:
        time_cond: dict[str, Any] = {}
        if start:
            time_cond["$gte"] = start
        if end:
            time_cond["$lte"] = end
        filt["time"] = time_cond
    
    result = await coll.delete_many(filt)
    return result.deleted_count


async def delete_all_5m(asset: str) -> int:
    """Delete all 5-minute candles for *asset*.
    
    Returns
    -------
    int
        Number of documents deleted
    """
    return await delete_many_5m(asset)


async def delete_all_15m(asset: str) -> int:
    """Delete all 15-minute candles for *asset*.
    
    Returns
    -------
    int
        Number of documents deleted
    """
    return await delete_many_15m(asset)


__all__ = [
    "insert_many",
    "get_many",
    "insert_many_5m",
    "get_many_5m",
    "insert_many_15m",
    "get_many_15m",
    "count_5m",
    "count_15m",
    "delete_many_5m",
    "delete_many_15m",
    "delete_all_5m",
    "delete_all_15m",
]
