from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse
from pymongo.errors import PyMongoError

from packages.core.config import MONGO_DB_NAME
from packages.db.client import get_client

router = APIRouter()


@router.get("/trades")
async def stream_trades() -> EventSourceResponse:
    """Stream trade changes from MongoDB via server-sent events.

    A heartbeat message is emitted every 30 seconds. The change stream is
    recreated if the underlying connection is lost.
    """

    coll = get_client()[MONGO_DB_NAME]["trades"]

    async def gen():
        while True:
            try:
                async with coll.watch() as stream:
                    while True:
                        try:
                            change = await asyncio.wait_for(stream.next(), timeout=30)
                            yield {"data": json.dumps(change, default=str)}
                        except asyncio.TimeoutError:
                            hb = {"type": "heartbeat", "time": datetime.now(timezone.utc).isoformat()}
                            yield {"data": json.dumps(hb)}
            except PyMongoError:
                continue

    return EventSourceResponse(gen(), headers={"Cache-Control": "no-cache"})
