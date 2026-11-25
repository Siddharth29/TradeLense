"""Process-global in-memory store of the last few CLOSED trades per asset.

Why this exists
---------------
The recent-trades-in-prompt context (anti-churn / re-entry reasoning) used to be
sourced from one Mongo query per cycle, gated by a request-time freshness cap
(``RECENT_TRADES_MAX_BARS``, 2h). In practice ~half the AI calls saw an EMPTY
``RECENT_TRADES`` section: a fresh ``AgentRunner`` is built every cycle (no
instance state survives) and any trade older than the cap was dropped. The model
therefore kept re-trading the same losing idea with no memory of having tried it.

This module is the fix the live loop actually needs: a tiny ring buffer —
``deque(maxlen=N)`` per asset — that lives for the whole process. Every closed
trade is pushed exactly once from the single DB close path
(``update_comprehensive_trade_exit``); every AI call reads the last N back out
with no DB round-trip and no freshness cap. Keep the last N, drop the older ones.
With ~6 assets that is at most ~18 records resident.

Request-relative fields (``ago_min`` / ``bars_elapsed``) are NOT stored; they are
recomputed against ``now`` at read time, so the same stored record reads
correctly on every cycle.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Optional

from packages.core.models import RecentTradeView

# Partial-exit child rows are their own closed docs; including them would
# double-count one trade as several outcomes and corrupt the anti-churn /
# re-entry reasoning. Mirror the Mongo query's exclusion here.
_PARTIAL_REASONS = {"TP1_PARTIAL", "TP2_PARTIAL"}

_DEFAULT_MAXLEN = 3

_store: dict[str, deque] = {}
_lock = Lock()
_maxlen_cache: Optional[int] = None


def _maxlen() -> int:
    """How many closed trades to keep per asset (config-driven, cached)."""
    global _maxlen_cache
    if _maxlen_cache is not None:
        return _maxlen_cache
    n = _DEFAULT_MAXLEN
    try:
        from packages.core.config import RECENT_TRADES_MEMORY_COUNT
        n = int(RECENT_TRADES_MEMORY_COUNT)
    except Exception:
        n = _DEFAULT_MAXLEN
    _maxlen_cache = max(1, n)
    return _maxlen_cache


def _normalize_side(side: Optional[str]) -> str:
    s = (side or "").lower()
    return {"long": "buy", "short": "sell"}.get(s, s)


def record_closed_trade(
    asset: Optional[str],
    *,
    side: Optional[str],
    entry: Optional[float],
    exit: Optional[float],
    outcome: Optional[str],
    pnl: Optional[float],
    closed_at: Optional[datetime] = None,
) -> None:
    """Push one closed-trade outcome into the asset's ring buffer (newest first).

    Inert by contract: any bad/missing input is tolerated and never raised — the
    DB close path that calls this must never break on a memory hiccup. Partial
    child rows are dropped so the buffer holds whole-trade outcomes only.
    """
    if not asset:
        return
    if outcome in _PARTIAL_REASONS:
        return
    closed_at = closed_at or datetime.now(timezone.utc)
    if closed_at.tzinfo is None:
        closed_at = closed_at.replace(tzinfo=timezone.utc)

    move_pct: Optional[float] = None
    if isinstance(entry, (int, float)) and isinstance(exit, (int, float)) and entry > 0:
        move_pct = round(abs(exit - entry) / entry * 100, 3)

    # TRUE outcome from PnL: trailing SL makes most winners exit "SL" too, so the
    # win/loss flag must come from money, not the exit_reason label.
    if isinstance(pnl, (int, float)):
        result = "win" if pnl > 0 else "loss" if pnl < 0 else "scratch"
    else:
        result = None

    rec = {
        "_closed_at": closed_at,
        "side": _normalize_side(side),
        "entry": entry,
        "exit": exit,
        "move_pct": move_pct,
        "outcome": outcome,
        "result": result,
        "pnl": round(pnl, 2) if isinstance(pnl, (int, float)) else None,
    }
    with _lock:
        dq = _store.get(asset)
        if dq is None or dq.maxlen != _maxlen():
            dq = deque(dq or (), maxlen=_maxlen())
            _store[asset] = dq
        dq.appendleft(rec)


def _to_view(rec: dict, now: datetime) -> RecentTradeView:
    t = rec.get("_closed_at")
    age_min = int((now - t).total_seconds() / 60) if t else None
    bars_elapsed = (age_min // 5) if age_min is not None else None
    return {
        "ago_min": age_min,
        "bars_elapsed": bars_elapsed,
        "side": rec.get("side"),
        "entry": rec.get("entry"),
        "exit": rec.get("exit"),
        "move_pct": rec.get("move_pct"),
        "outcome": rec.get("outcome"),
        "result": rec.get("result"),
        "pnl": rec.get("pnl"),
    }


def get_recent_trades(asset: str, limit: Optional[int] = None) -> list[RecentTradeView]:
    """Last ``limit`` closed trades for *asset*, newest-first, request-fresh.

    ``ago_min`` / ``bars_elapsed`` are computed against ``now`` so the values are
    correct for THIS call. Returns ``[]`` when nothing is buffered yet.
    """
    n = _maxlen() if limit is None else max(1, int(limit))
    now = datetime.now(timezone.utc)
    with _lock:
        dq = _store.get(asset)
        items = list(dq)[:n] if dq else []
    return [_to_view(rec, now) for rec in items]


def has_recent_trades(asset: str) -> bool:
    with _lock:
        dq = _store.get(asset)
        return bool(dq)


def seed_from_views(asset: str, views: list[RecentTradeView]) -> None:
    """One-time cold-start backfill from a DB query, only if the buffer is empty.

    ``views`` are newest-first ``RecentTradeView`` rows (from
    ``get_recent_closed_trades``). Their ``ago_min`` is used to reconstruct a
    close timestamp so later reads recompute freshness correctly. No-op once the
    buffer holds anything — live closes are the source of truth thereafter.
    """
    if not asset or not views:
        return
    now = datetime.now(timezone.utc)
    with _lock:
        if _store.get(asset):
            return  # already populated by a live close — do not overwrite
        dq: deque = deque(maxlen=_maxlen())
        # views are newest-first; appendleft in reverse so newest lands at front.
        for v in reversed(views[: _maxlen()]):
            ago = v.get("ago_min")
            closed_at = now - timedelta(minutes=int(ago)) if isinstance(ago, (int, float)) else now
            dq.appendleft({
                "_closed_at": closed_at,
                "side": v.get("side"),
                "entry": v.get("entry"),
                "exit": v.get("exit"),
                "move_pct": v.get("move_pct"),
                "outcome": v.get("outcome"),
                "result": v.get("result"),
                "pnl": v.get("pnl"),
            })
        _store[asset] = dq


def reset() -> None:
    """Clear all buffers (test hook)."""
    global _maxlen_cache
    with _lock:
        _store.clear()
    _maxlen_cache = None


__all__ = [
    "record_closed_trade",
    "get_recent_trades",
    "has_recent_trades",
    "seed_from_views",
    "reset",
]
