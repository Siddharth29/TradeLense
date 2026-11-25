from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

__all__ = ["await_next_bar_close"]


async def await_next_bar_close(interval: str, tz: str, offset: int = 0) -> datetime:
    """Sleep until the close of the next time bar and return its timestamp.

    Parameters
    ----------
    interval:
        Bar size such as ``"5m"``.
    tz:
        Timezone name understood by :class:`zoneinfo.ZoneInfo`.
    offset:
        Number of seconds to shift the alignment of bar closes.  For example,
        an offset of ``1800`` aligns hourly bars to ``:30`` past the hour.

    Notes
    -----
    Uses :func:`time.monotonic` for delay calculations and
    :func:`asyncio.sleep` to suspend execution without busy waiting.
    Returns the scheduled close time as a timezone-aware ``datetime``.
    """

    if interval.endswith("m"):
        step = int(interval[:-1]) * 60
    elif interval.endswith("h"):
        step = int(interval[:-1]) * 3600
    else:
        raise ValueError(f"Unsupported interval: {interval}")

    zone = ZoneInfo(tz)
    now = datetime.now(zone)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    seconds = (now - midnight).total_seconds()
    next_sec = math.ceil((seconds - offset) / step) * step + offset
    delay = next_sec - seconds

    end = time.monotonic() + delay
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(remaining)

    return midnight + timedelta(seconds=next_sec)
