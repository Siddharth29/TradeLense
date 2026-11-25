from __future__ import annotations

"""Utilities for working with datetimes consistently in UTC."""

from datetime import datetime, timezone
from typing import Any


def to_aware_utc(value: Any | None) -> datetime | None:
    """Return *value* as a timezone-aware ``datetime`` in UTC.

    Parameters
    ----------
    value:
        May be a ``datetime`` (naive or aware), an epoch seconds number
        (``int``/``float`` or digit-only ``str``), or an ISO8601 formatted
        ``str``.  ``None`` is passed through unchanged.

    Returns
    -------
    datetime | None
        A timezone-aware ``datetime`` normalised to UTC.  ``None`` is
        returned for ``None`` input values.

    Raises
    ------
    TypeError
        If ``value`` is of an unsupported type.
    """

    if value is None:
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)

    if isinstance(value, str):
        s = value.strip()
        # Try ISO8601 parsing first
        iso = s
        if iso.endswith("Z"):
            iso = iso[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            # Fallback to epoch seconds
            try:
                return datetime.fromtimestamp(float(s), tz=timezone.utc)
            except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
                raise TypeError(f"unsupported datetime string: {value!r}") from exc
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    raise TypeError(f"unsupported type: {type(value)!r}")
