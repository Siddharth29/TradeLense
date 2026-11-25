"""Per-session market statistics for the session-aware SL floor and ER context.

Consumes ``data/profiling/session_stats.json`` written by
``scripts/profiling/asset_profiler.py`` (4-year order statistics, per asset ×
UTC session). Two consumers:

  1. ``session_noise_floor_pct`` — the session term of the volatility SL floor
     (``recommended_stop_distance``): the 0.6×ATR(1h) live floor tracks dead
     sessions DOWN into noise (freeze week 1: EUR/GBP 5–6 pip stops in an
     ATR lull, then swept by moves that ran +2.0R/+2.98R/+5.17R post-exit —
     LP-03). The 4-year session quantile does not collapse in a lull.
     Session-boundary lookahead: a trade lives ~TTL (up to 2h) beyond entry, so
     the floor uses the noisiest session the position will plausibly see —
     max(now, now+2h) — closing the "born in Asia, dies at the EU open" hole.
  2. ``session_er_percentile`` — turns a live ``er_1h`` reading into
     "Nth percentile for this asset in this session" prompt context, so the
     judge sees HOW dead 0.003 actually is instead of a bare label
     (freeze week 1: EUR buys fired at session ER 0.003–0.024, 0W-4L).

Inert by design: every function returns ``None`` when the table is missing,
the asset is not enabled (floors), or inputs are invalid — callers keep their
pre-existing behaviour byte-identical.

Session boundaries (UTC, H1-bar aligned) MUST match the profiler's SESSIONS.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from .logger import get_logger

logger = get_logger(__name__)

# Keep in sync with scripts/profiling/asset_profiler.py::SESSIONS
_SESSIONS: dict[str, tuple[int, int]] = {
    "asia": (0, 8), "eu": (8, 13), "ny": (13, 18), "evening": (18, 24),
}

_cache: dict[str, Any] = {"path": None, "mtime": None, "data": None}


def session_of(when: Optional[datetime] = None) -> str:
    """UTC session name for a datetime (default: now)."""
    dt = when or datetime.now(timezone.utc)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    hour = dt.hour
    for name, (lo, hi) in _SESSIONS.items():
        if lo <= hour < hi:
            return name
    return "asia"


def session_bounds(when: Optional[datetime] = None) -> tuple[str, datetime, datetime]:
    """(session name, UTC start, UTC end) of the session containing *when*.

    Added 2026-07-25 for the session-tape observation (LP-08 Phase 0): the
    tape needs "candles since session open", which needs the open as a
    datetime, not just the session's name.
    """
    dt = when or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    name = session_of(dt)
    lo, hi = _SESSIONS[name]
    start = dt.replace(hour=lo, minute=0, second=0, microsecond=0)
    end = (dt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=hi)
           if hi == 24 else dt.replace(hour=hi, minute=0, second=0, microsecond=0))
    return name, start, end


def _load_table() -> Dict[str, Any]:
    """Load (and mtime-cache) the session-stats table; {} when unavailable."""
    from .config import SESSION_STATS_PATH
    path = SESSION_STATS_PATH
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    if _cache["path"] == path and _cache["mtime"] == mtime and _cache["data"] is not None:
        return _cache["data"]
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f).get("assets") or {}
    except (OSError, ValueError) as e:
        logger.warning(f"session_stats: failed to load {path}: {e}")
        data = {}
    _cache.update(path=path, mtime=mtime, data=data)
    return data


def _asset_key(asset: Optional[str]) -> str:
    # Broker demo suffixes ('XAUUSDm') must resolve to the plain symbol.
    key = (asset or "").strip().upper()
    return key[:-1] if key.endswith("M") and len(key) > 4 else key


def session_noise_floor_pct(asset: Optional[str], when: Optional[datetime] = None) -> Optional[float]:
    """Session noise floor as a % of price, or None (inert).

    ``SESSION_NOISE_FLOOR_MULT × tr_pct[SESSION_NOISE_FLOOR_QUANTILE]`` for the
    noisier of the entry session and the session ~2h ahead (TTL lookahead).
    Only for assets enabled in ``SESSION_NOISE_FLOOR_ENABLED_BY_ASSET``.
    """
    from .config import (
        SESSION_NOISE_FLOOR_ENABLED_BY_ASSET,
        SESSION_NOISE_FLOOR_MULT,
        SESSION_NOISE_FLOOR_QUANTILE,
    )
    key = _asset_key(asset)
    if not SESSION_NOISE_FLOOR_ENABLED_BY_ASSET.get(key, False):
        return None
    table = _load_table().get(key)
    if not table:
        return None
    now = when or datetime.now(timezone.utc)
    vals = []
    for dt in (now, now + timedelta(hours=2)):
        row = table.get(session_of(dt))
        if row:
            q = (row.get("tr_pct") or {}).get(SESSION_NOISE_FLOOR_QUANTILE)
            if q:
                vals.append(float(q))
    if not vals:
        return None
    return float(SESSION_NOISE_FLOOR_MULT) * max(vals)


def session_er_percentile(asset: Optional[str], er_value: Optional[float],
                          when: Optional[datetime] = None) -> Optional[int]:
    """Percentile (10..90, decile-resolved) of ``er_value`` within this asset's
    historical er_1h distribution for the current UTC session, or None (inert).

    Available for EVERY asset in the table (context, not a gate — no enable
    flag): 10 = at/below the p10 historical reading (dead tape), 90 = at/above
    p90 (exceptionally directional).
    """
    if er_value is None:
        return None
    table = _load_table().get(_asset_key(asset))
    if not table:
        return None
    row = table.get(session_of(when))
    deciles = (row or {}).get("er_deciles")
    if not deciles or len(deciles) != 9:
        return None
    try:
        er = float(er_value)
    except (TypeError, ValueError):
        return None
    pct = 10
    for i, d in enumerate(deciles):        # deciles[i] = p(10*(i+1))
        if er >= d:
            pct = min(90, 10 * (i + 2))
    return pct
