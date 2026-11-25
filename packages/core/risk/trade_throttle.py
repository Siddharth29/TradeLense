"""Daily entry cap and time-based cooldown from whole-position net outcomes.

Connected MT5 accounts reconcile their account-scoped operational SQLite ledger
before authorizing exposure. Missing history or failed reconciliation defers a
new entry. Analytics persistence has no influence on this path. Legacy brokers
without the operational snapshot API retain their prior Mongo fallback.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Tuple

from packages.db.client import get_client
from packages.core.config import (
    MONGO_DB_NAME,
    TRADE_THROTTLE_ENABLED,
    TRADE_COOLDOWN_MINUTES,
    BACKTEST_MAX_TRADES_PER_DAY,
    BACKTEST_COOLDOWN_SL_THRESHOLD,
)

logger = logging.getLogger(__name__)

_COLLECTION = "comprehensive_trades"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _day_start() -> datetime:
    n = _utc_now()
    return n.replace(hour=0, minute=0, second=0, microsecond=0)


def _as_utc(t):
    """Re-attach UTC to a Mongo-returned datetime (Motor is not tz_aware, so
    BSON datetimes decode naive; subtracting from an aware ``now`` would raise)."""
    if t is not None and getattr(t, "tzinfo", None) is None:
        return t.replace(tzinfo=timezone.utc)
    return t


async def trading_throttled(asset: str, *, broker=None, now=None) -> Tuple[bool, str]:
    """Return ``(blocked, reason)`` for *asset* at the current moment.

    ``blocked`` is True when today's per-asset trade cap is reached or a
    whole-position loss cooldown is active. Broker reconciliation failures block.
    """
    if not TRADE_THROTTLE_ENABLED:
        return False, ""
    if not asset:
        return False, ""
    if broker is not None and callable(getattr(broker, "operational_snapshot", None)):
        import asyncio
        from ..operational_ledger import sync_broker
        def check():
            account, ledger = sync_broker(broker, now=now)
            try:
                return ledger.throttle(account, asset, now or _utc_now(),
                    cap=BACKTEST_MAX_TRADES_PER_DAY, loss_threshold=BACKTEST_COOLDOWN_SL_THRESHOLD,
                    cooldown_minutes=TRADE_COOLDOWN_MINUTES)
            finally:
                ledger.close()
        try:
            return await asyncio.to_thread(check)
        except Exception as exc:
            logger.error(f"Operational ledger unavailable; deferring entry: {exc}")
            return True, "operational ledger could not be reconciled"
    try:
        col = get_client()[MONGO_DB_NAME][_COLLECTION]
        day_start = _day_start()

        # 1) Daily entry cap — count distinct entries today for this asset.
        # TP1/TP2 partial closes are stored as their own docs (each with a
        # trade_executed_at), so counting them would inflate the cap ~3x for a
        # fully-worked trade and throttle entries far too early. Exclude them.
        _PARTIALS = ["TP1_PARTIAL", "TP2_PARTIAL"]
        count = await col.count_documents(
            {
                "asset": asset,
                "trade_executed_at": {"$gte": day_start},
                "exit_reason": {"$nin": _PARTIALS},
            }
        )
        if count >= BACKTEST_MAX_TRADES_PER_DAY:
            return True, f"daily trade cap reached ({count}/{BACKTEST_MAX_TRADES_PER_DAY})"

        # 2) Consecutive-SL cooldown — look at the most recent closes today.
        threshold = int(BACKTEST_COOLDOWN_SL_THRESHOLD)
        if threshold > 0 and TRADE_COOLDOWN_MINUTES > 0:
            cursor = (
                col.find(
                    {
                        "asset": asset,
                        "trade_status": "closed",
                        "trade_closed_at": {"$gte": day_start},
                        # Streak is about whole-trade outcomes; partial-exit child
                        # rows (always a profit) would otherwise break a real SL
                        # streak and suppress the cooldown.
                        "exit_reason": {"$nin": _PARTIALS},
                    },
                    projection={"pnl": 1, "exit_reason": 1, "trade_closed_at": 1},
                )
                .sort("trade_closed_at", -1)
                .limit(threshold)
            )
            recent = await cursor.to_list(length=threshold)
            if len(recent) >= threshold and all(_is_loss(d) for d in recent):
                newest = _as_utc(recent[0].get("trade_closed_at"))
                if newest is not None:
                    elapsed_min = (_utc_now() - newest).total_seconds() / 60.0
                    if elapsed_min < TRADE_COOLDOWN_MINUTES:
                        remaining = TRADE_COOLDOWN_MINUTES - elapsed_min
                        return True, (
                            f"SL cooldown active ({threshold} consecutive losses; "
                            f"{remaining:.0f} min remaining)"
                        )
        return False, ""
    except Exception as e:  # fail-open
        logger.error(f"trade_throttle check failed (allowing trade): {e}")
        return False, ""


def _is_loss(doc: dict) -> bool:
    """A closed trade counts toward the SL streak when its P&L is negative.

    P&L is the authoritative signal (an SL exit can still net ~0 after a BE
    trail). A missing/None P&L is treated as NOT a loss so an incomplete record
    never arms the cooldown.
    """
    pnl = doc.get("pnl")
    return isinstance(pnl, (int, float)) and pnl < 0


__all__ = ["trading_throttled"]
