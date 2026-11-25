from __future__ import annotations

"""In-memory trade lifecycle state and event store."""

from datetime import datetime, timezone
from typing import Any, Dict

from packages.core.trade_lifecycle import (
    InMemoryTradeRepository,
    TradeLifecycle,
    TradeState,
)

repo = InMemoryTradeRepository()
lifecycle = TradeLifecycle(repo)

# Mapping of symbol -> current trade id
open_trades: Dict[str, str] = {}

# Append-only list of lifecycle events
# Each entry: {asset, trade_id, type, timestamp, agent_id, prompt_hash}
events: list[dict[str, Any]] = []


def record_event(
    asset: str,
    trade_id: str,
    state: TradeState,
    agent_id: str | None,
    prompt_hash: str | None,
) -> None:
    events.append(
        {
            "asset": asset,
            "trade_id": trade_id,
            "type": state.value,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent_id": agent_id,
            "prompt_hash": prompt_hash,
        }
    )
