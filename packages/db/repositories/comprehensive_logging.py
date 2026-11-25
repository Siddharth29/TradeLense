"""Comprehensive logging and slot-candle matching repository."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from packages.core.config import MONGO_DB_NAME
from packages.core.models import RecentTradeView
from packages.db.client import get_client
from packages.db.models import SlotCandleMatchDoc, ComprehensiveTradeDoc, HourlyCandleDoc
from packages.db.persistence_gate import (
    analytics_persistence_enabled,
    log_event,
    pop_open_trade,
    register_open_trade,
    synthetic_id,
)
from packages.core.logger import get_logger

logger = get_logger(__name__)

# Collection names
_COLLECTION_SLOT_CANDLE_MATCH = "slot_candle_matches"
_COLLECTION_COMPREHENSIVE_TRADES = "comprehensive_trades"
_COLLECTION_HOURLY_CANDLES = "hourly_candles"


def _generate_checksum(data: Mapping[str, Any]) -> str:
    """Generate a checksum for data integrity."""
    # Create a stable string representation
    stable_str = str(sorted(data.items()))
    return hashlib.sha256(stable_str.encode()).hexdigest()


async def store_slot_candle_match(
    asset: str,
    slot_id: int,
    slot_timestamp: datetime,
    gpt_response_id: str,
    level_drawn_at: datetime,
    level_value: float,
    direction: str,
    trigger_met: bool,
    agent_id: str,
    prompt_hash: str,
    trigger_check_id: Optional[str] = None,
    trigger_timestamp: Optional[datetime] = None,
    trigger_candle_timestamp: Optional[datetime] = None,
    trigger_candle_open: Optional[float] = None,
    trigger_candle_high: Optional[float] = None,
    trigger_candle_low: Optional[float] = None,
    trigger_candle_close: Optional[float] = None,
    trigger_candle_volume: Optional[float] = None,
    entry_price: Optional[float] = None,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    confidence: Optional[float] = None,
    pattern: Optional[str] = None,
    broker_type: Optional[str] = None,
    gpt_mode: Optional[str] = None,
    trade_executed: bool = False,
    trade_id: Optional[str] = None,
) -> str:
    """Store a slot-candle match record with full precision."""

    timestamp = datetime.now(timezone.utc)
    client_order_id = f"slot_match_{asset}_{slot_id}_{timestamp.isoformat()}"
    
    # Create the document data with full precision
    doc_data = {
        "version": 1,
        "timestamp": timestamp,
        "client_order_id": client_order_id,
        "checksum": "",  # Will be set after creating the doc
        "asset": asset,
        "slot_id": slot_id,
        "slot_timestamp": slot_timestamp,
        "gpt_response_id": gpt_response_id,
        "trigger_check_id": trigger_check_id,
        "level_drawn_at": level_drawn_at,
        "level_value": float(level_value),  # Ensure it's a float but preserve precision
        "direction": direction,
        "trigger_met": trigger_met,
        "trigger_timestamp": trigger_timestamp,
        "trigger_candle_timestamp": trigger_candle_timestamp,
        "trigger_candle_open": float(trigger_candle_open) if trigger_candle_open is not None else None,
        "trigger_candle_high": float(trigger_candle_high) if trigger_candle_high is not None else None,
        "trigger_candle_low": float(trigger_candle_low) if trigger_candle_low is not None else None,
        "trigger_candle_close": float(trigger_candle_close) if trigger_candle_close is not None else None,
        "trigger_candle_volume": float(trigger_candle_volume) if trigger_candle_volume is not None else None,
        "entry_price": float(entry_price) if entry_price is not None else None,
        "stop_loss": float(stop_loss) if stop_loss is not None else None,
        "take_profit": float(take_profit) if take_profit is not None else None,
        "confidence": float(confidence) if confidence is not None else None,
        "pattern": pattern,
        "agent_id": agent_id,
        "prompt_hash": prompt_hash,
        "broker_type": broker_type,
        "gpt_mode": gpt_mode,
        "trade_executed": trade_executed,
        "trade_id": trade_id,
    }
    
    # Generate checksum
    doc_data["checksum"] = _generate_checksum(doc_data)
    
    # Validate with Pydantic model
    doc = SlotCandleMatchDoc(**doc_data)
    doc_dump = doc.model_dump()

    # Durable structured event (system-of-record in log-only mode).
    log_event(
        "slot_match", doc_dump, asset=asset, agent_id=agent_id,
        gpt_response_id=gpt_response_id, trade_id=trade_id,
    )
    if not analytics_persistence_enabled():
        return synthetic_id()

    # Insert into database
    client = get_client()
    collection = client[MONGO_DB_NAME][_COLLECTION_SLOT_CANDLE_MATCH]
    result = await collection.insert_one(doc_dump)

    logger.info(
        f"Stored slot-candle match for {asset} slot {slot_id}",
        extra={
            "asset": asset,
            "slot_id": slot_id,
            "agent_id": agent_id,
            "level_value": level_value,
            "trigger_met": trigger_met,
            "trade_executed": trade_executed,
        }
    )

    return str(result.inserted_id)


async def store_comprehensive_trade(
    symbol: str,
    asset: str,
    quantity: float,
    price: float,
    entry_price: float,
    agent_id: str,
    prompt_hash: str,
    signal_received_at: datetime,
    trade_executed_at: datetime,
    gpt_response_id: Optional[str] = None,
    cycle_id: Optional[str] = None,
    slot_candle_match_id: Optional[str] = None,
    exit_price: Optional[float] = None,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    trade_type: str = "live",
    side: Optional[str] = None,
    pattern: Optional[str] = None,
    confidence: Optional[str] = None,
    broker_type: Optional[str] = None,
    gpt_mode: Optional[str] = None,
    trade_closed_at: Optional[datetime] = None,
    pnl: Optional[float] = None,
    fees: Optional[float] = None,
    slippage: Optional[float] = None,
    trade_status: str = "open",
    exit_reason: Optional[str] = None,
    position_ticket: Optional[str] = None,
    parent_trade_id: Optional[str] = None,
    trade_reason: Optional[str] = None,
    trigger_reason: Optional[str] = None,
    ai_cost: Optional[dict] = None,
    broker_cost: Optional[dict] = None,
) -> str:
    """Store a comprehensive trade record with full precision."""

    timestamp = datetime.now(timezone.utc)
    client_order_id = f"trade_{symbol}_{timestamp.isoformat()}"
    
    # Create the document data with full precision
    doc_data = {
        "version": 1,
        "timestamp": timestamp,
        "client_order_id": client_order_id,
        "checksum": "",  # Will be set after creating the doc
        "symbol": symbol,
        "asset": asset,
        "quantity": float(quantity),
        "price": float(price),
        "entry_price": float(entry_price),
        "exit_price": float(exit_price) if exit_price is not None else None,
        "stop_loss": float(stop_loss) if stop_loss is not None else None,
        "take_profit": float(take_profit) if take_profit is not None else None,
        "agent_id": agent_id,
        "prompt_hash": prompt_hash,
        "gpt_response_id": gpt_response_id,
        "cycle_id": cycle_id,
        "slot_candle_match_id": slot_candle_match_id,
        "trade_type": trade_type,
        "side": side,
        "pattern": pattern,
        "confidence": confidence,
        "broker_type": broker_type,
        "gpt_mode": gpt_mode,
        "signal_received_at": signal_received_at,
        "trade_executed_at": trade_executed_at,
        "trade_closed_at": trade_closed_at,
        "pnl": float(pnl) if pnl is not None else None,
        "fees": float(fees) if fees is not None else None,
        "slippage": float(slippage) if slippage is not None else None,
        "trade_status": trade_status,
        "exit_reason": exit_reason,
        "position_ticket": str(position_ticket) if position_ticket else None,
        "parent_trade_id": str(parent_trade_id) if parent_trade_id else None,
        "trade_reason": trade_reason,
        "trigger_reason": trigger_reason,
        "ai_cost": ai_cost,
        "broker_cost": broker_cost,
    }

    # Generate checksum
    doc_data["checksum"] = _generate_checksum(doc_data)
    
    # Validate with Pydantic model
    doc = ComprehensiveTradeDoc(**doc_data)
    doc_dump = doc.model_dump()

    # Durable structured event — system-of-record in log-only mode (and a
    # harmless duplicate of the Mongo doc when persistence is enabled).
    log_event(
        "trade_exit" if doc.trade_status == "closed" else "trade_open",
        doc_dump, asset=asset, symbol=symbol,
        cycle_id=cycle_id, position_ticket=position_ticket, agent_id=agent_id,
    )

    if not analytics_persistence_enabled():
        # No Mongo doc is created. Keep a tiny in-process record keyed by the
        # broker ticket (preferred) and the synthetic id so the close path can
        # still feed recent_trades_memory without a DB round-trip.
        tid = synthetic_id()
        if doc.trade_status == "open":
            register_open_trade(
                position_ticket or tid,
                {"asset": asset, "side": side, "entry_price": entry_price, "trade_id": tid,
                 "cycle_id": cycle_id, "trade_reason": trade_reason, "trigger_reason": trigger_reason,
                 "ai_cost": ai_cost, "broker_cost": broker_cost},
            )
        return tid

    # Insert into database
    client = get_client()
    collection = client[MONGO_DB_NAME][_COLLECTION_COMPREHENSIVE_TRADES]
    result = await collection.insert_one(doc_dump)

    logger.info(
        f"Stored comprehensive trade for {asset} {symbol}",
        extra={
            "asset": asset,
            "symbol": symbol,
            "agent_id": agent_id,
            "quantity": quantity,
            "price": price,
            "entry_price": entry_price,
            "trade_type": trade_type,
            "trade_status": trade_status,
        }
    )

    return str(result.inserted_id)


async def store_hourly_candle(
    asset: str,
    symbol: str,
    slot_id: int,
    slot_timestamp: datetime,
    open: float,
    high: float,
    low: float,
    close: float,
    candle_timestamp: datetime,
    volume: Optional[float] = None,
    market_session: Optional[str] = None,
    gpt_signals_count: int = 0,
    triggers_met_count: int = 0,
    trades_executed_count: int = 0,
) -> str:
    """Store an hourly candle with comprehensive metadata."""

    timestamp = datetime.now(timezone.utc)
    client_order_id = f"hourly_candle_{asset}_{slot_id}_{timestamp.isoformat()}"
    
    # Create the document data with full precision
    doc_data = {
        "version": 1,
        "timestamp": timestamp,
        "client_order_id": client_order_id,
        "checksum": "",  # Will be set after creating the doc
        "asset": asset,
        "symbol": symbol,
        "slot_id": slot_id,
        "slot_timestamp": slot_timestamp,
        "open": float(open),
        "high": float(high),
        "low": float(low),
        "close": float(close),
        "volume": float(volume) if volume is not None else None,
        "candle_timestamp": candle_timestamp,
        "candle_interval": "1h",
        "market_session": market_session,
        "gpt_signals_count": gpt_signals_count,
        "triggers_met_count": triggers_met_count,
        "trades_executed_count": trades_executed_count,
    }
    
    # Generate checksum
    doc_data["checksum"] = _generate_checksum(doc_data)
    
    # Validate with Pydantic model
    doc = HourlyCandleDoc(**doc_data)
    doc_dump = doc.model_dump()

    log_event("hourly_candle", doc_dump, asset=asset, symbol=symbol, slot_id=slot_id)
    if not analytics_persistence_enabled():
        return synthetic_id()

    # Insert into database
    client = get_client()
    collection = client[MONGO_DB_NAME][_COLLECTION_HOURLY_CANDLES]
    result = await collection.insert_one(doc_dump)

    logger.info(
        f"Stored hourly candle for {asset} {symbol} slot {slot_id}",
        extra={
            "asset": asset,
            "symbol": symbol,
            "slot_id": slot_id,
            "open": open,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )
    
    return str(result.inserted_id)


async def get_slot_candle_matches(
    asset: str,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Get slot-candle matches for an asset within a time range."""
    
    client = get_client()
    collection = client[MONGO_DB_NAME][_COLLECTION_SLOT_CANDLE_MATCH]
    
    filt = {"asset": asset}
    if start_time or end_time:
        time_cond = {}
        if start_time:
            time_cond["$gte"] = start_time
        if end_time:
            time_cond["$lt"] = end_time
        filt["slot_timestamp"] = time_cond
    
    cursor = collection.find(filt, {"_id": 0}).sort("slot_timestamp", -1)
    if limit:
        cursor = cursor.limit(limit)
    
    return await cursor.to_list(length=None)


async def get_comprehensive_trades(
    asset: str,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Get comprehensive trades for an asset within a time range."""
    
    client = get_client()
    collection = client[MONGO_DB_NAME][_COLLECTION_COMPREHENSIVE_TRADES]
    
    filt = {"asset": asset}
    if start_time or end_time:
        time_cond = {}
        if start_time:
            time_cond["$gte"] = start_time
        if end_time:
            time_cond["$lt"] = end_time
        filt["trade_executed_at"] = time_cond
    
    cursor = collection.find(filt).sort("trade_executed_at", -1)
    if limit:
        cursor = cursor.limit(limit)
    
    return await cursor.to_list(length=None)


async def get_all_slot_candle_matches(
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Get all slot-candle matches within a time range."""
    
    client = get_client()
    collection = client[MONGO_DB_NAME][_COLLECTION_SLOT_CANDLE_MATCH]
    
    filt = {}
    if start_time or end_time:
        time_cond = {}
        if start_time:
            time_cond["$gte"] = start_time
        if end_time:
            time_cond["$lt"] = end_time
        filt["slot_timestamp"] = time_cond
    
    cursor = collection.find(filt, {"_id": 0}).sort("slot_timestamp", -1)
    if limit:
        cursor = cursor.limit(limit)
    
    return await cursor.to_list(length=None)


async def get_all_comprehensive_trades(
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Get all comprehensive trades within a time range."""
    
    client = get_client()
    collection = client[MONGO_DB_NAME][_COLLECTION_COMPREHENSIVE_TRADES]
    
    filt = {}
    if start_time or end_time:
        time_cond = {}
        if start_time:
            time_cond["$gte"] = start_time
        if end_time:
            time_cond["$lt"] = end_time
        filt["trade_executed_at"] = time_cond
    
    cursor = collection.find(filt).sort("trade_executed_at", -1)
    if limit:
        cursor = cursor.limit(limit)
    
    return await cursor.to_list(length=None)


_PARTIAL_REASONS = ("TP1_PARTIAL", "TP2_PARTIAL")
_LINK_FIELDS = {"position_ticket": 1, "cycle_id": 1, "parent_trade_id": 1, "pnl": 1}


def partial_trade(row):
    return bool(row.get("parent_trade_id")) or row.get("exit_reason") in _PARTIAL_REASONS


def _whole_position(row):
    cost = row.get("broker_cost") or {}
    return (row.get("trade_status") == "closed" and cost.get("source") == "broker_deals"
            and cost.get("status") == "final" and not partial_trade(row))


def covered_partials(parents, children):
    """Map each included partial row to its reconciled whole-position parent."""
    by_id = {str(p["_id"]): p for p in parents if _whole_position(p)}
    by_position = {(p["cycle_id"], p["position_ticket"]): p for p in by_id.values()
                   if p.get("cycle_id") and p.get("position_ticket")}
    covered = {}
    for child in children:
        parent_id = child.get("parent_trade_id")
        parent = (by_id.get(str(parent_id)) if parent_id else
                  by_position.get((child.get("cycle_id"), child.get("position_ticket"))))
        if parent is not None:
            covered[child["_id"]] = parent
    return covered


async def _included_partial_amounts(collection, parents):
    parents = [p for p in parents if _whole_position(p)]
    if not parents:
        return {}
    children = await collection.find({"trade_status": "closed", "$or": [
        {"parent_trade_id": {"$in": [str(p["_id"]) for p in parents]}},
        {"position_ticket": {"$in": [p["position_ticket"] for p in parents if p.get("position_ticket")]},
         "exit_reason": {"$in": list(_PARTIAL_REASONS)}},
    ]}, _LINK_FIELDS).to_list(length=None)
    covered = covered_partials(parents, children)
    amounts = {}
    for child in children:
        parent = covered.get(child["_id"])
        if parent is not None:
            key = parent["_id"]
            amounts[key] = amounts.get(key, 0.0) + float(child.get("pnl") or 0)
    return amounts


async def aggregate_realized_pnl(
    asset: Optional[str] = None,
    since: Optional[datetime] = None,
) -> dict[str, dict[str, Any]]:
    """Aggregate realized (closed-trade) P&L per asset from ``comprehensive_trades``.

    Returns ``{asset: {"realized_pnl", "wins", "losses", "trades"}}``.

    * A reconciled final contributes only its remainder after recorded partials.
      Legacy leg-only records still contribute their full amount.
    * ``wins``/``losses``/``trades`` count **distinct decisions only**
      (``parent_trade_id is None``) by P&L sign, so partial legs don't double-count.

    Read-only dashboard aggregation — never on the trading path. Degrades to an
    empty mapping when Mongo analytics persistence is disabled or unavailable.
    """
    if not analytics_persistence_enabled():
        return {}
    try:
        client = get_client()
        collection = client[MONGO_DB_NAME][_COLLECTION_COMPREHENSIVE_TRADES]

        match: dict[str, Any] = {"trade_status": "closed"}
        if asset:
            match["asset"] = asset
        if since is not None:
            match["trade_closed_at"] = {"$gte": since}

        # A parent (distinct decision) has no parent_trade_id.
        is_parent = {"$and": [
            {"$in": [{"$ifNull": ["$parent_trade_id", None]}, [None, ""]]},
            {"$eq": [{"$in": [{"$ifNull": ["$exit_reason", ""]}, list(_PARTIAL_REASONS)]}, False]},
        ]}
        pipeline = [
            {"$match": match},
            {
                "$group": {
                    "_id": "$asset",
                    "realized_pnl": {"$sum": {"$ifNull": ["$pnl", 0]}},
                    "wins": {
                        "$sum": {"$cond": [{"$and": [is_parent, {"$gt": ["$pnl", 0]}]}, 1, 0]}
                    },
                    "losses": {
                        "$sum": {"$cond": [{"$and": [is_parent, {"$lt": ["$pnl", 0]}]}, 1, 0]}
                    },
                    "trades": {"$sum": {"$cond": [is_parent, 1, 0]}},
                }
            },
        ]

        out: dict[str, dict[str, Any]] = {}
        async for doc in collection.aggregate(pipeline):
            key = doc.get("_id") or "UNKNOWN"
            out[str(key)] = {
                "realized_pnl": float(doc.get("realized_pnl", 0.0) or 0.0),
                "wins": int(doc.get("wins", 0) or 0),
                "losses": int(doc.get("losses", 0) or 0),
                "trades": int(doc.get("trades", 0) or 0),
            }
        parents = await collection.find(
            {**match, "broker_cost.source": "broker_deals", "broker_cost.status": "final"},
            {**_LINK_FIELDS, "asset": 1, "broker_cost.status": 1, "broker_cost.source": 1,
             "trade_status": 1, "exit_reason": 1},
        ).to_list(length=None)
        amounts = await _included_partial_amounts(collection, parents)
        for parent in parents:
            key = parent.get("asset") or "UNKNOWN"
            if parent["_id"] in amounts and key in out:
                out[key]["realized_pnl"] -= amounts[parent["_id"]]
        return out
    except Exception as e:  # pragma: no cover - dashboard read must never crash
        logger.warning(f"aggregate_realized_pnl failed: {e}")
        return {}


async def get_daily_pnl_curve(since: datetime) -> list[dict[str, Any]]:
    """Return cumulative realized-P&L points ``[{"time", "pnl"}]`` since *since*.

    Built from closed ``comprehensive_trades`` (all legs) ordered by close time,
    accumulating money P&L. Empty when Mongo is off. Dashboard read only.
    """
    if not analytics_persistence_enabled():
        return []
    try:
        client = get_client()
        collection = client[MONGO_DB_NAME][_COLLECTION_COMPREHENSIVE_TRADES]
        cursor = (
            collection.find(
                {"trade_status": "closed", "trade_closed_at": {"$gte": since}},
                {**_LINK_FIELDS, "trade_closed_at": 1, "trade_status": 1, "exit_reason": 1,
                 "broker_cost.source": 1, "broker_cost.status": 1},
            )
            .sort("trade_closed_at", 1)
        )
        docs = await cursor.to_list(length=None)
        amounts = await _included_partial_amounts(collection, docs)
        points: list[dict[str, Any]] = []
        cumulative = 0.0
        for d in docs:
            closed_at = _as_utc(d.get("trade_closed_at"))
            if closed_at is None:
                continue
            cumulative += float(d.get("pnl") or 0.0) - amounts.get(d.get("_id"), 0.0)
            points.append({"time": closed_at, "pnl": cumulative})
        return points
    except Exception as e:  # pragma: no cover - dashboard read must never crash
        logger.warning(f"get_daily_pnl_curve failed: {e}")
        return []


async def get_hourly_candles(
    asset: str,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Get hourly candles for an asset within a time range."""
    
    client = get_client()
    collection = client[MONGO_DB_NAME][_COLLECTION_HOURLY_CANDLES]
    
    filt = {"asset": asset}
    if start_time or end_time:
        time_cond = {}
        if start_time:
            time_cond["$gte"] = start_time
        if end_time:
            time_cond["$lt"] = end_time
        filt["slot_timestamp"] = time_cond
    
    cursor = collection.find(filt, {"_id": 0}).sort("slot_timestamp", -1)
    if limit:
        cursor = cursor.limit(limit)
    
    return await cursor.to_list(length=None)


async def get_all_hourly_candles(
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Get all hourly candles within a time range."""
    
    client = get_client()
    collection = client[MONGO_DB_NAME][_COLLECTION_HOURLY_CANDLES]
    
    filt = {}
    if start_time or end_time:
        time_cond = {}
        if start_time:
            time_cond["$gte"] = start_time
        if end_time:
            time_cond["$lt"] = end_time
        filt["slot_timestamp"] = time_cond
    
    cursor = collection.find(filt, {"_id": 0}).sort("slot_timestamp", -1)
    if limit:
        cursor = cursor.limit(limit)
    
    return await cursor.to_list(length=None)


async def update_slot_candle_match_trigger(
    match_id: str,
    trigger_met: bool,
    trigger_timestamp: Optional[datetime] = None,
    trigger_candle_timestamp: Optional[datetime] = None,
    trigger_candle_open: Optional[float] = None,
    trigger_candle_high: Optional[float] = None,
    trigger_candle_low: Optional[float] = None,
    trigger_candle_close: Optional[float] = None,
    trigger_candle_volume: Optional[float] = None,
    entry_price: Optional[float] = None,
    trade_executed: bool = False,
    trade_id: Optional[str] = None,
) -> bool:
    """Update a slot-candle match with trigger information."""
    
    client = get_client()
    collection = client[MONGO_DB_NAME][_COLLECTION_SLOT_CANDLE_MATCH]
    
    update_data = {
        "trigger_met": trigger_met,
        "trade_executed": trade_executed,
    }
    
    if trigger_timestamp:
        update_data["trigger_timestamp"] = trigger_timestamp
    if trigger_candle_timestamp:
        update_data["trigger_candle_timestamp"] = trigger_candle_timestamp
    if trigger_candle_open is not None:
        update_data["trigger_candle_open"] = float(trigger_candle_open)
    if trigger_candle_high is not None:
        update_data["trigger_candle_high"] = float(trigger_candle_high)
    if trigger_candle_low is not None:
        update_data["trigger_candle_low"] = float(trigger_candle_low)
    if trigger_candle_close is not None:
        update_data["trigger_candle_close"] = float(trigger_candle_close)
    if trigger_candle_volume is not None:
        update_data["trigger_candle_volume"] = float(trigger_candle_volume)
    if entry_price is not None:
        update_data["entry_price"] = float(entry_price)
    if trade_id:
        update_data["trade_id"] = trade_id
    
    result = await collection.update_one(
        {"_id": match_id},
        {"$set": update_data}
    )
    
    return result.modified_count > 0


async def delete_comprehensive_trade(trade_id: str) -> bool:
    """Delete a comprehensive trade by ID."""
    from bson import ObjectId
    client = get_client()
    collection = client[MONGO_DB_NAME][_COLLECTION_COMPREHENSIVE_TRADES]
    result = await collection.delete_one({"_id": ObjectId(trade_id)})
    return result.deleted_count > 0


# Maximum plausible |exit−entry| / entry. Exit prices further from the entry
# than this are treated as data corruption (e.g. a gold price written into a
# GBP doc — two such rows existed before ticket-matched exits) and rejected.
_EXIT_SANITY_MAX_REL_DIST = 0.2

# Delay before the single exit-match retry, covering the window where a fast
# exit beats the fire-and-forget open-trade write at entry.
_EXIT_MATCH_RETRY_SECONDS = 0.75


def _exit_price_sane(doc: dict, exit_price: float) -> bool:
    """True when *exit_price* is plausibly an exit for *doc*'s entry price."""
    try:
        entry = float(doc.get("entry_price") or 0.0)
        if entry <= 0:
            return True  # nothing to compare against — accept
        return abs(float(exit_price) - entry) / entry <= _EXIT_SANITY_MAX_REL_DIST
    except (TypeError, ValueError):
        return True


async def update_comprehensive_trade_exit(
    symbol: str,
    exit_price: float,
    exit_reason: str,
    pnl: float,
    trade_closed_at: Optional[datetime] = None,
    trade_id: Optional[str] = None,
    position_ticket: Optional[str] = None,
    broker_cost: Optional[dict] = None,
) -> bool:
    """Update an open trade with exit information (close it).

    Match priority: ``position_ticket`` (exact broker position) →
    ``trade_id`` (Mongo ``_id``) → most recent open trade for *symbol*.

    Two guards protect the symbol fallback, which is the only fuzzy match:

    * **Ticket-pinning guard (2026-07-28)** — the fallback is REFUSED when the
      candidate doc carries a ``position_ticket`` that is not the one being
      closed. A doc that records a ticket could only have been matched by that
      ticket; reaching it through the symbol filter therefore means it is a
      *different* position, and closing it destroys a real trade record.
      2026-07-27 is the exhibit: a duplicate USTEC exit closed a 17-day-old
      2026-07-10 doc (entry 29845.65, ticket 2194353981) with that day's
      +3.12 / 28534.98, permanently overwriting the real outcome.
    * A sanity guard refuses to write an exit price more than 20% away from the
      matched doc's entry price (catches cross-symbol contamination) — 4.4% in
      the 07-27 case, so it did not fire.

    In both refusals the doc is left open and ``False`` is returned so the
    caller's logs surface it. No ``trade_exit`` event is emitted either, which
    keeps the durable event stream free of the phantom close.
    """
    from bson import ObjectId

    if trade_closed_at is None:
        trade_closed_at = datetime.now(timezone.utc)

    if not analytics_persistence_enabled():
        # Log-only: there is no Mongo doc to match. Recover the entry context
        # from the in-process registry (populated at store_comprehensive_trade),
        # feed recent_trades_memory directly, and emit the durable exit event.
        info = pop_open_trade(position_ticket, trade_id) or {}
        log_event(
            "trade_exit",
            {
                "symbol": symbol,
                "exit_price": float(exit_price),
                "exit_reason": exit_reason,
                "pnl": float(pnl),
                "trade_closed_at": trade_closed_at,
                "position_ticket": position_ticket,
                "trade_id": trade_id,
                "cycle_id": info.get("cycle_id"),
                "trade_reason": info.get("trade_reason"),
                "trigger_reason": info.get("trigger_reason"),
                "ai_cost": info.get("ai_cost"),
                "broker_cost": broker_cost,
                "asset": info.get("asset"),
                "side": info.get("side"),
                "entry_price": info.get("entry_price"),
            },
            asset=info.get("asset") or symbol, symbol=symbol,
            position_ticket=position_ticket, trade_id=trade_id,
        )
        try:
            from packages.core.services.recent_trades_memory import record_closed_trade
            record_closed_trade(
                info.get("asset") or symbol,
                side=info.get("side"),
                entry=info.get("entry_price"),
                exit=exit_price,
                outcome=exit_reason,
                pnl=pnl,
                closed_at=trade_closed_at,
            )
        except Exception:
            pass
        return True

    client = get_client()
    collection = client[MONGO_DB_NAME][_COLLECTION_COMPREHENSIVE_TRADES]

    update_data = {
        "$set": {
            "trade_status": "closed",
            "exit_price": float(exit_price),
            "exit_reason": exit_reason,
            "pnl": float(pnl),
            "trade_closed_at": trade_closed_at,
        }
    }

    if broker_cost is not None:
        update_data["$set"]["broker_cost"] = broker_cost

    # Build candidate filters in priority order.
    filters: list[dict] = []
    if position_ticket:
        filters.append({"position_ticket": str(position_ticket), "trade_status": "open"})
    if trade_id:
        try:
            filters.append({"_id": ObjectId(trade_id)})
        except Exception:
            pass
    filters.append({"symbol": symbol, "trade_status": "open"})

    # Symbol-fallback candidates refused for ticket pinning, so the final
    # log line can say *why* nothing was closed instead of "no open trade".
    ticket_conflicts: list[dict] = []
    want_ticket = str(position_ticket or "").strip()

    async def _try_match() -> Optional[bool]:
        """One matching pass: True=closed, False=refused (sanity), None=no doc."""
        for filter_query in filters:
            cursor = collection.find(filter_query).sort("trade_executed_at", -1).limit(1)
            docs = await cursor.to_list(length=1)
            if not docs:
                continue
            doc = docs[0]
            is_symbol_fallback = (
                "position_ticket" not in filter_query and "_id" not in filter_query
            )
            if is_symbol_fallback:
                doc_ticket = str(doc.get("position_ticket") or "").strip()
                if doc_ticket and doc_ticket != want_ticket:
                    # This doc is pinned to a different broker position. Record
                    # and fall through (never close it). Deliberately NOT an
                    # early `return False`: when a ticket was supplied the
                    # caller still deserves the fast-exit retry below, which a
                    # hard return would skip.
                    ticket_conflicts.append({
                        "doc_id": str(doc.get("_id")),
                        "doc_ticket": doc_ticket,
                        "doc_entry_price": doc.get("entry_price"),
                        "doc_opened_at": str(doc.get("trade_executed_at")),
                    })
                    continue
            if not _exit_price_sane(doc, exit_price):
                logger.error(
                    f"REFUSING exit update for {symbol}: exit_price {exit_price} is "
                    f">{_EXIT_SANITY_MAX_REL_DIST:.0%} away from doc entry "
                    f"{doc.get('entry_price')} (doc _id={doc.get('_id')}) — "
                    "probable cross-trade contamination",
                    extra={
                        "symbol": symbol,
                        "exit_price": exit_price,
                        "doc_entry_price": doc.get("entry_price"),
                        "exit_reason": exit_reason,
                        "matched_by": str(filter_query),
                    },
                )
                return False
            await collection.update_one({"_id": doc["_id"]}, update_data)
            # Durable exit event (parity with log-only mode; lets the offline
            # reconstructor work even while Mongo persistence is still enabled).
            log_event(
                "trade_exit",
                {
                    "symbol": symbol,
                    "exit_price": float(exit_price),
                    "exit_reason": exit_reason,
                    "pnl": float(pnl),
                    "trade_closed_at": trade_closed_at,
                    "position_ticket": position_ticket,
                    "trade_id": str(doc.get("_id")),
                    "cycle_id": doc.get("cycle_id"),
                    "trade_reason": doc.get("trade_reason"),
                    "trigger_reason": doc.get("trigger_reason"),
                    "ai_cost": doc.get("ai_cost"),
                    "broker_cost": broker_cost,
                    "asset": doc.get("asset"),
                    "side": doc.get("side") or doc.get("direction"),
                    "entry_price": doc.get("entry_price"),
                },
                asset=doc.get("asset") or symbol, symbol=symbol,
                position_ticket=position_ticket, trade_id=str(doc.get("_id")),
            )
            # Push this close into the process-global recent-trades ring buffer.
            # This is the single path every full close funnels through, so it is
            # the one hook that keeps the in-memory prompt context complete.
            # Inert: a memory hiccup must never break the DB close.
            try:
                from packages.core.services.recent_trades_memory import record_closed_trade
                record_closed_trade(
                    doc.get("asset") or symbol,
                    side=doc.get("side") or doc.get("direction"),
                    entry=doc.get("entry_price"),
                    exit=exit_price,
                    outcome=exit_reason,
                    pnl=pnl,
                    closed_at=trade_closed_at,
                )
            except Exception:
                pass
            matched_by = (
                "position_ticket" if "position_ticket" in filter_query
                else "_id" if "_id" in filter_query else "symbol"
            )
            extra = {
                "symbol": symbol,
                "exit_price": exit_price,
                "exit_reason": exit_reason,
                "pnl": pnl,
                "matched_by": matched_by,
                "doc_id": str(doc.get("_id")),
            }
            if matched_by == "symbol":
                # Last-resort fuzzy match: "most recent open doc for this symbol".
                # Precise ticket/_id linkage was unavailable, so this CAN close the
                # wrong doc when more than one position for the symbol is open and
                # leave the real one open forever (poisoning recent-trades context).
                # Surfaced as a WARNING so the fallback is never silent.
                logger.warning(
                    f"Closed comprehensive trade for {symbol} by SYMBOL fallback "
                    f"(no position_ticket/_id match) — verify linkage",
                    extra=extra,
                )
            else:
                logger.info(f"Updated comprehensive trade exit for {symbol}", extra=extra)
            return True
        return None

    # The open-trade doc is persisted fire-and-forget at entry, so a very fast
    # exit can arrive before that write lands. When a position_ticket was given
    # (the exact-linkage case) and nothing matched, retry once after a short
    # delay rather than leaving a phantom permanently-open doc.
    for attempt in range(2):
        result = await _try_match()
        if result is not None:
            return result
        if position_ticket and attempt == 0:
            await asyncio.sleep(_EXIT_MATCH_RETRY_SECONDS)
            continue
        break

    if ticket_conflicts:
        c = ticket_conflicts[-1]
        logger.error(
            f"REFUSING SYMBOL-fallback exit for {symbol}: the most recent open doc "
            f"(_id={c['doc_id']}, opened {c['doc_opened_at']}) is pinned to "
            f"position_ticket={c['doc_ticket']} but this close is for "
            f"ticket={want_ticket or 'none'} — left open, no exit written "
            "(a duplicate/spurious exit would otherwise overwrite a real trade)",
            extra={
                "symbol": symbol,
                "exit_price": exit_price,
                "exit_reason": exit_reason,
                "pnl": pnl,
                "position_ticket": want_ticket or None,
                **c,
            },
        )
        return False

    logger.warning(f"No open trade found to update for {symbol}")
    return False


async def append_sl_trail(
    symbol: str,
    new_sl: float,
    label: Optional[str] = None,
    position_ticket: Optional[str] = None,
    trade_id: Optional[str] = None,
    at: Optional[datetime] = None,
) -> bool:
    """Record a stop-loss move on the open trade's ``sl_trail`` history.

    ``stop_loss`` on the doc is deliberately left untouched as the *initial*
    stop; the progression of trailed levels (trail steps + the breakeven move)
    is appended to an ``sl_trail`` array so the Trading Analysis table can show
    SL → 1 → 2 → 3. Match priority mirrors :func:`update_comprehensive_trade_exit`
    (``position_ticket`` → ``trade_id`` → most-recent open doc for *symbol*).

    Best-effort and failure-isolated: this runs inside the live trade-monitor
    loop, so it never raises and never blocks the trading path.
    """
    from bson import ObjectId

    at = at or datetime.now(timezone.utc)
    entry = {"sl": float(new_sl), "label": label, "at": at}

    # Durable structured event — system-of-record in log-only mode and a
    # harmless duplicate of the Mongo update when persistence is enabled.
    try:
        log_event(
            "trade_sl_trail",
            {
                "symbol": symbol,
                "sl": float(new_sl),
                "label": label,
                "at": at,
                "position_ticket": position_ticket,
                "trade_id": trade_id,
            },
            asset=symbol, symbol=symbol,
            position_ticket=position_ticket, trade_id=trade_id,
        )
    except Exception:
        pass

    if not analytics_persistence_enabled():
        return True

    try:
        client = get_client()
        collection = client[MONGO_DB_NAME][_COLLECTION_COMPREHENSIVE_TRADES]

        filters: list[dict] = []
        if position_ticket:
            filters.append({"position_ticket": str(position_ticket), "trade_status": "open"})
        if trade_id:
            try:
                filters.append({"_id": ObjectId(trade_id)})
            except Exception:
                pass
        filters.append({"symbol": symbol, "trade_status": "open"})

        for filter_query in filters:
            cursor = collection.find(filter_query).sort("trade_executed_at", -1).limit(1)
            docs = await cursor.to_list(length=1)
            if not docs:
                continue
            await collection.update_one(
                {"_id": docs[0]["_id"]},
                {"$push": {"sl_trail": entry}},
            )
            return True
    except Exception as e:  # pragma: no cover - never break the trading loop
        logger.warning(f"append_sl_trail failed for {symbol}: {e}")
    return False


def _as_utc(t: datetime | None) -> datetime | None:
    """Re-attach UTC to a Mongo-returned datetime.

    The Motor client is not ``tz_aware``, so BSON datetimes decode as naive
    (UTC-valued) objects. Subtracting one from a tz-aware ``now`` raises
    ``TypeError`` — the exact failure that silently zeroed recent-trades
    context in production. Normalize here before any arithmetic.
    """
    if t is not None and t.tzinfo is None:
        return t.replace(tzinfo=timezone.utc)
    return t


async def get_recent_closed_trades(
    asset: str,
    lookback_hours: int = 24,
    limit: int = 6,
    max_bars: int | None = None,
) -> list["RecentTradeView"]:
    """Compact recent-outcome list for prompt context (anti-churn memory).

    Returns the most recently *closed* trades for *asset*, newest-close first,
    each as a small typed dict safe to JSON-embed in a prompt. One indexed
    query; callers must treat failures as "no context" (inert).

    Recency is anchored on ``trade_closed_at`` (not execution time) so a trade
    that closed recently after a long hold still counts, and a long-ago close is
    excluded. ``lookback_hours`` is the coarse DB-side filter; ``max_bars`` is
    the precise request-time freshness cap (``bars_elapsed`` measured against the
    live ``now``) that keeps pre-outage trades out of the prompt after a restart.
    """
    client = get_client()
    collection = client[MONGO_DB_NAME][_COLLECTION_COMPREHENSIVE_TRADES]
    since = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    # Over-fetch a little: rows beyond max_bars are dropped below, so fetching
    # exactly `limit` could under-fill after the freshness cap.
    fetch = int(limit) * 2 if max_bars else int(limit)
    cursor = (
        collection.find(
            {
                "asset": asset,
                "trade_status": "closed",
                "trade_closed_at": {"$gte": since},
                # Exclude partial-exit child rows: TP1/TP2 partials are stored as
                # their own `closed` docs. Without this the AI's recent-outcome
                # context double-counts one trade as several outcomes, corrupting
                # the anti-churn / re-entry reasoning it feeds.
                "exit_reason": {"$nin": ["TP1_PARTIAL", "TP2_PARTIAL"]},
            },
            projection={
                "side": 1, "entry_price": 1, "exit_price": 1, "exit_reason": 1,
                "pnl": 1, "trade_executed_at": 1, "trade_closed_at": 1,
            },
        )
        .sort("trade_closed_at", -1)
        .limit(fetch)
    )
    docs = await cursor.to_list(length=fetch)
    now = datetime.now(timezone.utc)
    out: list["RecentTradeView"] = []
    for d in docs:
        t = _as_utc(d.get("trade_closed_at") or d.get("trade_executed_at"))
        age_min = int((now - t).total_seconds() / 60) if t else None
        bars_elapsed = (age_min // 5) if age_min is not None else None
        # Request-time freshness cap: a trade older than max_bars (or with no
        # usable close time) is not "recent" relative to THIS request — skip it.
        if max_bars:
            if bars_elapsed is None or bars_elapsed > max_bars:
                continue
        side = (d.get("side") or "").lower()
        entry_p = d.get("entry_price")
        exit_p = d.get("exit_price")
        move_pct: float | None = None
        if isinstance(entry_p, (int, float)) and isinstance(exit_p, (int, float)) and entry_p > 0:
            move_pct = round(abs(exit_p - entry_p) / entry_p * 100, 3)
        pnl_v = d.get("pnl")
        # TRUE outcome from PnL. The system trails the SL, so a winner usually
        # exits when its trailed (in-profit) stop is hit and the broker labels
        # that exit "SL" too — `outcome` alone makes a win look like a stop-out.
        if isinstance(pnl_v, (int, float)):
            result = "win" if pnl_v > 0 else "loss" if pnl_v < 0 else "scratch"
        else:
            result = None
        out.append({
            "ago_min": age_min,
            "bars_elapsed": bars_elapsed,
            "side": {"long": "buy", "short": "sell"}.get(side, side),
            "entry": entry_p,
            "exit": exit_p,
            "move_pct": move_pct,
            "outcome": d.get("exit_reason"),
            "result": result,
            "pnl": round(pnl_v, 2) if isinstance(pnl_v, (int, float)) else None,
        })
        if len(out) >= int(limit):
            break
    return out


_COLLECTION_STRATEGY_EXPIRIES = "strategy_expiries"


async def store_strategy_expiry(doc: dict) -> str:
    """Persist a code-sniper strategy that expired without triggering.

    These no-trigger outcomes were previously log-only, which made it
    impossible to measure selectivity or feed 'entry never reached' context
    back into prompts. Stored as-is (small dict built by the runner).
    """
    doc = dict(doc)
    doc.setdefault("stored_at", datetime.now(timezone.utc))
    log_event("strategy_expiry", doc, asset=doc.get("asset"))
    if not analytics_persistence_enabled():
        return synthetic_id()
    client = get_client()
    collection = client[MONGO_DB_NAME][_COLLECTION_STRATEGY_EXPIRIES]
    result = await collection.insert_one(doc)
    return str(result.inserted_id)


async def delete_slot_candle_match(match_id: str) -> bool:
    """Delete a slot-candle match by ID."""
    from bson import ObjectId
    client = get_client()
    collection = client[MONGO_DB_NAME][_COLLECTION_SLOT_CANDLE_MATCH]
    result = await collection.delete_one({"_id": ObjectId(match_id)})
    return result.deleted_count > 0


__all__ = [
    "store_slot_candle_match",
    "store_comprehensive_trade",
    "store_hourly_candle",
    "get_slot_candle_matches",
    "get_comprehensive_trades",
    "get_hourly_candles",
    "get_all_slot_candle_matches",
    "get_all_comprehensive_trades",
    "get_all_hourly_candles",
    "update_slot_candle_match_trigger",
    "update_comprehensive_trade_exit",
    "get_recent_closed_trades",
    "store_strategy_expiry",
    "delete_comprehensive_trade",
    "delete_slot_candle_match",
]
