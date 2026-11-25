"""
Utility functions for handling datetime serialization across the application.

This module provides consistent datetime handling to prevent JSON serialization errors
that can occur when datetime objects are not properly converted to strings.
"""

from __future__ import annotations

import json
from datetime import datetime, date
from typing import Any, Dict, List, Union
import pandas as pd


def serialize_datetime(value: Any) -> str:
    """
    Safely serialize datetime objects to ISO format strings.
    
    Args:
        value: Any value that might contain datetime objects
        
    Returns:
        str: ISO format string representation of the datetime
    """
    if value is None:
        return None
    elif isinstance(value, datetime):
        return value.isoformat()
    elif isinstance(value, date):
        return value.isoformat()
    elif isinstance(value, pd.Timestamp):
        # Handle pandas Timestamp specifically
        try:
            return value.isoformat()
        except Exception:
            try:
                return value.to_pydatetime().isoformat()
            except Exception:
                return str(value)
    elif hasattr(value, 'isoformat'):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    elif hasattr(value, 'to_pydatetime'):
        try:
            return value.to_pydatetime().isoformat()
        except Exception:
            return str(value)
    elif hasattr(value, 'timestamp'):
        try:
            return datetime.fromtimestamp(value.timestamp()).isoformat()
        except Exception:
            return str(value)
    elif hasattr(value, 'to_datetime'):
        # Handle other datetime-like objects
        try:
            return value.to_datetime().isoformat()
        except Exception:
            return str(value)
    else:
        return str(value)


def serialize_for_json(obj: Any, _depth: int = 0) -> Any:
    """
    Recursively serialize objects for JSON, handling datetime objects and other non-serializable types.

    Args:
        obj: Any object to serialize

    Returns:
        JSON-serializable version of the object

    Depth-capped: self-referential or auto-generating object graphs (e.g.
    Mock objects, ORM back-references) previously recursed for minutes and
    could freeze the event loop — beyond the cap we fall back to ``str``.
    """
    if _depth > 20:
        return str(obj)
    if obj is None:
        return None
    elif isinstance(obj, (datetime, date)):
        return serialize_datetime(obj)
    elif isinstance(obj, pd.Timestamp):
        # Handle pandas Timestamp specifically
        return serialize_datetime(obj)
    elif isinstance(obj, dict):
        return {key: serialize_for_json(value, _depth + 1) for key, value in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [serialize_for_json(item, _depth + 1) for item in obj]
    elif hasattr(obj, '__dict__'):
        # Handle objects with __dict__ attribute
        try:
            return serialize_for_json(obj.__dict__, _depth + 1)
        except Exception:
            return str(obj)
    elif hasattr(obj, 'isoformat'):
        # Handle any object with isoformat method (like datetime-like objects)
        try:
            return serialize_datetime(obj)
        except Exception:
            return str(obj)
    elif hasattr(obj, 'to_pydatetime'):
        # Handle pandas Timestamp and similar objects
        try:
            return serialize_datetime(obj.to_pydatetime())
        except Exception:
            return str(obj)
    elif hasattr(obj, '__iter__') and not isinstance(obj, (str, bytes)):
        # Handle other iterable objects
        try:
            return [serialize_for_json(item) for item in obj]
        except Exception:
            return str(obj)
    else:
        # For any other type, try to convert to string if it's not JSON serializable
        try:
            # Test if it's JSON serializable
            json.dumps(obj)
            return obj
        except (TypeError, ValueError):
            # If it's not JSON serializable, try to convert to string
            try:
                return str(obj)
            except Exception:
                # Final fallback
                return f"<non-serializable object of type {type(obj).__name__}>"


def safe_json_dumps(obj: Any, **kwargs) -> str:
    """
    Safely serialize an object to JSON with comprehensive datetime handling.
    
    Args:
        obj: Object to serialize
        **kwargs: Additional arguments to pass to json.dumps
        
    Returns:
        str: JSON string representation
    """
    try:
        # First try normal serialization
        return json.dumps(obj, **kwargs)
    except (TypeError, ValueError) as e:
        # If that fails, serialize with datetime handling
        try:
            serialized_obj = serialize_for_json(obj)
            return json.dumps(serialized_obj, **kwargs)
        except Exception:
            # Final fallback
            return json.dumps(str(obj), **kwargs)


def normalize_candle_time(time_value: Any) -> pd.Timestamp:
    """
    Normalize various time formats to pandas Timestamp for consistent handling.
    
    Args:
        time_value: Time value in various formats (str, datetime, pd.Timestamp, etc.)
        
    Returns:
        pd.Timestamp: Normalized timestamp
    """
    if isinstance(time_value, pd.Timestamp):
        return time_value
    elif isinstance(time_value, str):
        return pd.Timestamp(time_value)
    elif isinstance(time_value, datetime):
        return pd.Timestamp(time_value)
    elif hasattr(time_value, 'to_pydatetime'):
        return pd.Timestamp(time_value.to_pydatetime())
    elif hasattr(time_value, 'isoformat'):
        return pd.Timestamp(time_value)
    else:
        return pd.Timestamp(time_value)
