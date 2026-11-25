from __future__ import annotations

import os
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorClient

from packages.core.offline_replay import offline_mode
from packages.core.config import MONGO_URI

_client: Optional[AsyncIOMotorClient] = None
_offline_client = None


def get_client() -> AsyncIOMotorClient:
    """Return a singleton Motor client configured from environment.

    The client uses a fixed connection pool for speed and fails fast when a
    server cannot be selected. ``MONGO_URI`` can be overridden via the
    ``MONGO_URI`` environment variable even after import.
    """
    global _client, _offline_client
    if offline_mode():
        if _offline_client is None:
            from mongomock_motor import AsyncMongoMockClient

            _offline_client = AsyncMongoMockClient(tz_aware=True)
        return _offline_client
    if _client is None:
        uri = os.getenv("MONGO_URI", MONGO_URI)
        _client = AsyncIOMotorClient(
            uri,
            maxPoolSize=50,
            serverSelectionTimeoutMS=900,
        )
    return _client
