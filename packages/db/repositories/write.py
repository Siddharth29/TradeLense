from __future__ import annotations

"""Async bulk insert helpers for MongoDB collections."""

from typing import Iterable, Mapping, Any, List
import asyncio

from packages.core.config import (
    MONGO_DB_NAME,
    MONGO_REPORTS_COLLECTION,
    MONGO_TRADES_COLLECTION,
)
from packages.db.client import get_client
from packages.db import models
from src.common import logger

# Global lock for MongoDB operations to prevent race conditions
_mongo_lock = asyncio.Lock()


async def _bulk_upsert(collection: str, docs: Iterable[object]) -> List[Any]:
    """Upsert *docs* into *collection* idempotently.

    Each document is matched using ``client_order_id`` or ``checksum``. Any
    failure during upsert is logged using :mod:`common.logger` with structured
    context.  A list of ``_id`` values for newly upserted documents is
    returned to allow callers to verify writes.
    """
    docs = list(docs)
    if not docs:
        return []
    client = get_client()
    coll = client[MONGO_DB_NAME][collection]
    upserted_ids: List[Any] = []
    for doc in docs:
        if hasattr(doc, "model_dump"):
            data: Mapping[str, Any] = doc.model_dump()
            client_order_id = getattr(doc, "client_order_id", None)
            checksum = getattr(doc, "checksum", None)
        else:
            data = dict(doc)  # type: ignore[arg-type]
            client_order_id = data.get("client_order_id")
            checksum = data.get("checksum")
        filt = {"$or": [{"client_order_id": client_order_id}, {"checksum": checksum}]}
        try:
            result = await coll.update_one(filt, {"$set": data}, upsert=True)
        except Exception as exc:  # pragma: no cover - network/driver errors
            logger.error(
                {"collection": collection},
                "failed to upsert document",
                error=str(exc),
                client_order_id=client_order_id,
                checksum=checksum,
            )
            continue

        if not getattr(result, "upserted_id", None) and not getattr(
            result, "matched_count", 0
        ):
            logger.error(
                {"collection": collection},
                "upsert resulted in neither match nor insert",
                client_order_id=client_order_id,
                checksum=checksum,
            )
            continue

        if getattr(result, "upserted_id", None) is not None:
            upserted_ids.append(result.upserted_id)

    return upserted_ids


async def insert_trades(trades: Iterable[models.TradeDoc]) -> List[Any]:
    """Thread-safe trade insertion to prevent race conditions."""
    async with _mongo_lock:
        return await _bulk_upsert("trades", trades)


async def insert_orders(orders: Iterable[models.OrderDoc]) -> List[Any]:
    return await _bulk_upsert("orders", orders)


async def insert_fills(fills: Iterable[models.FillDoc]) -> List[Any]:
    return await _bulk_upsert("fills", fills)


async def insert_signals(signals: Iterable[models.SignalDoc]) -> List[Any]:
    return await _bulk_upsert("signals", signals)


async def insert_logs(logs: Iterable[Mapping[str, Any]]) -> List[Any]:
    return await _bulk_upsert("logs", logs)


async def insert_daily_metrics(metrics: Iterable[models.DailyMetricDoc]) -> List[Any]:
    """Upsert daily metrics documents."""
    return await _bulk_upsert("daily_metrics", metrics)


async def insert_backtest_report(report: Mapping[str, Any]) -> None:
    """Insert a backtest report document into the reports collection."""
    client = get_client()
    coll = client[MONGO_DB_NAME][MONGO_REPORTS_COLLECTION]
    await coll.insert_one(report)


async def insert_backtest_trades(trades: Iterable[Mapping[str, Any]]) -> None:
    """Insert raw backtest trade documents into the trades collection."""
    trades = list(trades)
    if not trades:
        return
    client = get_client()
    coll = client[MONGO_DB_NAME][MONGO_TRADES_COLLECTION]
    await coll.insert_many(trades)
