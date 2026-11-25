"""Central gate for high-volume analytics persistence + durable event logging.

Background
----------
The runtime/trade/cycle documents we used to push into MongoDB one-at-a-time are
high-volume, TTL'd, and (by design) flat snapshots that drop the trade-management
lifecycle (SL-trail history, partial fills, state transitions). They cost write
throughput on the hot path without giving back analysable context.

In *log-only mode* (``PERSIST_ANALYTICS_TO_MONGO`` False) those analytics writes
are skipped. Every record is instead emitted exactly once to the structured JSON
event log (``src.common.logger``) as ``event:<name>`` with the full payload, so
the lifecycle can be reconstructed offline by ``scripts/logs_to_report.py``.

What is gated vs. kept
----------------------
GATED (analytics, log instead): multi_expert_cycles, comprehensive_trades and
their exits, gpt_responses, slot_candle_matches, hourly_candles, strategy
expiries.

NOT gated (operational — keep persisting): monitor_state (crash recovery of a
live position's rules/trail), account_risk equity baseline, the candle cache,
configs/agents/prompts/wallets and broker sessions.

Known coupling risks — why log-only is OPT-IN, not the default
--------------------------------------------------------------
Some "analytics" collections are actually read back by live business logic, so
log-only mode degrades them. Audited in docs/engineering/log-only-persistence-risks.md:
  * comprehensive_trades → packages/core/risk/trade_throttle.py derives the live
    daily trade cap + consecutive-SL cooldown from it. No docs ⇒ both brakes
    never fire (fail-open). This is the blocker for defaulting to log-only.
  * slot_candle_matches → slot_candle_matcher.execute_trade_from_trigger()'s
    Mongo update can't match a never-inserted doc, so it returns False and the
    in-memory _active_matches entry is not cleared until the 24h sweep (benign:
    re-execution is independently guarded by _active_trade / monitoring window).
Until these are made log-aware, keep PERSIST_ANALYTICS_TO_MONGO=True in prod.

recent_trades_memory
--------------------
The live anti-churn ``RECENT_TRADES`` prompt block is fed from the trade-close
path, which today reads the open doc back out of Mongo. In log-only mode there is
no doc to read, so a tiny in-process open-trades registry (populated at
``store_comprehensive_trade``, consumed at ``update_comprehensive_trade_exit``)
recovers the entry context with no DB round-trip and no caller changes.
"""

from __future__ import annotations

import os
import uuid
from threading import Lock
from typing import Any, Dict, Mapping, Optional

from src.common import logger as _event_logger


def analytics_persistence_enabled() -> bool:
    """Return True when high-volume analytics docs should be written to MongoDB.

    The ``PERSIST_ANALYTICS_TO_MONGO`` environment variable overrides the config
    constant, so the posture can be flipped per-deployment without a code change.
    Fails safe to the legacy behaviour (True) if config import is unavailable.
    """
    env = os.getenv("PERSIST_ANALYTICS_TO_MONGO")
    if env is not None and env.strip() != "":
        return env.strip().lower() in {"1", "true", "yes", "y", "on"}
    try:
        from packages.core.config import PERSIST_ANALYTICS_TO_MONGO as _cfg
        return bool(_cfg)
    except Exception:
        return True


def synthetic_id() -> str:
    """Return an in-process id used in log-only mode (no Mongo ``_id`` is made).

    Callers use the returned id only for in-process linking (e.g. a partial leg's
    ``parent_trade_id``); it is also written into the event log so the offline
    reconstructor can stitch parent/child legs.
    """
    return f"logonly_{uuid.uuid4().hex}"


def log_event(event: str, payload: Mapping[str, Any], **corr: Any) -> None:
    """Emit one durable structured event to the JSON log.

    Parameters
    ----------
    event:
        Record type — ``cycle`` | ``trade_open`` | ``trade_exit`` |
        ``gpt_response`` | ``slot_match`` | ``hourly_candle`` |
        ``strategy_expiry``.
    payload:
        The full document we would have stored (the report spec).
    corr:
        Correlation keys (asset / cycle_id / trade_id / position_ticket /
        agent_id …) hoisted into ``ctx`` so logs are trivial to filter and
        stitch into a lifecycle.

    Never raises — logging must not break the trade path.
    """
    try:
        ctx = {k: v for k, v in corr.items() if v is not None}
        _event_logger.info(ctx, f"event:{event}", event=event, payload=dict(payload))
    except Exception:
        pass


# ── In-process open-trades registry (log-only close matching) ────────────────
_open_trades: Dict[str, Dict[str, Any]] = {}
_open_lock = Lock()


def register_open_trade(key: Optional[str], info: Mapping[str, Any]) -> None:
    """Remember an open trade's entry context so the close path can recover it.

    Keyed by ``position_ticket`` (preferred) or the synthetic trade id. Only used
    in log-only mode; bounded by the number of concurrently open trades.
    """
    if not key:
        return
    with _open_lock:
        _open_trades[str(key)] = dict(info)


def pop_open_trade(*keys: Optional[str]) -> Optional[Dict[str, Any]]:
    """Remove and return the first registered open trade matching any of ``keys``."""
    with _open_lock:
        for k in keys:
            if k is None:
                continue
            info = _open_trades.pop(str(k), None)
            if info is not None:
                return info
    return None


__all__ = [
    "analytics_persistence_enabled",
    "synthetic_id",
    "log_event",
    "register_open_trade",
    "pop_open_trade",
]
