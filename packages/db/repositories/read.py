from __future__ import annotations

"""Async query helpers for MongoDB collections."""

from datetime import datetime
from typing import Any, Mapping

from packages.core.config import MONGO_DB_NAME
from packages.db.client import get_client


def _time_filter(
    asset: str | None = None,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict[str, Any]:
    """Return a filter matching ``asset`` and optional time range."""
    filt: dict[str, Any] = {}
    if asset is not None:
        filt["asset"] = asset
    if start or end:
        time_cond: dict[str, Any] = {}
        if start:
            time_cond["$gte"] = start
        if end:
            time_cond["$lt"] = end
        filt["time"] = time_cond
    return filt


async def _fetch(
    collection: str,
    filt: Mapping[str, Any],
    *,
    limit: int = 0,
    projection: Mapping[str, int] | None = None,
) -> list[Mapping[str, Any]]:
    """Fetch documents from *collection* applying *filt* and *projection*."""
    client = get_client()
    coll = client[MONGO_DB_NAME][collection]
    cursor = coll.find(filt, projection or {"_id": 0}).sort("time", 1)
    if limit:
        cursor = cursor.limit(limit)
    return await cursor.to_list(length=None)


async def get_trades(
    asset: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 0,
) -> list[Mapping[str, Any]]:
    """Return trade documents for *asset* within optional time range."""
    filt = _time_filter(asset, start=start, end=end)
    return await _fetch("trades", filt, limit=limit)


async def get_orders(
    asset: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 0,
) -> list[Mapping[str, Any]]:
    """Return order documents for *asset* within optional time range."""
    filt = _time_filter(asset, start=start, end=end)
    return await _fetch("orders", filt, limit=limit)


async def get_fills(
    asset: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 0,
) -> list[Mapping[str, Any]]:
    """Return fill documents for *asset* within optional time range."""
    filt = _time_filter(asset, start=start, end=end)
    return await _fetch("fills", filt, limit=limit)


async def list_signals(
    asset: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 0,
) -> list[Mapping[str, Any]]:
    """List trading signals for *asset* within optional time range."""
    filt = _time_filter(asset, start=start, end=end)
    return await _fetch("signals", filt, limit=limit)


async def list_logs(
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 0,
) -> list[Mapping[str, Any]]:
    """List log entries within optional time range."""
    filt = _time_filter(None, start=start, end=end)
    return await _fetch("logs", filt, limit=limit)


async def get_candles(
    asset: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 0,
) -> list[Mapping[str, Any]]:
    """Return candle documents for *asset* within optional time range."""
    filt = _time_filter(asset, start=start, end=end)
    return await _fetch("candles_5m", filt, limit=limit)


__all__ = [
    "get_trades",
    "get_orders",
    "get_fills",
    "list_signals",
    "list_logs",
    "get_candles",
]
