from __future__ import annotations

"""Archive expired MongoDB documents to Parquet.

This script exports documents older than the TTL window for each managed
collection to Parquet and then deletes them.  It is intended to be run before
MongoDB's TTL monitor removes the documents so that a backup is kept on disk.
"""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from packages.core.config import MONGO_DB_NAME
from packages.db.client import get_client
from packages.db import bootstrap

# Mapping of collection name to TTL in seconds.
TTL_WINDOWS: Mapping[str, int] = {
    "logs": bootstrap._LOG_TTL_SECONDS,
    "signals": bootstrap._SIGNAL_TTL_SECONDS,
}


async def archive_collection(
    coll: Any,
    name: str,
    ttl_seconds: int,
    output_dir: Path,
    *,
    now: datetime | None = None,
) -> Path | None:
    """Archive documents from *coll* older than the TTL.

    Returns the path of the created Parquet file or ``None`` if no documents
    matched the TTL window.
    """

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=ttl_seconds)
    cursor = coll.find({"time": {"$lt": cutoff}})
    docs = await cursor.to_list(None)
    if not docs:
        return None

    df = pd.DataFrame(docs)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{name}_{int(now.timestamp())}.parquet"
    df.to_parquet(path)
    ids = [doc["_id"] for doc in docs]
    await coll.delete_many({"_id": {"$in": ids}})
    return path


async def archive_old_data(output_dir: Path) -> None:
    """Archive documents for all TTL-managed collections."""

    client = get_client()
    db = client[MONGO_DB_NAME]
    for name, ttl in TTL_WINDOWS.items():
        coll = db[name]
        await archive_collection(coll, name, ttl, output_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description="Archive expired MongoDB data")
    parser.add_argument(
        "--output",
        default=".",
        help="Directory to write Parquet archives",
    )
    args = parser.parse_args()
    asyncio.run(archive_old_data(Path(args.output)))


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    main()
