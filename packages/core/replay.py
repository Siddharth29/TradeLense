from __future__ import annotations

"""Utilities to reconstruct a decision timeline from database records."""

from datetime import datetime
from typing import Any, Iterable
import asyncio

from packages.db.repositories import read


async def build_replay(
    asset: str,
    start: datetime | None = None,
    end: datetime | None = None,
) -> Iterable[dict[str, Any]]:
    """Build an ordered sequence of decision events for *asset*.

    Trades and orders are fetched from the database and merged on their
    ``client_order_id``.  The resulting events are sorted by time and each
    contains the order and trade documents (when available).
    """

    orders_task = asyncio.create_task(read.get_orders(asset, start=start, end=end))
    trades_task = asyncio.create_task(read.get_trades(asset, start=start, end=end))
    orders, trades = await asyncio.gather(orders_task, trades_task)

    orders_by_id = {o["client_order_id"]: o for o in orders}
    trades_by_id = {t["client_order_id"]: t for t in trades}

    events: list[dict[str, Any]] = []
    for cid in sorted(set(orders_by_id) | set(trades_by_id)):
        order = orders_by_id.get(cid)
        trade = trades_by_id.get(cid)
        times = [d["time"] for d in (order, trade) if d and "time" in d]
        time = min(times) if times else None
        events.append(
            {
                "client_order_id": cid,
                "time": time,
                "order": order,
                "trade": trade,
            }
        )

    events.sort(key=lambda e: e["time"] or datetime.min)
    return events


__all__ = ["build_replay"]
