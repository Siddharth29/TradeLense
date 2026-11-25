from __future__ import annotations

"""Database bootstrap utilities."""

import asyncio
from typing import Iterable

from packages.core.config import MONGO_DB_NAME
from packages.db.client import get_client

# Collections managed by the bootstrap process. The ``candles`` collection
# stores OHLC data keyed by ``asset`` and ``ts`` and therefore requires a
# different compound index compared to the other collections which use
# ``time`` and also enforce a ``client_order_id`` uniqueness constraint.
COLLECTIONS: Iterable[str] = (
    "trades",
    "orders",
    "fills",
    "signals",
    "logs",
    "candles_5m",
    "candles_15m",
    "daily_metrics",
    "candles",
    "broker_sessions",
    "wallet_snapshots",
    "agent_runs",
    "gpt_responses",
    "trigger_checks",
    "gpt_call_summaries",
    "gpt_threads",
    "multi_expert_cycles",
    "backtests",
    "backtest_iterations",
    "active_trades",
)

# Event collections are those that represent lifecycle events and enforce
# additional uniqueness constraints on both ``client_order_id`` and
# ``checksum`` to guarantee idempotent writes.
_EVENT_COLLECTIONS = {"trades", "orders", "fills", "signals", "gpt_responses", "trigger_checks"}

# Configuration collections may be added in the future. They do not follow
# the standard ``asset``/``time`` pattern and instead are keyed by the
# combination of ``domain``, ``env`` and ``version``.  When new configuration
# collections are introduced they should be appended to this set.
_CONFIG_COLLECTIONS: set[str] = set()

_LOG_TTL_SECONDS = 60 * 60 * 24  # 1 day
_SIGNAL_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days
_WALLET_TTL_SECONDS = 60 * 60 * 24 * 7  # ~7 days
_AGENT_RUN_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days
_GPT_RESPONSE_TTL_SECONDS = 60 * 60 * 24 * 90  # 90 days
_TRIGGER_CHECK_TTL_SECONDS = 60 * 60 * 24 * 90  # 90 days
_GPT_CALL_SUMMARY_TTL_SECONDS = 60 * 60 * 24 * 365  # 1 year
_GPT_THREADS_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days
_MULTI_EXPERT_CYCLES_TTL_SECONDS = 60 * 60 * 24 * 90  # 90 days

_TTL_INDEXES = {
    "logs": _LOG_TTL_SECONDS,
    "signals": _SIGNAL_TTL_SECONDS,
    "agent_runs": _AGENT_RUN_TTL_SECONDS,
    "gpt_responses": _GPT_RESPONSE_TTL_SECONDS,
    "trigger_checks": _TRIGGER_CHECK_TTL_SECONDS,
    "gpt_call_summaries": _GPT_CALL_SUMMARY_TTL_SECONDS,
    "gpt_threads": _GPT_THREADS_TTL_SECONDS,
    "multi_expert_cycles": _MULTI_EXPERT_CYCLES_TTL_SECONDS,
}


async def init_db() -> None:
    """Create application collections and indexes if missing."""
    client = get_client()
    db = client[MONGO_DB_NAME]
    existing = await db.list_collection_names()
    for name in COLLECTIONS:
        if name not in existing:
            await db.create_collection(name)
        coll = db[name]
        if name == "candles":
            # Candles are keyed by asset and timestamp
            await coll.create_index([("asset", 1), ("ts", 1)])
            continue

        if name == "broker_sessions":
            await coll.create_index("expires_at", expireAfterSeconds=0)
            continue

        if name == "wallet_snapshots":
            await coll.create_index("as_of", expireAfterSeconds=_WALLET_TTL_SECONDS)
            continue

        if name == "agent_runs":
            await coll.create_index([("asset", 1), ("slot_id", 1)], unique=True)
            await coll.create_index("time", expireAfterSeconds=_AGENT_RUN_TTL_SECONDS)
            continue

        if name == "gpt_threads":
            await coll.create_index([("asset", 1), ("agent_id", 1)], unique=True)
            await coll.create_index("created_at", expireAfterSeconds=_GPT_THREADS_TTL_SECONDS)
            await coll.create_index("thread_id", unique=True)
            continue

        if name == "multi_expert_cycles":
            await coll.create_index([("asset", 1), ("agent_id", 1)])
            await coll.create_index([("cycle_id", 1)], unique=True)
            await coll.create_index("started_at", expireAfterSeconds=_MULTI_EXPERT_CYCLES_TTL_SECONDS)
            await coll.create_index([("asset", 1), ("started_at", -1)])
            continue

        if name in ("candles_5m", "candles_15m"):
            # Historical candle collections: no client_order_id, no unique constraints
            # Only compound index on asset and time for efficient queries
            await coll.create_index([("asset", 1), ("time", 1)])
            # No TTL index - historical data should persist
            continue

        if name == "backtests":
            await coll.create_index([("status", 1)])
            await coll.create_index([("asset", 1), ("created_at", -1)])
            await coll.create_index("checksum", unique=True, sparse=True)
            continue

        if name == "backtest_iterations":
            await coll.create_index([("backtest_id", 1), ("iteration_number", 1)], unique=True)
            await coll.create_index([("backtest_id", 1)])
            await coll.create_index("checksum", unique=True, sparse=True)
            continue

        if name in _CONFIG_COLLECTIONS:
            await coll.create_index(
                [("domain", 1), ("env", 1), ("version", 1)], unique=True
            )
            continue

        # Default case uses ``asset`` and ``time`` keys with unique
        # ``client_order_id``. Event collections additionally enforce a unique
        # ``checksum`` index to prevent accidental duplicates.
        await coll.create_index([("asset", 1), ("time", 1)])
        await coll.create_index("client_order_id", unique=True)
        if name in _EVENT_COLLECTIONS:
            await coll.create_index("checksum", unique=True)
        if name in _TTL_INDEXES:
            await coll.create_index("time", expireAfterSeconds=_TTL_INDEXES[name])


async def migrate_mt5_login_id() -> None:
    """Rename ``mt5.login`` to ``mt5.loginId`` in stored configs."""

    client = get_client()
    db = client[MONGO_DB_NAME]
    cfg_coll = db["configs"]
    hist_coll = db["config_history"]
    filt = {
        "domain": "brokers",
        "doc.mt5.login": {"$exists": True},
        "doc.mt5.loginId": {"$exists": False},
    }
    update = {"$rename": {"doc.mt5.login": "doc.mt5.loginId"}}
    await cfg_coll.update_many(filt, update)
    await hist_coll.update_many(filt, update)


async def _run_all() -> None:
    await init_db()
    await migrate_mt5_login_id()


def main() -> None:
    asyncio.run(_run_all())


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    main()
