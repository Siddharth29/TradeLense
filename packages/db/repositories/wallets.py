from __future__ import annotations

"""Repository helpers for wallet snapshot persistence."""

from typing import Any, Mapping

from packages.core.config import MONGO_DB_NAME
from packages.db.client import get_client
from packages.db.models import WalletSnapshotDoc


async def insert_wallet_snapshot(snapshot: WalletSnapshotDoc) -> Any:
    """Insert *snapshot* into the wallet_snapshots collection."""
    client = get_client()
    coll = client[MONGO_DB_NAME]["wallet_snapshots"]
    result = await coll.insert_one(snapshot.model_dump())
    return result.inserted_id


async def get_last_wallet_snapshot(broker: str) -> Mapping[str, Any] | None:
    """Return the most recent wallet snapshot for *broker*."""
    client = get_client()
    coll = client[MONGO_DB_NAME]["wallet_snapshots"]
    doc = await coll.find_one({"broker": broker}, sort=[("as_of", -1)])
    if doc:
        doc.pop("_id", None)
    return doc


__all__ = ["insert_wallet_snapshot", "get_last_wallet_snapshot"]
