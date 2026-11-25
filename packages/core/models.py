"""Common dataclasses for trading models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional, TypedDict


class RecentTradeView(TypedDict):
    """One closed-trade outcome shaped for prompt context and the re-entry gate.

    Produced by ``get_recent_closed_trades`` (the single source) and consumed by
    the prompt builders (``_recent_trades_section``) and the adaptive re-entry
    gate (``_most_recent_closed_trade``). Every field may be ``None`` when the
    source document is incomplete, so consumers must guard before arithmetic —
    ``bars_elapsed`` in particular is ``None`` for a trade with no close time.
    """

    ago_min: Optional[int]       # minutes since close, measured at query time
    bars_elapsed: Optional[int]  # ago_min // 5 (5-min bars since close)
    side: str                    # normalized to "buy"/"sell"
    entry: Optional[float]
    exit: Optional[float]
    move_pct: Optional[float]    # |exit-entry|/entry * 100
    outcome: Optional[str]       # exit_reason: "TP2"/"SL"/"TIMEOUT"/...
    result: Optional[str]        # "win"/"loss"/"scratch" from PnL — the TRUE
                                 # outcome. With trailing SL most exits read
                                 # "SL" regardless of profit, so the AI must
                                 # judge by this, not by `outcome`.
    pnl: Optional[float]


class Side(str, Enum):
    """Order side."""

    BUY = "buy"
    SELL = "sell"


class PriceType(str, Enum):
    """Order price type."""

    MARKET = "market"
    LIMIT = "limit"


@dataclass(slots=True)
class Decision:
    """High level trading decision used to create an order."""

    asset: str
    symbol: str
    side: Side
    price_type: PriceType
    entry: float
    quantity: float
    sl: Optional[float] = None
    tp1: Optional[float] = None
    tp: Optional[float] = None
    valid_until: Optional[datetime] = None
    notes: Optional[str] = None
    client_order_id: Optional[str] = None


@dataclass(slots=True)
class Order:
    """Represents an order submitted to a broker."""

    id: str
    decision: Decision
    status: str
    filled_quantity: float = 0.0
    average_price: float = 0.0
    fees: float = 0.0
    client_order_id: Optional[str] = None


@dataclass(slots=True)
class Fill:
    """Execution information for a filled order."""

    order_id: str
    quantity: float
    price: float
    timestamp: datetime


@dataclass(slots=True)
class Position:
    """Current open position for a symbol."""

    symbol: str
    quantity: float
    average_price: float


__all__ = ["Side", "PriceType", "Decision", "Order", "Fill", "Position"]
