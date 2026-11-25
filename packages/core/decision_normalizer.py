"""Normalization utilities for strategy decisions and predictions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from pydantic import ValidationError

from .prediction import Prediction
from .schemas import SniperResponse, coerce_prediction
from .schemas_trade import DynamicConfig
from .logger import get_logger

logger = get_logger(__name__)


def _normalize_field_names(response_data: dict) -> dict:
    """Map field name variants to canonical names.
    
    This is a one-time transformation from GPT response variants to canonical names.
    No validation or fixing of data - just field name mapping.
    """
    # Map common field name variations to canonical names
    field_mappings = {
        # Entry price variants -> entry
        "entry_price": "entry",
        "entryPrice": "entry",
        "price": "entry",
        "level": "entry",  # Legacy mapping
        # Stop loss variants -> stop
        "stoploss": "stop",
        "stop_loss": "stop",
        "stopLoss": "stop",
        "sl": "stop",
        # Target variants -> target
        "take_profit": "target",
        "take_profit_1": "target",
        "takeProfit": "target",
        "tp": "target",
        # Pattern variants -> pattern
        "condition": "pattern",
        "signal": "pattern",
        "trade_type": "pattern",
        # Direction variants -> direction
        "side": "direction",
        "action": "direction",
    }
    
    normalized = response_data.copy()
    
    # Apply field mappings (only if canonical name not already present)
    for old_name, canonical_name in field_mappings.items():
        if old_name in normalized and canonical_name not in normalized:
            normalized[canonical_name] = normalized.pop(old_name)
    
    return normalized


class DecisionAction(str, Enum):
    """Possible actions produced by a strategy."""

    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass(slots=True)
class Decision:
    """Normalized decision used by the backtest engine."""

    action: DecisionAction
    size: float
    client_order_id: str | None = None


def normalize_decision(obj: Mapping[str, Any]) -> Decision:
    """Return a :class:`Decision` from *obj* applying defaults and coercions."""

    raw_action = obj.get("action", DecisionAction.HOLD)
    action = DecisionAction(str(getattr(raw_action, "value", raw_action)).lower())
    # Preserve full precision - do not round
    size = float(obj.get("size", 0.0))
    client_order_id = obj.get("client_order_id", None)
    return Decision(action=action, size=size, client_order_id=client_order_id)


def parse_prediction(obj: Mapping[str, Any]) -> Prediction | None:
    """Return a :class:`Prediction` from *obj* with basic validation only.
    
    This function:
    1. Maps field name variants to canonical names (entry, stop, target, direction, pattern, confidence, status)
    2. Validates required fields exist and types are correct via Pydantic
    3. Fails fast on invalid data (no fallbacks or fixing)
    
    Args:
        obj: Raw prediction data from GPT response (may contain field name variants)
        
    Returns:
        Prediction object if valid, None if validation fails
        
    Raises:
        ValueError: If required fields are missing or invalid
    """
    try:
        response_data = obj.get("prediction", obj)
        if not isinstance(response_data, dict):
            logger.error(f"Invalid prediction data type: {type(response_data)}, expected dict")
            return None
        
        # Step 1: Normalize field names (map variants to canonical names)
        response_data = _normalize_field_names(response_data)
        
        # Step 2: Validate via Pydantic (will raise ValidationError if invalid)
        # Pydantic will check:
        # - Required fields exist
        # - Types are correct
        # - Literal values are valid
        # - Price ordering is correct (if all values > 0)
        model = SniperResponse.model_validate(response_data)
        
    except ValidationError as exc:
        logger.error(f"SniperResponse validation failed: {exc}")
        logger.error(f"Original data: {obj}")
        logger.error(f"Normalized data: {response_data if 'response_data' in locals() else 'N/A'}")
        return None
    except Exception as exc:
        logger.error(f"Unexpected error in parse_prediction: {exc}", exc_info=True)
        return None
    
    return coerce_prediction(model)


def validate_dynamic_config(config_data: dict) -> DynamicConfig | None:
    """Validate and normalize dynamic configuration data.
    
    Args:
        config_data: Raw dynamic configuration data from AI response
        
    Returns:
        DynamicConfig object if valid, None if invalid or missing
        
    Raises:
        ValueError: If configuration data is invalid
    """
    if not config_data:
        return None
    
    try:
        # First, clamp values to safe ranges before validation
        clamped_data = config_data.copy()
        
        # Clamp TTL to safe range
        if "ttl_minutes" in clamped_data:
            clamped_data["ttl_minutes"] = max(30, min(180, clamped_data["ttl_minutes"]))
        
        # Clamp 5m candle count to safe range
        if "candles_5m_count" in clamped_data:
            clamped_data["candles_5m_count"] = max(50, min(200, clamped_data["candles_5m_count"]))
        
        # Clamp 15m candle count to safe range
        if "candles_15m_count" in clamped_data:
            clamped_data["candles_15m_count"] = max(20, min(100, clamped_data["candles_15m_count"]))
        
        # Log clamping if values were changed
        if clamped_data != config_data:
            logger.warning(
                "Dynamic configuration values clamped to safe ranges",
                extra={
                    "original": config_data,
                    "clamped": clamped_data
                }
            )
        
        # Now validate the clamped configuration
        dynamic_config = DynamicConfig(**clamped_data)
        
        logger.info(
            "Dynamic configuration validated successfully",
            extra={
                "ttl_minutes": dynamic_config.ttl_minutes,
                "candles_5m_count": dynamic_config.candles_5m_count,
                "candles_15m_count": dynamic_config.candles_15m_count,
                "market_volatility": dynamic_config.market_volatility,
                "trend_strength": dynamic_config.trend_strength,
            }
        )
        
        return dynamic_config
        
    except Exception as e:
        logger.error(
            f"Failed to validate dynamic configuration: {e}",
            extra={"config_data": config_data}
        )
        raise ValueError(f"Invalid dynamic configuration: {e}")


__all__ = [
    "DecisionAction",
    "Decision",
    "Prediction",
    "normalize_decision",
    "parse_prediction",
    "validate_dynamic_config",
]
