from __future__ import annotations

"""Utilities for configuration collections.

The application stores its configuration in a single pair of collections to
avoid scattering settings across multiple tables.  The ``configs`` collection
contains the latest configuration for each environment while
``config_history`` retains previous revisions.  Old history entries are
automatically pruned using a TTL index on ``updated_at``.
"""

from datetime import datetime, timezone
import hashlib
import json
import time
from typing import Any, Mapping, Tuple

from packages.db.config_schemas import AgentConfig

from motor.motor_asyncio import AsyncIOMotorCollection
from pymongo import ReadPreference, WriteConcern

from packages.core.offline_replay import offline_mode
from packages.core.config import MONGO_DB_NAME
from packages.core.trade_lifecycle import OptimisticLockError
from packages.db.client import get_client as _get_client

CONFIGS_COLLECTION = "configs"
CONFIG_HISTORY_COLLECTION = "config_history"
MODEL_CONFIG_DOMAIN = "model_config"
MODEL_CONFIG_ENV = "prod"
HISTORY_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days

_TTL_ENSURED = False

# (domain, env) -> (expiry_timestamp, get_client_ref, (doc, checksum))
# The cache is intentionally short lived (``_CACHE_TTL_SECONDS``) to minimise
# stale data while still avoiding repeated database reads when callers invoke
# :func:`get_current` in quick succession.  Entries are invalidated explicitly
# by ``put``/``rollback`` and automatically when expired.  The cache also stores
# the current ``get_client`` function so monkeypatching during tests automatically
# results in a miss.
_CACHE: dict[
    Tuple[str, str],
    Tuple[float, Any, Tuple[Mapping[str, Any] | None, str | None]],
] = {}
_CACHE_TTL_SECONDS = 5

async def get_client() -> Tuple[AsyncIOMotorCollection, AsyncIOMotorCollection]:
    """Return the configuration collections ensuring TTL on history.

    The TTL index on ``config_history.updated_at`` is created the first time
    this helper is invoked. Subsequent calls reuse the collections without
    re-issuing the index command.
    """
    global _TTL_ENSURED
    client = _get_client()
    db = client[MONGO_DB_NAME]
    configs = db[CONFIGS_COLLECTION]
    history = db[CONFIG_HISTORY_COLLECTION]
    if not offline_mode():
        configs = configs.with_options(read_preference=ReadPreference.SECONDARY_PREFERRED)
        history = history.with_options(read_preference=ReadPreference.SECONDARY_PREFERRED)
    if not _TTL_ENSURED:
        await history.create_index("updated_at", expireAfterSeconds=HISTORY_TTL_SECONDS)
        _TTL_ENSURED = True
    return configs, history


def _checksum(doc: Mapping[str, Any]) -> str:
    """Return a stable checksum for *doc*.

    The document is serialized using ``json.dumps`` with ``sort_keys`` to ensure
    deterministic output before hashing with SHA256.
    """

    payload = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


async def get_current(domain: str, env: str) -> Tuple[Mapping[str, Any] | None, str | None]:
    """Return the latest config and its checksum for ``domain``/``env``.

    Results are cached for a short period to reduce database load.  The cache is
    invalidated by :func:`put` and :func:`rollback` and expires after
    ``_CACHE_TTL_SECONDS`` seconds to avoid stale data.  If no configuration
    exists, ``(None, None)`` is returned."""

    key = (domain, env)
    now = time.monotonic()
    cached = _CACHE.get(key)
    if cached:
        expiry, client_ref, value = cached
        if now < expiry and client_ref is get_client:
            return value
        _CACHE.pop(key, None)

    configs, _ = await get_client()
    doc = await configs.find_one({"domain": domain, "env": env}, {"_id": 0})
    if not doc:
        result = (None, None)
    else:
        result = (doc.get("doc"), doc.get("checksum"))
    _CACHE[key] = (now + _CACHE_TTL_SECONDS, get_client, result)
    return result


async def get_model_config() -> Tuple[Mapping[str, Any] | None, str | None]:
    return await get_current(MODEL_CONFIG_DOMAIN, MODEL_CONFIG_ENV)


async def put(
    domain: str,
    env: str,
    cfg: Mapping[str, Any],
    *,
    if_match_checksum: str | None = None,
) -> Tuple[Any, Any]:
    """Persist *cfg* for ``domain``/``env`` with optimistic locking.

    ``if_match_checksum`` may be provided to enforce optimistic concurrency. If
    it does not match the current checksum an :class:`OptimisticLockError` is
    raised.  The function returns a tuple ``(config_upserted_id,
    history_inserted_id)`` representing the acknowledged identifiers of the
    operations on the ``configs`` and ``config_history`` collections
    respectively.
    """

    configs, history = await get_client()
    _CACHE.pop((domain, env), None)
    current = await configs.find_one({"domain": domain, "env": env}, {"_id": 0})
    if if_match_checksum is not None:
        expected = current.get("checksum") if current else None
        if expected != if_match_checksum:
            raise OptimisticLockError("checksum mismatch")
    version = (current.get("version", 0) + 1) if current else 1
    checksum = _checksum(cfg)
    now = datetime.now(timezone.utc)

    if not offline_mode():
        history = history.with_options(write_concern=WriteConcern(w=1))
        configs = configs.with_options(write_concern=WriteConcern(w=1))

    history_res = await history.insert_one(
        {
            "domain": domain,
            "env": env,
            "doc": dict(cfg),
            "checksum": checksum,
            "version": version,
            "updated_at": now,
        }
    )

    filt = {"domain": domain, "env": env}
    if if_match_checksum is not None:
        filt["checksum"] = if_match_checksum
    update_res = await configs.update_one(
        filt,
        {
            "$set": {
                "doc": dict(cfg),
                "checksum": checksum,
                "version": version,
                "updated_at": now,
            }
        },
        upsert=True,
    )

    if if_match_checksum is not None and update_res.matched_count == 0:
        raise OptimisticLockError("checksum mismatch")

    _CACHE.pop((domain, env), None)
    return update_res.upserted_id, history_res.inserted_id


async def list_versions(domain: str, env: str) -> list[int]:
    """Return list of version numbers for ``domain``/``env``."""

    _, history = await get_client()
    cursor = history.find({"domain": domain, "env": env}, {"version": 1, "_id": 0}).sort(
        "version", 1
    )
    docs = await cursor.to_list(length=None)
    return [d["version"] for d in docs]


async def rollback(domain: str, env: str, version: int) -> Tuple[Any, Any]:
    """Roll back to *version* for ``domain``/``env``.

    The historical configuration is copied to the current configs collection as
    a new version.  Returns the same values as :func:`put`.
    """

    configs, history = await get_client()
    _CACHE.pop((domain, env), None)
    hist = await history.find_one(
        {"domain": domain, "env": env, "version": version}, {"_id": 0}
    )
    if not hist:
        raise ValueError("version not found")
    current = await configs.find_one({"domain": domain, "env": env}, {"_id": 0})
    expected = current.get("checksum") if current else None
    return await put(
        domain,
        env,
        hist["doc"],
        if_match_checksum=expected,
    )


async def get_agents_config(env: str) -> Tuple[AgentConfig | None, str | None]:
    """Return the ``AgentConfig`` for ``env`` and its checksum.

    The underlying document is retrieved via :func:`get_current` from the
    generic configuration repository and validated against the
    :class:`AgentConfig` schema.  ``None`` is returned if no configuration is
    present.
    """

    doc, checksum = await get_current("agents", env)
    if doc is None:
        return None, checksum

    # Migration support for legacy candle count fields.
    doc = dict(doc)
    if "candle_count" in doc:
        legacy = doc.pop("candle_count")
        doc.setdefault("candles_5_count", legacy)
        doc.setdefault("candles_15_count", legacy)
    if "candle_count_5m" in doc:
        doc.setdefault("candles_5_count", doc.pop("candle_count_5m"))
    if "candle_count_15m" in doc:
        doc.setdefault("candles_15_count", doc.pop("candle_count_15m"))

    return AgentConfig.model_validate(doc, strict=False), checksum


async def put_agents_config(
    cfg: AgentConfig,
    env: str,
    *,
    if_match_checksum: str | None = None,
) -> Tuple[Any, Any]:
    """Persist *cfg* for ``env`` using the generic config repository."""

    return await put(
        "agents",
        env,
        cfg.model_dump(mode="json"),
        if_match_checksum=if_match_checksum,
    )


async def get_agent_config(agent_id: str, env: str) -> Tuple[AgentConfig | None, str | None]:
    """Return the ``AgentConfig`` for ``agent_id``/``env`` and its checksum.

    Configurations are stored under a namespaced domain ``agents/<agent_id>`` to
    allow distinct settings per agent.  ``None`` is returned when no
    configuration exists for the requested identifier.
    """

    doc, checksum = await get_current(f"agents/{agent_id}", env)
    if doc is None:
        return None, checksum

    doc = dict(doc)
    if "candle_count" in doc:
        legacy = doc.pop("candle_count")
        doc.setdefault("candles_5_count", legacy)
        doc.setdefault("candles_15_count", legacy)
    if "candle_count_5m" in doc:
        doc.setdefault("candles_5_count", doc.pop("candle_count_5m"))
    if "candle_count_15m" in doc:
        doc.setdefault("candles_15_count", doc.pop("candle_count_15m"))

    return AgentConfig.model_validate(doc, strict=False), checksum


async def put_agent_config(
    agent_id: str,
    cfg: AgentConfig,
    env: str,
    *,
    if_match_checksum: str | None = None,
) -> Tuple[Any, Any]:
    """Persist ``cfg`` for ``agent_id``/``env`` using the generic repository."""

    return await put(
        f"agents/{agent_id}",
        env,
        cfg.model_dump(mode="json"),
        if_match_checksum=if_match_checksum,
    )

__all__ = [
    "CONFIGS_COLLECTION",
    "CONFIG_HISTORY_COLLECTION",
    "HISTORY_TTL_SECONDS",
    "get_client",
    "get_current",
    "put",
    "list_versions",
    "rollback",
    "get_agents_config",
    "put_agents_config",
    "get_agent_config",
    "put_agent_config",
]
