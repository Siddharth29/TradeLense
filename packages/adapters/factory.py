"""Factory to create broker adapters lazily."""

from __future__ import annotations

from typing import Any

from .broker_base import IBroker


def create_broker(name: str, **kwargs: Any) -> IBroker:
    """Create broker by name.

    Parameters
    ----------
    name: str
        Broker identifier (mt5).
    kwargs: Any
        Adapter specific arguments.
    """
    key = name.lower()
    if key == "mt5":
        from .mt5_adapter import MT5Adapter

        return MT5Adapter(**kwargs)
    raise ValueError(f"Unknown broker: {name}")


__all__ = ["create_broker"]
