"""Type definitions for backtest modules."""

from __future__ import annotations

from typing import TypedDict, Optional
from datetime import datetime
import pandas as pd


# ── Trade result vocabulary (2026-08-07) ──────────────────────────────────
# There was no single list, so producers and consumers disagreed. Measured
# consequence: ``simulate_trade`` emitted "Time Exit (Win)" / "Time Exit (Loss)"
# for every monitor-expiry close, and the summary endpoint's substring classifier
# ("TP" in s / "SL" in s / "BE" in s / "TIMEOUT" in s) matched NONE of them, so
# those trades fell into "other" — excluded from win/loss counts, win rate,
# profit factor, expectancy and the go-live scorecard, while their P&L still
# landed in total_pnl. "Time Exit (BE)" was worse: it matched "BE" and was
# counted as a break-even.
#
# Producers now emit only the labels below and consumers classify with
# ``result_type`` — an exact lookup, so a new label fails loudly instead of
# silently binning itself as "other".
RESULT_TP = "TP"                 # final target reached (runner disabled)
RESULT_SL = "SL"                 # initial stop hit
RESULT_BE = "BE"                 # stop hit after it moved to BE+0.5R
RESULT_TRAIL = "Trail"           # post-TP2 runner stopped out on the chandelier
RESULT_TIMEOUT = "Timeout"       # monitor expiry / forced close at market
RESULT_OPEN = "Open"             # still being monitored
RESULT_NO_ENTRY = "No Entry"     # never triggered
RESULT_FILTERED = "Filtered"     # rejected by a guard before entry

#: Labels that represent a CLOSED position whose P&L must be booked.
CLOSED_RESULTS: frozenset[str] = frozenset(
    {RESULT_TP, RESULT_SL, RESULT_BE, RESULT_TRAIL, RESULT_TIMEOUT}
)

#: result label -> bucket used by reporting. Trailed runner exits bucket with TP
#: because that is what they are: a position that reached TP2 and then gave back
#: part of the runner. Timeouts keep their own bucket; whether one is a win is
#: decided by its P&L, not its label.
_RESULT_BUCKETS: dict[str, str] = {
    RESULT_TP: "TP",
    RESULT_TRAIL: "TP",
    RESULT_SL: "SL",
    RESULT_BE: "BE",
    RESULT_TIMEOUT: "Timeout",
}


def result_type(result: Optional[str]) -> str:
    """Bucket a trade result label. Unknown / open / skipped -> ``"other"``."""
    return _RESULT_BUCKETS.get((result or "").strip(), "other")


class PredictionDict(TypedDict, total=False):
    """Type definition for prediction dictionary."""
    direction: str | None
    entry: float | None
    stop: float | None
    target: float | None
    pattern: str | None
    condition: str | None
    level: float | None
    entry_price: float | None
    stoploss: float | None
    stop_loss: float | None
    confidence: float | None


class OutcomeDict(TypedDict, total=False):
    """Type definition for trade outcome dictionary."""
    trade_reason: str | None
    trigger_reason: str | None
    ai_cost: dict | None
    broker_cost: dict | None
    direction: str | None
    entry: float | None
    stop: float | None
    initial_stop: float | None
    target: float | None
    tp1_points: float
    risk_reward: float | None
    entry_time: datetime | pd.Timestamp | None
    exit_time: datetime | pd.Timestamp | None
    exit_price: float | None
    profit: float
    result: str
    signal_time: datetime | pd.Timestamp | None


class IterationDict(TypedDict, total=False):
    """Type definition for iteration dictionary."""
    iteration: int
    timestamp: str
    status: str
    prediction: PredictionDict | dict
    outcome: OutcomeDict | dict
    error: str | None
    monitoring_active_trade: bool
    entry_iteration: int | None
    active_code_monitoring: dict | None
    execution_result: dict | None


class BacktestEventDict(TypedDict, total=False):
    """Type definition for backtest event dictionary."""
    type: str
    message: str | None
    candle: dict | None
    outcome: OutcomeDict | dict | None

