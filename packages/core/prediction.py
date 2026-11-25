from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(slots=True)
class Prediction:
    """Normalized trade prediction produced by a strategy."""

    direction: Literal["long", "short"]
    level: float
    stop: float
    target: float
    condition: str
    buffer: float


__all__ = ["Prediction"]
