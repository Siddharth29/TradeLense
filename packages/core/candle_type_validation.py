"""Type validation utilities for candle data to ensure consistency with live trading format."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Any

import pandas as pd

from .logger import get_logger

logger = get_logger(__name__)


def validate_candle_dict_types(candle_dict: Dict[str, Any]) -> tuple[bool, str]:
    """Validate that a single candle dictionary has correct types.
    
    Parameters
    ----------
    candle_dict : Dict[str, Any]
        Candle dictionary to validate
        
    Returns
    -------
    tuple[bool, str]
        (is_valid, error_message) - True if valid, False with error message otherwise
    """
    required_fields = ["time", "open", "high", "low", "close", "volume"]
    
    # Check required fields exist
    missing_fields = [field for field in required_fields if field not in candle_dict]
    if missing_fields:
        return False, f"Missing required fields: {missing_fields}"
    
    # Validate time is datetime
    time_val = candle_dict.get("time")
    if not isinstance(time_val, datetime):
        return False, f"time field must be datetime, got {type(time_val).__name__}"
    
    # Validate timezone-aware
    if time_val.tzinfo is None:
        return False, "time field must be timezone-aware"
    
    # Validate numeric fields are float
    numeric_fields = ["open", "high", "low", "close", "volume"]
    for field in numeric_fields:
        value = candle_dict.get(field)
        if not isinstance(value, (int, float)):
            return False, f"{field} field must be numeric (int or float), got {type(value).__name__}"
    
    return True, ""


def normalize_candle_types(candle_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize candle dictionary to ensure correct types.
    
    Converts types to match live trading format:
    - time: datetime (timezone-aware UTC)
    - open, high, low, close, volume: float
    
    Parameters
    ----------
    candle_dict : Dict[str, Any]
        Candle dictionary to normalize
        
    Returns
    -------
    Dict[str, Any]
        Normalized candle dictionary with correct types
    """
    normalized = dict(candle_dict)
    
    # Normalize time to datetime
    time_val = normalized.get("time")
    if time_val is None:
        logger.error(f"Time field is None in candle: {normalized}")
        raise ValueError("time field cannot be None")
    
    if not isinstance(time_val, datetime):
        if hasattr(time_val, 'to_pydatetime'):
            time_val = time_val.to_pydatetime()
        else:
            time_val = pd.to_datetime(time_val).to_pydatetime()
            # Check for NaT (Not a Time) which pandas returns for None/invalid dates
            if pd.isna(time_val):
                logger.error(f"Invalid time field in candle: {normalized.get('time')}")
                raise ValueError(f"time field is invalid: {normalized.get('time')}")
    
    # Ensure timezone-aware UTC
    if time_val.tzinfo is None:
        time_val = time_val.replace(tzinfo=timezone.utc)
    else:
        # Convert to UTC if not already
        time_val = time_val.astimezone(timezone.utc)
    
    normalized["time"] = time_val
    
    # Normalize numeric fields to float
    numeric_fields = ["open", "high", "low", "close", "volume"]
    for field in numeric_fields:
        if field in normalized:
            value = normalized[field]
            if isinstance(value, str):
                try:
                    normalized[field] = float(value)
                except ValueError:
                    logger.warning(f"Could not convert {field} to float: {value}")
            elif isinstance(value, int):
                normalized[field] = float(value)
            elif not isinstance(value, float):
                logger.warning(f"Unexpected type for {field}: {type(value).__name__}, converting to float")
                try:
                    normalized[field] = float(value)
                except (ValueError, TypeError):
                    logger.error(f"Failed to convert {field} to float: {value}")
    
    return normalized


def validate_candle_list_types(candle_list: List[Dict[str, Any]]) -> tuple[bool, str]:
    """Validate that a list of candle dictionaries have correct types.
    
    Parameters
    ----------
    candle_list : List[Dict[str, Any]]
        List of candle dictionaries to validate
        
    Returns
    -------
    tuple[bool, str]
        (is_valid, error_message) - True if all valid, False with error message otherwise
    """
    if not candle_list:
        return True, "Empty list is valid"
    
    for i, candle in enumerate(candle_list):
        is_valid, error_msg = validate_candle_dict_types(candle)
        if not is_valid:
            return False, f"Candle at index {i}: {error_msg}"
    
    return True, ""


def normalize_candle_list_types(candle_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize a list of candle dictionaries to ensure correct types.
    
    Parameters
    ----------
    candle_list : List[Dict[str, Any]]
        List of candle dictionaries to normalize
        
    Returns
    -------
    List[Dict[str, Any]]
        List of normalized candle dictionaries
    """
    normalized = []
    for candle in candle_list:
        try:
            normalized_candle = normalize_candle_types(candle)
            normalized.append(normalized_candle)
        except Exception as e:
            logger.warning(f"Failed to normalize candle: {e}, skipping")
            # Still add original candle but log warning
            normalized.append(candle)
    
    return normalized

