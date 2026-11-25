"""Protocol defining required broker operations."""

from __future__ import annotations

from typing import Any, Dict, List, Protocol

from packages.core.models import Decision, Order, Position


class IBroker(Protocol):
    """Interface describing broker capabilities."""

    async def connect(self, **kwargs: Any) -> None:
        """Establish a connection using adapter-specific parameters."""

    def place(self, decision: Decision) -> Order:
        """Place an order based on a trading decision."""

    def modify(self, order: Order, decision: Decision) -> Order:
        """Modify an existing order according to a new decision."""

    def cancel(self, order: Order) -> None:
        """Cancel an existing order."""

    def positions(self) -> List[Position]:
        """Return open positions."""

    def balances(self) -> Dict[str, float]:
        """Return account balances keyed by currency."""

    def symbol_info(self, symbol: str) -> Dict[str, str]:
        """Return metadata for a tradable symbol."""

    def modify_sl(self, symbol: str, new_sl: float) -> None:
        """Modify the stop-loss of an open position for *symbol*."""

    def move_sl_to_be(self, symbol: str) -> None:
        """Move stop-loss to break-even (entry price) for *symbol*."""

    def close(self, symbol: str) -> None:
        """Close an open position for *symbol* at market price."""


__all__ = ["IBroker"]
