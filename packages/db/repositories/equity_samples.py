"""Equity-curve sample repository.

Persists a lightweight intraday time-series of account equity so the dashboard
can render a real equity curve for the current trading day. Samples are written
by a background sampler task (never on a request/trade path), so these writes add
no latency to the trading system. Degrades gracefully (no-op / empty) when Mongo
analytics persistence is disabled.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from packages.core.config import MONGO_DB_NAME
from packages.db.client import get_client
from packages.db.persistence_gate import analytics_persistence_enabled, log_event
from packages.core.logger import get_logger

logger = get_logger(__name__)

_COLLECTION_EQUITY_SAMPLES = "equity_samples"
_index_ensured = False


async def insert_equity_sample(
    broker: str,
    equity: float,
    balance: Optional[float] = None,
    ts: Optional[datetime] = None,
) -> bool:
    """Append one equity sample. Best-effort; never raises.

    Always emits a durable ``equity_sample`` event (log-only-mode parity) and,
    when Mongo analytics persistence is enabled, inserts the sample into the
    ``equity_samples`` collection (creating a ``timestamp`` index once).
    """
    ts = ts or datetime.now(timezone.utc)
    doc: dict[str, Any] = {
        "broker": broker,
        "equity": float(equity),
        "balance": float(balance) if balance is not None else None,
        "timestamp": ts,
    }

    try:
        log_event("equity_sample", doc, broker=broker)
    except Exception:
        pass

    if not analytics_persistence_enabled():
        return True

    global _index_ensured
    try:
        client = get_client()
        coll = client[MONGO_DB_NAME][_COLLECTION_EQUITY_SAMPLES]
        if not _index_ensured:
            try:
                await coll.create_index("timestamp")
            except Exception:  # pragma: no cover - index best-effort
                pass
            _index_ensured = True
        await coll.insert_one(dict(doc))
        return True
    except Exception as e:  # pragma: no cover - persistence must never crash sampler
        logger.warning(f"insert_equity_sample failed: {e}")
        return False


async def get_equity_samples(
    since: datetime,
    end: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """Return equity samples at/after *since* (optionally before *end*), ordered
    by time. Empty when Mongo is off. Dashboard read only."""
    if not analytics_persistence_enabled():
        return []
    try:
        client = get_client()
        coll = client[MONGO_DB_NAME][_COLLECTION_EQUITY_SAMPLES]
        time_cond: dict[str, Any] = {"$gte": since}
        if end is not None:
            time_cond["$lt"] = end
        cursor = coll.find({"timestamp": time_cond}, {"_id": 0}).sort("timestamp", 1)
        return await cursor.to_list(length=None)
    except Exception as e:  # pragma: no cover - dashboard read must never crash
        logger.warning(f"get_equity_samples failed: {e}")
        return []


__all__ = ["insert_equity_sample", "get_equity_samples"]
