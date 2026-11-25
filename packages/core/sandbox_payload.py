"""Request-value conversion for sandbox transport, independent of process execution."""

from datetime import datetime


def ensure_json_serializable(data):
    """Preserve sandbox datetime conversion without changing other value types."""
    if data is None:
        return None
    if isinstance(data, list):
        return [ensure_json_serializable(item) for item in data]
    elif isinstance(data, dict):
        return {key: ensure_json_serializable(value) for key, value in data.items()}
    elif isinstance(data, datetime):
        return data.isoformat()
    else:
        return data
