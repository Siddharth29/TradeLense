"""Pydantic schemas for core models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, ConfigDict, PositiveFloat, model_validator

from .models import Decision, Side, PriceType
from .prediction import Prediction


class DecisionSchemaV1(BaseModel):
    """Schema for Decision version 1."""

    version: Literal[1] = Field(1, description="Schema version")
    asset: str
    symbol: str
    side: Side
    price_type: PriceType
    entry: float
    quantity: float = Field(0)
    sl: float | None = None
    tp1: float | None = None
    tp: float | None = None
    valid_until: datetime | None = None
    notes: str | None = None
    client_order_id: str | None = None

    model_config = ConfigDict(extra="forbid", strict=True)

    def to_decision(self) -> Decision:
        """Convert schema instance to :class:`Decision`."""

        return Decision(
            asset=self.asset,
            symbol=self.symbol,
            side=self.side,
            price_type=self.price_type,
            entry=self.entry,
            quantity=self.quantity,
            sl=self.sl,
            tp1=self.tp1,
            tp=self.tp,
            valid_until=self.valid_until,
            notes=self.notes,
            client_order_id=self.client_order_id,
        )


class SniperResponse(BaseModel):
    """Schema returned by the sniper agent.
    
    Uses canonical field names aligned with BacktestPrediction:
    - entry: Entry price (canonical name, not level)
    - stop: Stop loss price
    - target: Target price
    """

    direction: Literal["long", "short"]
    pattern: Literal[
        "retest",
        "breakout",
        "bos",
        "BOS",
        "fvg fill",
        "FVG fill",
        "fvg",
        "orderblock",
        "liquidity sweep",
        "compression breakout",
        "range breakout retest",
        "fair value gap",
        "support",
        "resistance",
        "trendline",
        "channel",
        "pattern",
        "reversal",
        "continuation",
        "pullback",
    ]
    entry: float  # Entry price (canonical name)
    stop: float   # Stop loss price
    target: float # Target price
    confidence: Literal["high", "medium", "low"]
    status: Literal["waiting"] = "waiting"

    model_config = ConfigDict(extra="forbid", strict=True)

    @model_validator(mode="after")
    def _check_ordering(self) -> "SniperResponse":  # pragma: no cover - simple validation
        # Basic validation: only check ordering if all values are positive
        if self.entry > 0 and self.stop > 0 and self.target > 0:
            if self.direction == "long":
                if not (self.stop < self.entry < self.target):
                    raise ValueError(
                        f"Invalid price ordering for long: stop ({self.stop}) < entry ({self.entry}) < target ({self.target})"
                    )
            else:
                if not (self.target < self.entry < self.stop):
                    raise ValueError(
                        f"Invalid price ordering for short: target ({self.target}) < entry ({self.entry}) < stop ({self.stop})"
                    )
        return self


def coerce_prediction(resp: SniperResponse) -> Prediction:
    """Coerce :class:`SniperResponse` into :class:`Prediction`."""

    return Prediction(
        direction=resp.direction,
        level=resp.entry,  # Map entry to level for Prediction model
        stop=resp.stop,
        target=resp.target,
        condition=resp.pattern,
        buffer=0.0,
    )


__all__ = ["DecisionSchemaV1", "SniperResponse", "coerce_prediction"]
