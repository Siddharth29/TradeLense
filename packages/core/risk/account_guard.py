"""Account-wide daily drawdown kill switch.

This is an *additive* safety gate. It only blocks NEW entries once the account
equity has fallen :data:`ACCOUNT_MAX_DAILY_DD_PCT` below the day's starting
equity. It never touches open positions or their management — the matured
trade-management logic is untouched.

The per-day starting-equity baseline is persisted (collection ``account_risk``)
and keyed by UTC date, so a process restart cannot reset the day's loss memory
(closing the loophole where an in-memory counter zeroes on every crash).

If the account or persisted baseline cannot be verified, the check defers NEW
entries until a later successful read. Open-position management is independent.
"""

from __future__ import annotations

import asyncio
import logging
import math
import hashlib
from datetime import datetime, timezone
from typing import Any, Tuple
from pymongo.errors import DuplicateKeyError

from packages.db.client import get_client
from packages.core.config import (
    MONGO_DB_NAME,
    ACCOUNT_DAILY_DD_GUARD_ENABLED,
    ACCOUNT_MAX_DAILY_DD_PCT,
)

logger = logging.getLogger(__name__)

_COLLECTION = "account_risk"


def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _account_fingerprint(broker: Any, bal: dict) -> str:
    """Stable per-account identity so a paper ($10M) and a live (~$700) account
    can NEVER share a day-start baseline.

    Prefer the real MT5 login/server (the adapter stores ``_credentials``); fall
    back to an explicit account_id or broker class for paper brokers. Identity
    must never depend on equity: crossing a digit boundary must not reset DD.
    """
    creds = getattr(broker, "_credentials", None) or {}
    login = creds.get("login")
    if login:
        return f"mt5:{creds.get('server') or '?'}:{login}"
    if hasattr(broker, "_mt5"):
        raise ValueError("MT5 account identity is unavailable")
    cls = type(broker).__name__
    return f"{cls}:{getattr(broker, 'account_id', None) or 'default'}"


async def _seed_or_get_start_equity(current_equity: float, account: str) -> float:
    """Return the persisted day-start equity for ``account``, seeding it on the
    first call of the UTC day. Keyed by ``(date, account)`` and seeded with
    ``$setOnInsert`` and a deterministic Mongo _id for concurrent first seeds.
    Existing date/account documents remain authoritative (no loss reset)."""
    col = get_client()[MONGO_DB_NAME][_COLLECTION]
    flt = {"date": _today_key(), "account": account}
    doc = await col.find_one(flt)
    if doc is None:
        seed_id = "daily-dd:" + hashlib.sha256(
            f"{flt['date']}:{account}".encode("utf-8")
        ).hexdigest()
        try:
            await col.update_one(
                {"_id": seed_id},
                {"$setOnInsert": {**flt, "start_equity": float(current_equity),
                                  "created_at": datetime.now(timezone.utc)}},
                upsert=True,
            )
        except DuplicateKeyError:
            pass  # Another process seeded this exact account/day first.
        doc = await col.find_one(flt)
    start = doc.get("start_equity") if doc else None
    if (isinstance(start, bool) or not isinstance(start, (int, float))
            or not math.isfinite(start) or start <= 0):
        raise ValueError("Persisted daily drawdown baseline is unavailable or invalid")
    return float(start)


async def account_trading_halted(broker: Any) -> Tuple[bool, str]:
    """Return ``(halted, reason)``.

    ``halted`` is True when the limit is reached or account risk is unknown.
    Never raises. The configured disabled mode remains an explicit opt-out.
    """
    if not ACCOUNT_DAILY_DD_GUARD_ENABLED:
        return False, ""
    try:
        if not hasattr(broker, "balances"):
            return True, "account balance source unavailable; new entries deferred"
        bal = await asyncio.to_thread(broker.balances)
        if not bal:
            return True, "account balances unavailable; new entries deferred"
        # Zero equity is a real account state, not a missing value that can
        # safely fall back to the cash balance before unrealized losses.
        raw_equity = bal.get("equity")
        equity = float(raw_equity if raw_equity is not None else bal.get("cash", 0.0))
        if not math.isfinite(equity) or equity <= 0:
            return True, "account equity is non-positive or invalid; new entries halted"
        account = _account_fingerprint(broker, bal)
        start = await _seed_or_get_start_equity(equity, account)
        if not math.isfinite(start) or start <= 0:
            return True, "account drawdown baseline invalid; new entries deferred"

        # The account fingerprint isolates paper/live baselines. Loss size
        # alone cannot establish contamination: a gap can exceed the limit.
        # Correct bad migrations/cashflows explicitly, never by erasing DD.
        dd_pct = (start - equity) / start * 100.0
        if dd_pct >= ACCOUNT_MAX_DAILY_DD_PCT:
            return True, (
                f"account daily DD {dd_pct:.2f}% ≥ {ACCOUNT_MAX_DAILY_DD_PCT:.2f}% "
                f"(start={start:.2f}, equity={equity:.2f}, account={account})"
            )
        return False, ""
    except Exception as e:
        logger.error(f"account_guard check failed (deferring new entries): {e}")
        return True, "account drawdown could not be verified; new entries deferred"


__all__ = ["account_trading_halted"]
