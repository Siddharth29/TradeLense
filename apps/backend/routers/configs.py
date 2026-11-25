from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from prometheus_client import Histogram
from typing import Any

from packages.db.repositories import configs as repo
from packages.db.config_schemas import ModelConfig

logger = logging.getLogger(__name__)

_config_write_latency = Histogram(
    "config_write_latency_seconds", "Latency of configuration writes"
)

router = APIRouter()


class RollbackPayload(BaseModel):
    env: str
    version: int


def _actor_from(request: Request) -> str:
    return request.headers.get("x-actor") or request.headers.get("actor") or "unknown"

@router.post("/{domain}/rollback")
async def rollback_config(domain: str, payload: RollbackPayload, request: Request):
    """Roll back configuration for ``domain`` to *payload.version*.

    The operation is logged and its latency recorded in the Prometheus
    histogram.
    """

    actor = _actor_from(request)
    start = time.perf_counter()
    try:
        await repo.rollback(domain, payload.env, payload.version)
    except ValueError as exc:  # pragma: no cover - defensive
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _config_write_latency.observe(time.perf_counter() - start)

    cfg_coll, _ = await repo.get_client()
    meta = await cfg_coll.find_one(
        {"domain": domain, "env": payload.env}, {"version": 1, "checksum": 1, "_id": 0}
    )
    version = meta.get("version") if meta else None
    checksum = meta.get("checksum") if meta else None
    logger.info(
        "config rollback",
        extra={"actor": actor, "domain": domain, "version": version, "checksum": checksum},
    )
    return {"status": "ok", "version": version, "checksum": checksum}


@router.get("/{domain}/versions")
async def list_config_versions(domain: str, env: str, cursor: int | None = None, limit: int = 50):
    """Return paginated history for domain/env.

    The response shape matches the frontend's expectation: a list of versions
    with embedded ``doc`` and ``updated_at`` fields and an optional
    ``nextCursor`` when more items are available. When no history is present,
    an empty page is returned instead of 404.
    """

    cfg_coll, hist_coll = await repo.get_client()
    filt: dict[str, Any] = {"domain": domain, "env": env}
    if cursor is not None:
        filt["version"] = {"$gt": int(cursor)}
    try:
        cur = (
            hist_coll.find(filt, {"_id": 0})
            .sort("version", 1)
            .limit(max(int(limit), 1) + 1)
        )
        docs = await cur.to_list(length=None)
    except Exception:
        # If Mongo isn't available, degrade gracefully.
        docs = []

    next_cursor: int | None = None
    if len(docs) > limit:
        next_cursor = int(docs[limit - 1]["version"]) if limit > 0 else None
        docs = docs[:limit]

    versions = [
        {
            "version": int(d.get("version", 0)),
            "doc": d.get("doc", {}),
            "updated_at": d.get("updated_at"),
        }
        for d in docs
    ]
    return {"versions": versions, "nextCursor": next_cursor}


@router.get("/model-config")
async def get_model_config():
    """Return the global model configuration."""
    doc, checksum = await repo.get_model_config()
    if doc is None:
        default = ModelConfig()
        return {"config": default.model_dump(exclude_none=True), "checksum": None}
    return {"config": doc, "checksum": checksum}


@router.put("/model-config")
async def put_model_config(payload: ModelConfig, request: Request):
    """Save the global model configuration."""
    actor = _actor_from(request)
    start = time.perf_counter()
    cfg_dict = payload.model_dump(exclude_none=True)

    await repo.put(repo.MODEL_CONFIG_DOMAIN, repo.MODEL_CONFIG_ENV, cfg_dict)
    _config_write_latency.observe(time.perf_counter() - start)

    logger.info(
        "model config updated",
        extra={"actor": actor, "model": payload.model},
    )
    return {"status": "ok", "config": cfg_dict}


__all__ = ["router"]

