"""Utility functions for storing and retrieving backtest summaries in MongoDB."""
from __future__ import annotations

import asyncio
import warnings
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

import numpy as np
import pandas as pd
from pymongo import MongoClient

try:  # pragma: no cover - optional dependency for async persistence
    from packages.db.repositories import write as repo
except Exception:  # pragma: no cover - repository unavailable in tests
    repo = None  # type: ignore[assignment]

from .config import (
    MONGO_URI,
    MONGO_DB_NAME,
    MONGO_COLLECTION,
    MONGO_REPORTS_COLLECTION,
    MONGO_TRADES_COLLECTION,
)


def _get_client() -> MongoClient:
    """Return a Mongo client with a short server selection timeout.

    The tests run in an environment without a real MongoDB server.  Using the
    default ``MongoClient`` constructor would block for ~30 seconds while the
    driver tries to discover the server.  By providing a very small
    ``serverSelectionTimeoutMS`` we fail fast, allowing the caller to handle the
    error gracefully or ignore it.  This keeps the test suite snappy and avoids
    hanging when MongoDB isn't available.
    """

    return MongoClient(MONGO_URI, serverSelectionTimeoutMS=200)


def _get_collection():
    """Return the configured MongoDB collection, creating it if necessary."""
    client = _get_client()
    db = client[MONGO_DB_NAME]
    if MONGO_COLLECTION not in db.list_collection_names():
        db.create_collection(MONGO_COLLECTION)
    return db[MONGO_COLLECTION]


def _get_reports_collection():
    """Return the MongoDB collection for detailed backtest reports."""
    client = _get_client()
    db = client[MONGO_DB_NAME]
    if MONGO_REPORTS_COLLECTION not in db.list_collection_names():
        db.create_collection(MONGO_REPORTS_COLLECTION)
    return db[MONGO_REPORTS_COLLECTION]


def _get_trades_collection():
    """Return the MongoDB collection for individual trades."""
    client = _get_client()
    db = client[MONGO_DB_NAME]
    if MONGO_TRADES_COLLECTION not in db.list_collection_names():
        db.create_collection(MONGO_TRADES_COLLECTION)
    return db[MONGO_TRADES_COLLECTION]


def _clean_value(value):
    """Convert numpy and pandas types to plain Python objects."""
    if isinstance(value, (np.generic,)):
        return value.item()
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    return value


def _clean_doc(doc: dict) -> dict:
    """Return a copy of *doc* with values converted for BSON encoding."""
    return {k: _clean_value(v) for k, v in doc.items()}


def save_backtest_result(test_name: str, summary: dict, collection=None) -> None:
    """Save a backtest summary document to MongoDB.

    Parameters
    ----------
    test_name:
        Unique name for the backtest run.
    summary:
        Dictionary containing summary metrics such as risk-to-reward and
        total API calls. These values are stored along with a timestamp.
    collection:
        Optional collection to use (primarily for testing). If omitted the
        configured collection is used.
    """
    col = collection or _get_collection()
    doc = {
        "test_name": test_name,
        "date": datetime.now(timezone.utc),
        **_clean_doc(summary),
    }
    col.insert_one(doc)


def _save_backtest_report_sync(
    test_name: str, trades: Iterable[dict], *, collection=None
) -> None:
    """Legacy synchronous implementation of :func:`save_backtest_report_async`."""
    col = collection or _get_reports_collection()
    trade_list = [_clean_doc(t) for t in trades]

    real_trades = [t for t in trade_list if str(t.get("result")) != "No Entry"]
    total_api_calls = len(trade_list)

    rr_values = [
        t.get("risk_to_reward") for t in real_trades if t.get("risk_to_reward") is not None
    ]
    avg_rr = sum(rr_values) / len(rr_values) if rr_values else 0.0
    total_trades = len(real_trades)
    stop_loss_hits = sum(1 for t in real_trades if str(t.get("result")) == "SL")
    wins = sum(1 for t in real_trades if (t.get("points") or 0) > 0)
    win_rate = wins / total_trades if total_trades else 0.0
    avg_pl = (
        sum((t.get("points") or 0.0) for t in real_trades) / total_trades
        if total_trades
        else 0.0
    )
    metrics = {
        "risk_to_reward": avg_rr,
        "total_trades": total_trades,
        "stop_loss_hits": stop_loss_hits,
        "win_rate": win_rate,
        "average_profit_loss": avg_pl,
        "total_api_calls": total_api_calls,
    }

    doc = {
        "test_name": test_name,
        "created_at": datetime.now(timezone.utc),
        "trades": trade_list,
        "metrics": _clean_doc(metrics),
    }
    col.insert_one(doc)


async def save_backtest_report_async(test_name: str, trades: Iterable[dict]) -> None:
    """Async wrapper to persist a full backtest run using M4 repositories."""
    if repo is None:  # pragma: no cover - requires M4 runtime
        raise RuntimeError("M4 repositories unavailable")
    trade_list = [_clean_doc(t) for t in trades]

    real_trades = [t for t in trade_list if str(t.get("result")) != "No Entry"]
    total_api_calls = len(trade_list)

    rr_values = [
        t.get("risk_to_reward") for t in real_trades if t.get("risk_to_reward") is not None
    ]
    avg_rr = sum(rr_values) / len(rr_values) if rr_values else 0.0
    total_trades = len(real_trades)
    stop_loss_hits = sum(1 for t in real_trades if str(t.get("result")) == "SL")
    wins = sum(1 for t in real_trades if (t.get("points") or 0) > 0)
    win_rate = wins / total_trades if total_trades else 0.0
    avg_pl = (
        sum((t.get("points") or 0.0) for t in real_trades) / total_trades
        if total_trades
        else 0.0
    )
    metrics = {
        "risk_to_reward": avg_rr,
        "total_trades": total_trades,
        "stop_loss_hits": stop_loss_hits,
        "win_rate": win_rate,
        "average_profit_loss": avg_pl,
        "total_api_calls": total_api_calls,
    }

    doc = {
        "test_name": test_name,
        "created_at": datetime.now(timezone.utc),
        "trades": trade_list,
        "metrics": _clean_doc(metrics),
    }
    await repo.insert_backtest_report(doc)


def save_backtest_report(
    test_name: str, trades: Iterable[dict], *, collection=None
) -> None:
    """Deprecated synchronous wrapper for :func:`save_backtest_report_async`."""
    warnings.warn(
        "save_backtest_report is deprecated; use save_backtest_report_async",
        DeprecationWarning,
        stacklevel=2,
    )
    if collection is not None:
        _save_backtest_report_sync(test_name, trades, collection=collection)
    else:
        asyncio.run(save_backtest_report_async(test_name, trades))


def _save_trades_sync(
    test_name: str,
    trades: Iterable[dict],
    *,
    collection=None,
) -> int:
    """Legacy synchronous implementation of :func:`save_trades_async`."""
    col = collection or _get_trades_collection()
    docs = []
    for t in trades:
        result = str(t.get("result")) if t.get("result") is not None else ""
        if result == "No Entry":
            continue
        d = {
            "test_name": test_name,
            "created_at": datetime.now(timezone.utc),
            **_clean_doc(t),
        }
        docs.append(d)
    if docs:
        col.insert_many(docs)
    return len(docs)


async def save_trades_async(
    test_name: str,
    trades: Iterable[dict],
) -> int:
    """Async wrapper to insert individual trade documents via repositories."""
    if repo is None:  # pragma: no cover - requires M4 runtime
        raise RuntimeError("M4 repositories unavailable")
    docs = []
    for t in trades:
        result = str(t.get("result")) if t.get("result") is not None else ""
        if result == "No Entry":
            continue
        d = {
            "test_name": test_name,
            "created_at": datetime.now(timezone.utc),
            **_clean_doc(t),
        }
        docs.append(d)
    await repo.insert_backtest_trades(docs)
    return len(docs)


def save_trades(
    test_name: str,
    trades: Iterable[dict],
    *,
    collection=None,
) -> int:
    """Deprecated synchronous wrapper for :func:`save_trades_async`."""
    warnings.warn(
        "save_trades is deprecated; use save_trades_async",
        DeprecationWarning,
        stacklevel=2,
    )
    if collection is not None:
        return _save_trades_sync(test_name, trades, collection=collection)
    return asyncio.run(save_trades_async(test_name, trades))


def fetch_reports(
    test_name: Optional[str] = None,
    *,
    include_trades: bool = False,
    collection=None,
) -> list[dict]:
    """Return stored backtest reports.

    Parameters
    ----------
    test_name:
        Optional test name to filter by.
    include_trades:
        When ``True`` trade data is included, otherwise it is omitted.
    collection:
        Optional Mongo collection used primarily for testing.
    """

    col = collection or _get_reports_collection()
    query: dict[str, object] = {}
    if test_name:
        query["test_name"] = test_name
    projection = None if include_trades else {"trades": 0}
    return list(col.find(query, projection))


def query_backtests(
    test_name: Optional[str] = None,
    date: Optional[datetime | str] = None,
    *,
    collection=None,
) -> list[dict]:
    """Query stored backtests by ``test_name`` or specific date.

    ``date`` may be either a :class:`datetime` or an ISO ``YYYY-MM-DD`` string.
    When provided, records from that day (UTC) are returned.
    """
    col = collection or _get_collection()
    query: dict[str, object] = {}
    if test_name:
        query["test_name"] = test_name
    if date:
        if isinstance(date, str):
            start = datetime.fromisoformat(date)
        else:
            start = date
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        query["date"] = {"$gte": start, "$lt": end}
    return list(col.find(query))
