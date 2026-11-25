"""Persistence for live trade-monitor state — the source of truth for
crash/restart recovery.

Every position the :class:`TradeMonitor` manages is mirrored into the
``monitor_state`` collection at queue time and updated on each state
transition (TP1 partial, BE move, TP2 partial, trail). On process start the
monitor reads the still-open rows back and re-adopts the positions that remain
open at the broker.

All functions are deliberately defensive: a Mongo hiccup logs and returns
without ever raising into the live trading path. Losing a persistence write is
strictly better than interrupting trade management.

This module touches no business/signal logic — it is pure lifecycle I/O.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from packages.db.client import get_client
from packages.core.config import MONGO_DB_NAME

logger = logging.getLogger(__name__)

_COLLECTION = "monitor_state"


def _col():
    return get_client()[MONGO_DB_NAME][_COLLECTION]


def _clean(state: Dict[str, Any] | None) -> Dict[str, Any]:
    """Drop the evaluator's transient per-candle scratch keys (``_pending_*``,
    ``_trail_label``) while keeping the durable trailing cursor (``trail_step``,
    ``trail_consec``, ``cont_trail_r``, ``runner_extreme``) so trailing resumes
    at exactly the right place after recovery."""
    if not state:
        return {}
    return {k: v for k, v in state.items() if not str(k).startswith("_")}


async def upsert_monitor_state(
    *,
    position_ticket: Any,
    symbol: str,
    asset: str,
    trade_state: Dict[str, Any],
    rules: Dict[str, Any],
    expiry: datetime,
    prompt_hash: str = "",
    gpt_mode: str = "",
    latency_ms: float = 0.0,
    trade_id: str = "",
    cycle_id: str = "",
    gpt_response_id: str = "",
    agent_id: str = "",
) -> None:
    """Create or refresh the open-state row for a monitored position."""
    try:
        ticket = str(position_ticket or "")
        now = datetime.now(timezone.utc)
        await _col().update_one(
            {"position_ticket": ticket},
            {
                "$set": {
                    "position_ticket": ticket,
                    "symbol": symbol,
                    "asset": asset or symbol,
                    "trade_state": _clean(trade_state),
                    "rules": _clean(rules),
                    "expiry": expiry,
                    "prompt_hash": prompt_hash,
                    "gpt_mode": gpt_mode,
                    "latency_ms": latency_ms,
                    "trade_id": trade_id,
                    "cycle_id": cycle_id,
                    "gpt_response_id": gpt_response_id,
                    "agent_id": agent_id,
                    "status": "open",
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
    except Exception as e:  # never break trading on a persistence failure
        logger.error(f"monitor_state upsert failed for {symbol}: {e}")


async def update_monitor_state(
    position_ticket: Any,
    *,
    trade_state: Dict[str, Any] | None = None,
    expiry: datetime | None = None,
) -> None:
    """Update the mutable parts of an open row after a state transition."""
    try:
        updates: Dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}
        if trade_state is not None:
            updates["trade_state"] = _clean(trade_state)
        if expiry is not None:
            updates["expiry"] = expiry
        await _col().update_one(
            {"position_ticket": str(position_ticket)}, {"$set": updates}
        )
    except Exception as e:
        logger.error(f"monitor_state update failed for {position_ticket}: {e}")


async def clear_monitor_state(position_ticket: Any, reason: str = "closed") -> None:
    """Mark a monitored position closed so recovery never re-adopts it."""
    try:
        await _col().update_one(
            {"position_ticket": str(position_ticket)},
            {
                "$set": {
                    "status": "closed",
                    "close_reason": reason,
                    "closed_at": datetime.now(timezone.utc),
                }
            },
        )
    except Exception as e:
        logger.error(f"monitor_state clear failed for {position_ticket}: {e}")


async def get_open_monitor_states() -> List[Dict[str, Any]]:
    """Return all rows still flagged ``open`` (the recovery work list)."""
    try:
        cursor = _col().find({"status": "open"})
        out: List[Dict[str, Any]] = []
        async for doc in cursor:
            doc["_id"] = str(doc["_id"])
            out.append(doc)
        return out
    except Exception as e:
        logger.error(f"monitor_state load failed: {e}")
        return []


__all__ = [
    "upsert_monitor_state",
    "update_monitor_state",
    "clear_monitor_state",
    "get_open_monitor_states",
]
