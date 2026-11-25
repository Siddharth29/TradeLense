from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Deque, DefaultDict, Mapping


@dataclass
class AgentMetrics:
    runs: int = 0
    decisions: int = 0
    orders: int = 0
    open_trades: int = 0
    wins: int = 0
    losses: int = 0
    pnl: float = 0.0
    latencies: Deque[float] = field(default_factory=lambda: deque(maxlen=100))
    drift_ms: Deque[float] = field(default_factory=lambda: deque(maxlen=100))
    slot_decisions: Deque[int] = field(default_factory=lambda: deque(maxlen=100))
    budget_skips: int = 0
    last_run: datetime | None = None


_registry: DefaultDict[str, AgentMetrics] = defaultdict(AgentMetrics)


def record_run(asset: str) -> None:
    m = _registry[asset]
    m.runs += 1
    m.slot_decisions.append(0)
    m.last_run = datetime.now(timezone.utc)


def record_decision(asset: str) -> None:
    m = _registry[asset]
    m.decisions += 1
    if m.slot_decisions:
        m.slot_decisions[-1] += 1


def record_order(asset: str, pnl: float, win: bool | None = None) -> None:
    m = _registry[asset]
    m.orders += 1
    m.pnl += pnl
    if win is None:
        if pnl > 0:
            win = True
        elif pnl < 0:
            win = False
    if win:
        m.wins += 1
    elif win is False:
        m.losses += 1


def record_trade_open(asset: str) -> None:
    """Record that a new trade was opened."""
    m = _registry[asset]
    m.orders += 1
    m.open_trades += 1


def record_trade_close(asset: str, pnl: float) -> None:
    """Record a trade closure with its P&L result."""
    m = _registry[asset]
    m.open_trades = max(0, m.open_trades - 1)
    m.pnl += pnl
    if pnl > 0:
        m.wins += 1
    elif pnl < 0:
        m.losses += 1


def record_latency(asset: str, latency_ms: float) -> None:
    _registry[asset].latencies.append(latency_ms)


def record_drift(asset: str, drift_ms: float) -> None:
    _registry[asset].drift_ms.append(drift_ms)


def record_budget_skip(asset: str) -> None:
    _registry[asset].budget_skips += 1


def metrics() -> Mapping[str, AgentMetrics]:
    return _registry


def reset() -> None:
    _registry.clear()


__all__ = [
    "AgentMetrics",
    "record_run",
    "record_decision",
    "record_order",
    "record_trade_open",
    "record_trade_close",
    "record_latency",
    "record_drift",
    "record_budget_skip",
    "metrics",
    "reset",
]
