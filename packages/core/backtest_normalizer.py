"""Normalization utilities for backtest iteration data.

Handles field name variations between prediction, execution_result, and trade_outcome
to ensure consistent data structures.
"""

from __future__ import annotations

from typing import Any, Dict
from datetime import datetime, timezone

from .logger import get_logger

logger = get_logger(__name__)


def normalize_prediction_fields(pred: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize prediction field names to canonical format.
    
    Maps variants to canonical names:
    - entry_price/level -> entry (canonical)
    - stoploss/stop_loss -> stop (canonical)
    - take_profit_1 -> target (canonical)
    
    Parameters
    ----------
    pred : Dict[str, Any]
        Prediction dictionary with potentially variant field names
    
    Returns
    -------
    Dict[str, Any]
        Normalized prediction with canonical field names
    """
    if not pred or not isinstance(pred, dict):
        return pred or {}
    
    normalized = {**pred}
    
    # Normalize entry price: variants -> entry (canonical)
    # Use explicit None checks — 0.0 is a valid (if unlikely) financial value
    def _first_not_none(*values):
        for v in values:
            if v is not None:
                return v
        return None

    entry = _first_not_none(normalized.get("entry"), normalized.get("entry_price"), normalized.get("level"))
    if entry is not None:
        normalized["entry"] = float(entry)

    # Normalize stop loss: variants -> stop (canonical)
    stop = _first_not_none(normalized.get("stop"), normalized.get("stop_loss"), normalized.get("stoploss"))
    if stop is not None:
        normalized["stop"] = float(stop)

    # Normalize target: variants -> target (canonical)
    target = _first_not_none(normalized.get("target"), normalized.get("take_profit_1"))
    if target is not None:
        normalized["target"] = float(target)
    
    return normalized


def normalize_execution_result_fields(exec_result: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize execution_result field names to canonical format.
    
    Maps variants to canonical names:
    - stop_loss -> stop
    - take_profit_1 -> target
    
    Parameters
    ----------
    exec_result : Dict[str, Any]
        Execution result dictionary
    
    Returns
    -------
    Dict[str, Any]
        Normalized execution result with canonical field names
    """
    if not exec_result or not isinstance(exec_result, dict):
        return exec_result or {}
    
    normalized = {**exec_result}
    
    # Map stop_loss -> stop (canonical)
    if "stop_loss" in normalized and "stop" not in normalized:
        normalized["stop"] = normalized["stop_loss"]
    
    # Map take_profit_1 -> target (canonical)
    if "take_profit_1" in normalized and "target" not in normalized:
        normalized["target"] = normalized["take_profit_1"]
    
    # Ensure all numeric fields are floats
    for field in ["entry", "stop", "target"]:
        if field in normalized and normalized[field] is not None:
            try:
                normalized[field] = float(normalized[field])
            except (ValueError, TypeError):
                logger.warning(f"Failed to convert {field} to float: {normalized[field]}")
    
    return normalized


def normalize_trade_outcome_fields(outcome: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize trade_outcome field names to standard format.
    
    Parameters
    ----------
    outcome : Dict[str, Any]
        Trade outcome dictionary from simulate_trade
    
    Returns
    -------
    Dict[str, Any]
        Normalized trade outcome with standard field names
    """
    if not outcome or not isinstance(outcome, dict):
        return outcome or {}
    
    normalized = {**outcome}
    
    # Ensure all numeric fields are floats
    for field in ["entry", "stop", "initial_stop", "target", "exit_price", "profit", "risk_reward"]:
        if field in normalized and normalized[field] is not None:
            try:
                normalized[field] = float(normalized[field])
            except (ValueError, TypeError):
                logger.warning(f"Failed to convert {field} to float: {normalized[field]}")
    
    # Normalize timestamps to datetime objects if they're strings
    for field in ["entry_time", "exit_time", "signal_time"]:
        if field in normalized and normalized[field] is not None:
            ts = normalized[field]
            if isinstance(ts, str):
                try:
                    normalized[field] = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                except (ValueError, AttributeError):
                    logger.warning(f"Failed to parse {field} timestamp: {ts}")
            # Don't force UTC on naive datetimes — they may already be in
            # the correct timezone (e.g. IST for NIFTY data).  Only tag
            # genuinely unaware timestamps that came from the backtest
            # runner (which always operates in UTC).
            # Leaving naive datetimes untouched avoids silent timezone errors.
 
    # Ensure trade_closed_at mirrors exit_time for downstream storage
    if normalized.get("exit_time") is not None and "trade_closed_at" not in normalized:
        normalized["trade_closed_at"] = normalized["exit_time"]

    return normalized


def normalize_iteration_data(iteration_data: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize all fields in iteration data to standard format.
    
    Applies normalization to prediction, execution_result, and trade_outcome.
    
    Parameters
    ----------
    iteration_data : Dict[str, Any]
        Raw iteration data from backtest runner
    
    Returns
    -------
    Dict[str, Any]
        Normalized iteration data with standard field names
    """
    if not iteration_data or not isinstance(iteration_data, dict):
        return iteration_data or {}
    
    normalized = {**iteration_data}
    
    # Normalize prediction
    if "prediction" in normalized and normalized["prediction"]:
        normalized["prediction"] = normalize_prediction_fields(normalized["prediction"])
    
    # Normalize execution_result
    if "execution_result" in normalized and normalized["execution_result"]:
        normalized["execution_result"] = normalize_execution_result_fields(normalized["execution_result"])
    
    # Normalize trade_outcome
    if "trade_outcome" in normalized and normalized["trade_outcome"]:
        normalized["trade_outcome"] = normalize_trade_outcome_fields(normalized["trade_outcome"])
    
    # Ensure iteration_timestamp is datetime
    if "iteration_timestamp" in normalized and normalized["iteration_timestamp"]:
        ts = normalized["iteration_timestamp"]
        if isinstance(ts, str):
            try:
                normalized["iteration_timestamp"] = datetime.fromisoformat(ts.replace('Z', '+00:00'))
            except (ValueError, AttributeError):
                logger.warning(f"Failed to parse iteration_timestamp: {ts}")
    
    # Ensure timestamp is datetime (strict Pydantic model requires datetime, not string)
    if "timestamp" in normalized and normalized["timestamp"]:
        ts = normalized["timestamp"]
        if isinstance(ts, str):
            try:
                parsed = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                if parsed.tzinfo is None:
                    from datetime import timezone as _tz
                    parsed = parsed.replace(tzinfo=_tz.utc)
                normalized["timestamp"] = parsed
            except (ValueError, AttributeError):
                logger.warning(f"Failed to parse timestamp: {ts}")
    
    # Strip fields not in BacktestIterationDoc (extra="forbid" rejects them)
    _extra_fields = {"outcome", "monitoring_active_trade", "entry_iteration", "timeout_closed"}
    for field in _extra_fields:
        normalized.pop(field, None)
    
    return normalized


def extract_unified_fields(iteration: Dict[str, Any]) -> Dict[str, Any]:
    """Extract canonical entry/stop/target from iteration data.
    
    Uses canonical field names: entry, stop, target
    Checks execution_result, prediction, and trade_outcome in order of preference.
    
    Parameters
    ----------
    iteration : Dict[str, Any]
        Iteration data (should use canonical field names)
    
    Returns
    -------
    Dict[str, Any]
        Canonical fields: entry, stop, target, direction
    """
    exec_result = iteration.get("execution_result", {}) or {}
    prediction = iteration.get("prediction", {}) or {}
    outcome = iteration.get("trade_outcome", {}) or {}

    def _first_not_none(*values):
        for v in values:
            if v is not None:
                return v
        return None

    # Priority: execution_result -> prediction -> trade_outcome
    # Use canonical field names (fallback to variants for backward compatibility)
    entry = _first_not_none(
        exec_result.get("entry"), exec_result.get("entry_price"), exec_result.get("level"),
        prediction.get("entry"), prediction.get("entry_price"), prediction.get("level"),
        outcome.get("entry"),
    )

    stop = _first_not_none(
        exec_result.get("stop"), exec_result.get("stop_loss"),
        prediction.get("stop"), prediction.get("stop_loss"), prediction.get("stoploss"),
        outcome.get("stop"),
    )

    target = _first_not_none(
        exec_result.get("target"), exec_result.get("take_profit_1"),
        prediction.get("target"), prediction.get("take_profit_1"),
        outcome.get("target"),
    )

    direction = _first_not_none(
        exec_result.get("direction"),
        prediction.get("direction"),
        outcome.get("direction"),
    )

    return {
        "entry": entry,
        "stop": stop,
        "target": target,
        "direction": direction,
    }

