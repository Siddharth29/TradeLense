"""Repository for backtest persistence and retrieval."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4
import hashlib

from packages.core.config import MONGO_DB_NAME
from packages.db.client import get_client
from packages.core.logger import get_logger
from packages.db.models import BacktestIterationDoc
from pydantic import ValidationError

logger = get_logger(__name__)

_COLLECTION_BACKTESTS = "backtests"
_COLLECTION_BACKTEST_ITERATIONS = "backtest_iterations"

# Track if indexes have been created
_indexes_created = False


async def ensure_indexes() -> None:
    """Ensure MongoDB indexes are created for optimal query performance."""
    global _indexes_created
    if _indexes_created:
        return
    
    client = get_client()
    backtests_coll = client[MONGO_DB_NAME][_COLLECTION_BACKTESTS]
    iterations_coll = client[MONGO_DB_NAME][_COLLECTION_BACKTEST_ITERATIONS]
    
    try:
        # Backtest collection indexes
        await backtests_coll.create_index([("asset", 1), ("created_at", -1)])
        await backtests_coll.create_index([("status", 1), ("created_at", -1)])
        await backtests_coll.create_index([("created_at", -1)])
        await backtests_coll.create_index([("test_name", 1)])
        
        # Backtest iterations collection indexes
        await iterations_coll.create_index([("backtest_id", 1), ("iteration", 1)])
        await iterations_coll.create_index([("backtest_id", 1), ("timestamp", -1)])
        
        _indexes_created = True
        logger.info("MongoDB indexes created for backtest collections")
    except Exception as e:
        logger.warning(f"Failed to create indexes (may already exist): {e}")


def _generate_checksum(data: Dict[str, Any]) -> str:
    """Generate checksum for data."""
    # Create stable string representation
    stable_str = str(sorted(data.items()))
    return hashlib.sha256(stable_str.encode()).hexdigest()[:16]


def _serialize_for_mongo(obj: Any) -> Any:
    """Recursively serialize objects for MongoDB storage."""
    if isinstance(obj, datetime):
        # Ensure timezone-aware
        if obj.tzinfo is None:
            obj = obj.replace(tzinfo=timezone.utc)
        return obj
    elif isinstance(obj, dict):
        return {k: _serialize_for_mongo(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [_serialize_for_mongo(item) for item in obj]
    else:
        return obj


async def create_backtest(
    test_name: str,
    config: Dict[str, Any],
    asset: str,
    start_date: datetime,
    end_date: datetime
) -> str:
    """Create a new backtest record.
    
    Parameters
    ----------
    test_name : str
        Unique name for the backtest
    config : Dict[str, Any]
        Backtest configuration
    asset : str
        Asset symbol
    start_date : datetime
        Start date (timezone-aware)
    end_date : datetime
        End date (timezone-aware)
    
    Returns
    -------
    str
        Backtest ID (MongoDB _id)
    """
    # Ensure indexes exist
    await ensure_indexes()
    
    client = get_client()
    coll = client[MONGO_DB_NAME][_COLLECTION_BACKTESTS]
    
    # Ensure timezone-aware
    if start_date.tzinfo is None:
        start_date = start_date.replace(tzinfo=timezone.utc)
    if end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=timezone.utc)
    
    # Create document
    now = datetime.now(timezone.utc)
    client_order_id = str(uuid4())
    checksum = _generate_checksum({
        "test_name": test_name,
        "asset": asset,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat()
    })
    
    doc = {
        "test_name": test_name,
        "asset": asset,
        "start_date": start_date,
        "end_date": end_date,
        "config": config,
        "status": "pending",
        "iterations": 0,
        "total_signals": 0,
        "wins": 0,
        "losses": 0,
        "total_pnl": 0.0,
        "max_drawdown": 0.0,
        "win_rate": 0.0,
        "avg_rr": 0.0,
        "created_at": now,
        "completed_at": None,
        "error_message": None,
        "client_order_id": client_order_id,
        "checksum": checksum,
        "version": 1,
        "timestamp": now
    }
    
    result = await coll.insert_one(doc)
    return str(result.inserted_id)


async def get_backtest(backtest_id: str) -> Optional[Dict[str, Any]]:
    """Get backtest by ID.
    
    Parameters
    ----------
    backtest_id : str
        Backtest ID
    
    Returns
    -------
    Optional[Dict[str, Any]]
        Backtest document or None if not found
    """
    client = get_client()
    coll = client[MONGO_DB_NAME][_COLLECTION_BACKTESTS]
    
    try:
        from bson import ObjectId
        doc = await coll.find_one({"_id": ObjectId(backtest_id)})
        if doc:
            doc["_id"] = str(doc["_id"])
        return doc
    except Exception as e:
        logger.error(f"Failed to get backtest {backtest_id}: {e}")
        return None


async def list_backtests(
    asset: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    limit: int = 50
) -> List[Dict[str, Any]]:
    """List backtests with optional filters.
    
    Parameters
    ----------
    asset : str, optional
        Filter by asset
    start_date : datetime, optional
        Filter by start date
    end_date : datetime, optional
        Filter by end date
    limit : int
        Maximum results (default: 50)
    
    Returns
    -------
    List[Dict[str, Any]]
        List of backtest documents
    """
    client = get_client()
    coll = client[MONGO_DB_NAME][_COLLECTION_BACKTESTS]
    
    filt: Dict[str, Any] = {}
    if asset:
        filt["asset"] = asset
    if start_date or end_date:
        time_cond: Dict[str, Any] = {}
        if start_date:
            time_cond["$gte"] = start_date
        if end_date:
            time_cond["$lte"] = end_date
        filt["created_at"] = time_cond
    
    cursor = coll.find(filt).sort("created_at", -1).limit(limit)
    docs = await cursor.to_list(length=None)
    
    # Convert _id to string
    for doc in docs:
        doc["_id"] = str(doc["_id"])
    
    return docs


async def update_backtest_status(
    backtest_id: str,
    status: str,
    error_message: Optional[str] = None
) -> bool:
    """Update backtest status.
    
    Parameters
    ----------
    backtest_id : str
        Backtest ID
    status : str
        New status (pending, running, completed, failed)
    error_message : str, optional
        Error message if status is failed
    
    Returns
    -------
    bool
        True if updated successfully
    """
    client = get_client()
    coll = client[MONGO_DB_NAME][_COLLECTION_BACKTESTS]
    
    try:
        from bson import ObjectId
        update: Dict[str, Any] = {"status": status}
        if error_message:
            update["error_message"] = error_message
        
        result = await coll.update_one(
            {"_id": ObjectId(backtest_id)},
            {"$set": update}
        )
        return result.modified_count > 0
    except Exception as e:
        logger.error(f"Failed to update backtest status: {e}")
        return False


async def update_backtest_progress(
    backtest_id: str,
    iterations: int,
    total_signals: int,
    wins: int,
    losses: int,
    total_pnl: float,
    max_drawdown: float
) -> bool:
    """Update backtest progress metrics.
    
    Parameters
    ----------
    backtest_id : str
        Backtest ID
    iterations : int
        Number of iterations completed
    total_signals : int
        Total signals generated
    wins : int
        Number of winning trades
    losses : int
        Number of losing trades
    total_pnl : float
        Total P&L
    max_drawdown : float
        Maximum drawdown
    
    Returns
    -------
    bool
        True if updated successfully
    """
    client = get_client()
    coll = client[MONGO_DB_NAME][_COLLECTION_BACKTESTS]
    
    try:
        from bson import ObjectId
        update = {
            "iterations": iterations,
            "total_signals": total_signals,
            "wins": wins,
            "losses": losses,
            "total_pnl": total_pnl,
            "max_drawdown": max_drawdown
        }
        
        result = await coll.update_one(
            {"_id": ObjectId(backtest_id)},
            {"$set": update}
        )
        return result.modified_count > 0
    except Exception as e:
        logger.error(f"Failed to update backtest progress: {e}")
        return False


async def update_backtest_completion(
    backtest_id: str,
    iterations: int,
    total_signals: int,
    wins: int,
    losses: int,
    total_pnl: float,
    max_drawdown: float,
    win_rate: float,
    avg_rr: float
) -> bool:
    """Update backtest with final completion metrics.
    
    Parameters
    ----------
    backtest_id : str
        Backtest ID
    iterations : int
        Total iterations
    total_signals : int
        Total signals generated
    wins : int
        Number of winning trades
    losses : int
        Number of losing trades
    total_pnl : float
        Total P&L
    max_drawdown : float
        Maximum drawdown
    win_rate : float
        Win rate (0.0 to 1.0)
    avg_rr : float
        Average risk/reward ratio
    
    Returns
    -------
    bool
        True if updated successfully
    """
    client = get_client()
    coll = client[MONGO_DB_NAME][_COLLECTION_BACKTESTS]
    
    try:
        from bson import ObjectId
        now = datetime.now(timezone.utc)
        update = {
            "status": "completed",
            "iterations": iterations,
            "total_signals": total_signals,
            "wins": wins,
            "losses": losses,
            "total_pnl": total_pnl,
            "max_drawdown": max_drawdown,
            "win_rate": win_rate,
            "avg_rr": avg_rr,
            "completed_at": now
        }
        
        result = await coll.update_one(
            {"_id": ObjectId(backtest_id)},
            {"$set": update}
        )
        return result.modified_count > 0
    except Exception as e:
        logger.error(f"Failed to update backtest completion: {e}")
        return False


async def add_iteration(
    backtest_id: str,
    iteration_data: Dict[str, Any]
) -> str:
    """Add an iteration result to a backtest.
    
    Validates iteration data using Pydantic models before storing.
    
    Parameters
    ----------
    backtest_id : str
        Backtest ID
    iteration_data : Dict[str, Any]
        Iteration data (will be validated and serialized for MongoDB)
    
    Returns
    -------
    str
        Iteration document ID
    
    Raises
    ------
    ValidationError
        If iteration data fails validation
    ValueError
        If required fields are missing or invalid
    """
    client = get_client()
    coll = client[MONGO_DB_NAME][_COLLECTION_BACKTEST_ITERATIONS]
    
    # Ensure backtest_id is in iteration_data
    iteration_data_with_id = {**iteration_data, "backtest_id": backtest_id}
    
    # Ensure iteration_timestamp is a datetime if it's a string
    if "iteration_timestamp" in iteration_data_with_id:
        ts = iteration_data_with_id["iteration_timestamp"]
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                iteration_data_with_id["iteration_timestamp"] = ts
            except (ValueError, AttributeError) as e:
                logger.warning(f"Failed to parse iteration_timestamp: {ts}, error: {e}")
    
    # Validate using Pydantic model
    try:
        validated_doc = BacktestIterationDoc(**iteration_data_with_id)
        # Convert Pydantic model to dict for MongoDB
        doc_dict = validated_doc.model_dump(mode="json", exclude_none=False)
        doc_dict["iteration_timestamp"] = validated_doc.iteration_timestamp
    except ValidationError as e:
        error_details = []
        for error in e.errors():
            field_path = " -> ".join(str(loc) for loc in error["loc"])
            error_msg = f"{field_path}: {error['msg']}"
            if "ctx" in error:
                error_msg += f" (context: {error['ctx']})"
            error_details.append(error_msg)
        
        error_message = f"Validation failed for backtest iteration {backtest_id}: " + "; ".join(error_details)
        logger.error(
            error_message,
            extra={
                "backtest_id": backtest_id,
                "iteration": iteration_data.get("iteration"),
                "validation_errors": e.errors(),
                "iteration_data_keys": list(iteration_data.keys())
            }
        )
        raise ValueError(error_message) from e
    except Exception as e:
        logger.error(
            f"Unexpected error validating iteration data: {e}",
            extra={
                "backtest_id": backtest_id,
                "iteration": iteration_data.get("iteration"),
                "error_type": type(e).__name__
            },
            exc_info=True
        )
        raise ValueError(f"Failed to validate iteration data: {str(e)}") from e
    
    # Serialize for MongoDB (handle datetime, etc.)
    serialized_data = _serialize_for_mongo(doc_dict)
    
    now = datetime.now(timezone.utc)
    client_order_id = str(uuid4())
    checksum = _generate_checksum({
        "backtest_id": backtest_id,
        "iteration": iteration_data.get("iteration"),
        "timestamp": str(iteration_data.get("iteration_timestamp", now))
    })
    
    doc = {
        **serialized_data,
        "client_order_id": client_order_id,
        "checksum": checksum,
        "version": 1,
        "timestamp": now
    }
    
    try:
        result = await coll.insert_one(doc)
        logger.debug(
            f"Successfully stored iteration {iteration_data.get('iteration')} for backtest {backtest_id}",
            extra={
                "backtest_id": backtest_id,
                "iteration": iteration_data.get("iteration"),
                "iteration_id": str(result.inserted_id)
            }
        )
        return str(result.inserted_id)
    except Exception as e:
        logger.error(
            f"Failed to insert iteration into MongoDB: {e}",
            extra={
                "backtest_id": backtest_id,
                "iteration": iteration_data.get("iteration"),
                "error_type": type(e).__name__
            },
            exc_info=True
        )
        raise


def _deserialize_from_mongo(obj: Any) -> Any:
    """Recursively deserialize MongoDB objects for JSON serialization."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    elif isinstance(obj, dict):
        return {k: _deserialize_from_mongo(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [_deserialize_from_mongo(item) for item in obj]
    else:
        return obj


async def add_iterations_bulk(
    backtest_id: str,
    iterations: List[Dict[str, Any]],
) -> int:
    """Bulk-insert a list of pre-normalized iteration dicts for a backtest.

    Each item in *iterations* must already have been processed by
    ``normalize_iteration_data`` (i.e. it's in the same shape that
    ``add_iteration`` would write to MongoDB).  The function validates,
    serialises, and inserts them in a single ``insert_many`` call.

    Parameters
    ----------
    backtest_id : str
        Backtest ID that owns these iterations.
    iterations : List[Dict[str, Any]]
        Pre-normalised iteration dicts.

    Returns
    -------
    int
        Number of documents successfully inserted.
    """
    if not iterations:
        return 0

    client = get_client()
    coll = client[MONGO_DB_NAME][_COLLECTION_BACKTEST_ITERATIONS]
    now = datetime.now(timezone.utc)

    docs = []
    for iteration_data in iterations:
        iteration_data_with_id = {**iteration_data, "backtest_id": backtest_id}

        # Normalise iteration_timestamp string → datetime
        if "iteration_timestamp" in iteration_data_with_id:
            ts = iteration_data_with_id["iteration_timestamp"]
            if isinstance(ts, str):
                try:
                    ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    iteration_data_with_id["iteration_timestamp"] = ts
                except (ValueError, AttributeError) as e:
                    logger.warning(f"Failed to parse iteration_timestamp: {ts}, error: {e}")

        try:
            validated_doc = BacktestIterationDoc(**iteration_data_with_id)
            doc_dict = validated_doc.model_dump(mode="json", exclude_none=False)
            doc_dict["iteration_timestamp"] = validated_doc.iteration_timestamp
        except ValidationError as e:
            logger.warning(
                f"Skipping invalid iteration {iteration_data.get('iteration')} in bulk insert: {e}",
                extra={"backtest_id": backtest_id, "iteration": iteration_data.get("iteration")},
            )
            continue
        except Exception as e:
            logger.warning(
                f"Unexpected error validating iteration {iteration_data.get('iteration')}: {e}",
                extra={"backtest_id": backtest_id},
            )
            continue

        serialized_data = _serialize_for_mongo(doc_dict)
        checksum = _generate_checksum({
            "backtest_id": backtest_id,
            "iteration": iteration_data.get("iteration"),
            "timestamp": str(iteration_data.get("iteration_timestamp", now)),
        })
        docs.append({
            **serialized_data,
            "client_order_id": str(uuid4()),
            "checksum": checksum,
            "version": 1,
            "timestamp": now,
        })

    if not docs:
        return 0

    try:
        result = await coll.insert_many(docs, ordered=False)
        inserted = len(result.inserted_ids)
        logger.debug(
            "Bulk-inserted %d iterations for backtest %s",
            inserted,
            backtest_id,
        )
        return inserted
    except Exception as e:
        logger.error(
            "Failed to bulk-insert iterations for backtest %s: %s",
            backtest_id,
            e,
            exc_info=True,
        )
        raise


async def get_backtest_iterations(
    backtest_id: str,
    limit: int = 100
) -> List[Dict[str, Any]]:
    """Get all iterations for a backtest.
    
    Parameters
    ----------
    backtest_id : str
        Backtest ID
    limit : int
        Maximum iterations to return (default: 100)
    
    Returns
    -------
    List[Dict[str, Any]]
        List of iteration documents with datetime fields serialized to ISO strings
    """
    client = get_client()
    coll = client[MONGO_DB_NAME][_COLLECTION_BACKTEST_ITERATIONS]
    
    cursor = coll.find({"backtest_id": backtest_id}).sort("iteration", 1).limit(limit)
    docs = await cursor.to_list(length=None)
    
    # Convert _id to string and serialize datetime fields for JSON
    result = []
    for doc in docs:
        doc["_id"] = str(doc["_id"])
        # Deserialize datetime objects to ISO strings for JSON response
        deserialized = _deserialize_from_mongo(doc)
        result.append(deserialized)
    
    return result


async def delete_backtest(backtest_id: str) -> bool:
    """Delete a backtest, all its iterations, and associated candle data.

    Parameters
    ----------
    backtest_id : str
        Backtest ID

    Returns
    -------
    bool
        True if deleted successfully
    """
    from packages.db.repositories import candles as candle_repo

    client = get_client()
    backtests_coll = client[MONGO_DB_NAME][_COLLECTION_BACKTESTS]
    iterations_coll = client[MONGO_DB_NAME][_COLLECTION_BACKTEST_ITERATIONS]

    try:
        from bson import ObjectId

        # Delete associated candle data (5m and 15m)
        try:
            candles_5m_coll = client[MONGO_DB_NAME]["candles_5m"]
            candles_15m_coll = client[MONGO_DB_NAME]["candles_15m"]

            result_5m = await candles_5m_coll.delete_many({"backtest_id": backtest_id})
            result_15m = await candles_15m_coll.delete_many({"backtest_id": backtest_id})

            logger.info(
                f"Deleted {result_5m.deleted_count} 5m candles and {result_15m.deleted_count} 15m candles for backtest {backtest_id}",
                extra={"backtest_id": backtest_id}
            )
        except Exception as candle_error:
            logger.warning(f"Failed to delete candle data: {candle_error}")

        # Delete iterations
        await iterations_coll.delete_many({"backtest_id": backtest_id})

        # Delete backtest
        result = await backtests_coll.delete_one({"_id": ObjectId(backtest_id)})
        return result.deleted_count > 0
    except Exception as e:
        logger.error(f"Failed to delete backtest: {e}")
        return False


async def get_latest_iteration_with_monitoring_state(backtest_id: str) -> Optional[Dict[str, Any]]:
    """Get the latest iteration that has active monitoring state or active trade.

    Parameters
    ----------
    backtest_id : str
        Backtest ID

    Returns
    -------
    Optional[Dict[str, Any]]
        Latest iteration with monitoring state or None if none found
    """
    client = get_client()
    coll = client[MONGO_DB_NAME][_COLLECTION_BACKTEST_ITERATIONS]

    try:
        # Find iterations with monitoring_state or active trade, sorted by iteration descending
        cursor = coll.find({
            "backtest_id": backtest_id,
            "$or": [
                {"monitoring_state": {"$ne": None}},
                {"trade_status": {"$in": ["monitoring", "active"]}}
            ]
        }).sort("iteration", -1).limit(1)

        docs = await cursor.to_list(length=1)
        if docs:
            doc = docs[0]
            doc["_id"] = str(doc["_id"])
            return _deserialize_from_mongo(doc)
        return None
    except Exception as e:
        logger.error(f"Failed to get latest iteration with monitoring state: {e}")
        return None


async def get_last_gpt_call_time(backtest_id: str) -> Optional[datetime]:
    """Get the timestamp of the last GPT call for this backtest.

    Parameters
    ----------
    backtest_id : str
        Backtest ID

    Returns
    -------
    Optional[datetime]
        Timestamp of last GPT call or None if no GPT calls found
    """
    client = get_client()
    coll = client[MONGO_DB_NAME][_COLLECTION_BACKTEST_ITERATIONS]

    try:
        # Find latest iteration where GPT was called (has signal_data or cycle_id)
        cursor = coll.find({
            "backtest_id": backtest_id,
            "$or": [
                {"cycle_id": {"$ne": None}},
                {"signal_data": {"$ne": {}}},
                {"last_gpt_call_time": {"$ne": None}}
            ]
        }).sort("iteration", -1).limit(1)

        docs = await cursor.to_list(length=1)
        if docs:
            doc = docs[0]
            # Try to get from last_gpt_call_time field first
            if doc.get("last_gpt_call_time"):
                ts = doc["last_gpt_call_time"]
                if isinstance(ts, str):
                    return datetime.fromisoformat(ts.replace('Z', '+00:00'))
                return ts
            # Fallback to iteration_timestamp
            return doc.get("iteration_timestamp")
        return None
    except Exception as e:
        logger.error(f"Failed to get last GPT call time: {e}")
        return None

