from __future__ import annotations

"""Trade lifecycle management with optimistic concurrency control.

This module provides a small state machine handling transitions between
``open`` → ``break-even`` → ``closed`` states.  Each trade carries a
``version`` field which is incremented on every successful persistence
operation.  Repositories are expected to enforce optimistic locking by
matching the expected version before applying updates.
"""

from dataclasses import dataclass, replace
from enum import Enum
from typing import Dict, Protocol


class OptimisticLockError(RuntimeError):
    """Raised when a repository update fails due to version mismatch."""


class TradeState(str, Enum):
    """Possible states of a trade during its lifecycle."""

    OPEN = "open"
    BREAK_EVEN = "break-even"
    CLOSED = "closed"


@dataclass(slots=True)
class Trade:
    """Represents the lifecycle state of a trade."""

    id: str
    state: TradeState = TradeState.OPEN
    version: int = 0


class TradeRepository(Protocol):
    """Repository interface used for persisting :class:`Trade` objects."""

    async def get(self, trade_id: str) -> Trade | None:  # pragma: no cover - interface
        ...

    async def save(self, trade: Trade, *, expected_version: int) -> None:  # pragma: no cover - interface
        ...


class InMemoryTradeRepository:
    """Simple in-memory repository used for tests."""

    def __init__(self) -> None:
        self._items: Dict[str, Trade] = {}

    async def get(self, trade_id: str) -> Trade | None:
        trade = self._items.get(trade_id)
        if trade is None:
            return None
        # return a copy to prevent accidental external mutation
        return replace(trade)

    async def save(self, trade: Trade, *, expected_version: int) -> None:
        current = self._items.get(trade.id)
        current_version = current.version if current else 0
        if current_version != expected_version:
            raise OptimisticLockError(
                f"expected version {expected_version}, found {current_version}"
            )
        trade.version = expected_version + 1
        self._items[trade.id] = replace(trade)


class TradeLifecycle:
    """High level API for managing trade state transitions."""

    def __init__(self, repository: TradeRepository) -> None:
        self._repo = repository

    async def open(self, trade_id: str) -> Trade:
        """Create a new trade in ``open`` state."""
        trade = Trade(id=trade_id)
        await self._repo.save(trade, expected_version=0)
        return trade

    async def move_to_break_even(self, trade_id: str) -> Trade:
        """Transition an ``open`` trade to ``break-even``."""
        trade = await self._require_trade(trade_id)
        if trade.state is not TradeState.OPEN:
            raise ValueError("trade not open")
        trade.state = TradeState.BREAK_EVEN
        await self._repo.save(trade, expected_version=trade.version)
        return trade

    async def close(self, trade_id: str) -> Trade:
        """Close an existing trade."""
        trade = await self._require_trade(trade_id)
        if trade.state is TradeState.CLOSED:
            raise ValueError("trade already closed")
        trade.state = TradeState.CLOSED
        await self._repo.save(trade, expected_version=trade.version)
        return trade

    async def _require_trade(self, trade_id: str) -> Trade:
        trade = await self._repo.get(trade_id)
        if trade is None:
            raise ValueError("trade not found")
        return trade


__all__ = [
    "TradeState",
    "Trade",
    "OptimisticLockError",
    "TradeRepository",
    "InMemoryTradeRepository",
    "TradeLifecycle",
]
