from __future__ import annotations

"""Database document models used for MongoDB persistence."""

from datetime import datetime, timezone
from typing import Literal
from bson import ObjectId

from pydantic import BaseModel, Field, ConfigDict, field_validator


class _BaseDoc(BaseModel):
    """Common fields for all documents.

    Every document stored in the database carries a schema ``version`` so that
    migrations can be handled in the future.  Timestamps must always be
    timezone-aware and expressed in UTC.  ``client_order_id`` and ``checksum``
    are application level identifiers used to prevent duplicates and trace the
    origin of records.
    """

    version: Literal[1] = Field(1, description="Schema version")
    timestamp: datetime
    client_order_id: str
    checksum: str

    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("timestamp")
    @classmethod
    def _ensure_utc(cls, v: datetime) -> datetime:
        if v.tzinfo != timezone.utc:
            raise ValueError("timestamp must be UTC")
        return v


class TradeDoc(_BaseDoc):
    """Represents an executed trade."""

    symbol: str
    quantity: float
    price: float
    agent_id: str | None = None
    prompt_hash: str | None = None
    trade_type: Literal["paper", "live"] = Field("live", description="Type of trade - paper or live")
    side: str | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    pattern: str | None = None
    confidence: str | None = None
    broker_type: str | None = None


class OrderDoc(_BaseDoc):
    """Represents an order submitted to a broker."""

    symbol: str
    side: str
    quantity: float
    price: float | None = None
    status: str


class FillDoc(_BaseDoc):
    """Represents a fill for an order."""

    order_id: str
    quantity: float
    price: float


class SignalDoc(_BaseDoc):
    """Represents a trading signal emitted by the strategy."""

    symbol: str
    side: str
    price: float | None = None


class CandleDoc(_BaseDoc):
    """Represents OHLCV candle data."""

    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float


class DailyMetricDoc(_BaseDoc):
    """Represents daily aggregated metrics for the strategy."""

    metric: str
    value: float


class BrokerSessionDoc(BaseModel):
    """Represents a broker authentication session.

    ``access_token`` is used for OAuth-style brokers while ``server``/``login``
    store credentials for session-based brokers like MT5. Only the fields
    relevant to the specific broker will be populated.
    """

    broker: str
    access_token: str | None = None
    server: str | None = None
    login: str | None = None
    expires_at: datetime
    active: bool
    created_at: datetime

    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("expires_at", "created_at")
    @classmethod
    def _ensure_utc(cls, v: datetime) -> datetime:
        if v.tzinfo != timezone.utc:
            raise ValueError("timestamp must be UTC")
        return v


class WalletEquity(BaseModel):
    """Represents equity margin fields in a wallet snapshot."""

    available_margin: float
    used_margin: float
    payin_amount: float


class WalletSnapshotDoc(BaseModel):
    """Represents a broker wallet snapshot."""

    broker: str
    as_of: datetime
    equity: WalletEquity

    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("as_of")
    @classmethod
    def _ensure_utc(cls, v: datetime) -> datetime:
        if v.tzinfo != timezone.utc:
            raise ValueError("timestamp must be UTC")
        return v


class AgentRunDoc(BaseModel):
    """Represents a single execution run of an agent."""
    asset: str
    slot_id: int
    time: datetime

    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("time")
    @classmethod
    def _ensure_utc(cls, v: datetime) -> datetime:
        if v.tzinfo != timezone.utc:
            raise ValueError("time must be UTC")
        return v


class GptResponseDoc(_BaseDoc):
    """Represents a detailed GPT response with all metadata."""
    
    asset: str
    symbol: str  # Additional symbol field for clarity
    agent_id: str
    assistant_id: str
    model: str
    prompt_hash: str
    prompt_content: str
    response_content: str
    response_type: str  # "json", "text", "error"
    latency_ms: float
    tokens_used: int | None = None
    thread_id: str | None = None
    run_id: str | None = None
    error_message: str | None = None
    parsed_decision: dict | None = None
    confidence: float | None = None
    pattern: str | None = None
    direction: str | None = None
    level: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    quantity: float | None = None
    broker_type: str | None = None
    gpt_mode: str | None = None
    
    # New fields for audit and TTL tracking (optional for legacy compatibility)
    ttl_minutes: int | None = None
    active_window_c5: int | None = None
    active_window_c15: int | None = None
    rationale: str | None = None


class TriggerCheckDoc(_BaseDoc):
    """Represents trigger check results and conditions."""
    
    asset: str
    symbol: str  # Additional symbol field for clarity
    agent_id: str
    prompt_hash: str
    gpt_response_id: str  # Reference to GptResponseDoc
    trigger_condition: str
    trigger_type: str  # "retest", "breakout", "fvg_fill", etc.
    level: float | None = None
    direction: str | None = None
    buffer: float | None = None
    trigger_met: bool
    trigger_time: datetime | None = None
    future_candles_count: int | None = None
    entry_price: float | None = None
    confidence: float | None = None
    pattern: str | None = None


class GptCallSummaryDoc(BaseModel):
    """Represents a summary of GPT call for reporting and analysis."""
    
    asset: str
    symbol: str  # Additional symbol field for clarity
    agent_id: str
    timestamp: datetime
    prompt_hash: str
    gpt_response_id: str
    trigger_check_id: str | None = None
    latency_ms: float
    tokens_used: int | None = None
    response_type: str
    trigger_met: bool | None = None
    decision_made: bool
    order_placed: bool
    trade_executed: bool
    confidence: float | None = None
    pattern: str | None = None
    direction: str | None = None
    broker_type: str | None = None
    gpt_mode: str | None = None
    
    # New fields for audit and TTL tracking (optional for legacy compatibility)
    ttl_minutes: int | None = None
    active_window_c5: int | None = None
    active_window_c15: int | None = None
    rationale: str | None = None

    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("timestamp")
    @classmethod
    def _ensure_utc(cls, v: datetime) -> datetime:
        if v.tzinfo != timezone.utc:
            raise ValueError("timestamp must be UTC")
        return v


class ComprehensiveBacktestResultDoc(_BaseDoc):
    """Represents comprehensive backtest results."""
    
    test_name: str
    asset: str
    start_date: datetime
    end_date: datetime
    candle_count_5m: int
    candle_count_15m: int
    
    # Execution metrics
    iterations: int
    total_signals: int
    wins: int
    losses: int
    
    # P&L metrics
    total_pnl: float
    max_drawdown: float
    win_rate: float
    avg_rr: float
    
    # Timestamps
    created_at: datetime
    completed_at: datetime | None = None
    
    # Status and error handling
    status: Literal["pending", "running", "completed", "failed"] = "pending"
    error_message: str | None = None
    
    # Additional results and configuration
    config: dict = Field(default_factory=dict)  # Store full config for reproducibility
    results: dict = Field(default_factory=dict)  # Additional metrics and summary data

    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("created_at", "start_date", "end_date", "completed_at")
    @classmethod
    def _ensure_utc(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo != timezone.utc:
            raise ValueError("timestamp must be UTC")
        return v


class BacktestConfigDoc(BaseModel):
    """Stores backtest configuration for reproducibility."""
    
    test_name: str
    asset: str
    start_date: datetime
    end_date: datetime
    candle_count_5m: int
    candle_count_15m: int
    use_code_sniper: bool
    interval_hours: int
    gpt_config: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("created_at", "start_date", "end_date")
    @classmethod
    def _ensure_utc(cls, v: datetime) -> datetime:
        if v.tzinfo != timezone.utc:
            raise ValueError("timestamp must be UTC")
        return v


class PredictionDoc(BaseModel):
    """Represents a prediction/signal from AI analysis.
    
    Handles field name variations: entry_price/entry/level, stoploss/stop_loss/stop, target/take_profit_1
    Accepts all variants and normalizes them.
    """
    
    direction: str | None = None  # "long" or "short"
    # Accept all entry price variants
    entry_price: float | None = None
    entry: float | None = None
    level: float | None = None
    # Accept all stop loss variants
    stoploss: float | None = None
    stop_loss: float | None = None
    stop: float | None = None
    # Accept all target variants
    target: float | None = None
    take_profit_1: float | None = None
    pattern: str | None = None
    confidence: str | float | None = None  # Can be "high"/"medium"/"low" or numeric
    status: str | None = None  # "waiting", "monitoring", etc.
    
    # Code sniper specific fields
    code_executed: bool | None = None
    triggered_at_candle: int | None = None
    entry_timestamp: datetime | str | None = None
    
    model_config = ConfigDict(extra="allow", populate_by_name=True)  # Allow extra for flexibility
    
    @field_validator("entry_timestamp", mode="before")
    @classmethod
    def _normalize_timestamp(cls, v):
        if v is None:
            return None
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v.replace('Z', '+00:00'))
            except (ValueError, AttributeError):
                return v
        return v


class ExecutionResultDoc(BaseModel):
    """Represents execution result from code sniper or direct execution."""
    
    direction: str | None = None  # "long", "short", "buy", "sell"
    entry: float | None = None
    stop_loss: float | None = None
    take_profit_1: float | None = None
    confidence: float | str | None = None
    rationale: str | None = None
    triggered_candle_time: datetime | str | None = None
    
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    
    @field_validator("triggered_candle_time", mode="before")
    @classmethod
    def _normalize_timestamp(cls, v):
        if v is None:
            return None
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v.replace('Z', '+00:00'))
            except (ValueError, AttributeError):
                return v
        return v


class TradeOutcomeDoc(BaseModel):
    """Represents simulated trade outcome from trade_sim."""
    
    direction: str | None = None
    entry: float | None = None
    stop: float | None = None
    initial_stop: float | None = None
    target: float | None = None
    risk_reward: float | None = None
    entry_time: datetime | str | None = None
    exit_time: datetime | str | None = None
    exit_price: float | None = None
    profit: float = 0.0
    result: Literal["TP", "SL", "BE", "Timeout", "No Entry", "Open"] | str = "No Entry"
    signal_time: datetime | str | None = None
    
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    
    @field_validator("entry_time", "exit_time", "signal_time", mode="before")
    @classmethod
    def _normalize_timestamp(cls, v):
        if v is None:
            return None
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v.replace('Z', '+00:00'))
            except (ValueError, AttributeError):
                return v
        return v


class BacktestIterationDoc(BaseModel):
    """Represents a single backtest iteration result with validated nested structures."""

    backtest_id: str
    iteration: int
    iteration_timestamp: datetime

    # Signal and prediction data (validated)
    signal_data: dict = Field(default_factory=dict)  # Raw signal from multi_expert or code_sniper
    prediction: PredictionDoc | dict = Field(default_factory=dict)  # Validated prediction
    execution_result: ExecutionResultDoc | dict | None = Field(default=None)  # Validated execution result
    trade_outcome: TradeOutcomeDoc | dict = Field(default_factory=dict)  # Validated trade outcome

    # Metrics and metadata
    metrics: dict = Field(default_factory=dict)  # Performance metrics
    cycle_id: str | None = None  # Multi-expert cycle ID for traceability
    cycle_data: dict = Field(default_factory=dict)  # Full cycle doc from multi_expert (experts, judge)
    ai_cost: dict | None = None
    trade_reason: str | None = None

    # Active trade tracking
    active_trade: dict | None = Field(default=None)  # Track active trade state across iterations
    trade_entry_time: datetime | str | None = None  # When trade was entered
    trade_status: Literal["none", "monitoring", "active", "closed"] | str = "none"  # Trade lifecycle status

    # Monitoring state persistence
    monitoring_state: dict | None = Field(default=None)  # State for code monitoring across iterations
    last_gpt_call_time: datetime | str | None = None  # When last GPT call was made
    monitoring_ttl_expiry: datetime | str | None = None  # When monitoring TTL expires

    # Status tracking
    status: Literal["completed", "skipped", "error"] = "completed"
    error: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("timestamp", "iteration_timestamp")
    @classmethod
    def _ensure_utc(cls, v: datetime) -> datetime:
        if v.tzinfo != timezone.utc:
            raise ValueError("timestamp must be UTC")
        return v

    @field_validator("trade_entry_time", "last_gpt_call_time", "monitoring_ttl_expiry", mode="before")
    @classmethod
    def _normalize_optional_timestamps(cls, v):
        """Normalize optional timestamp fields."""
        if v is None:
            return None
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v.replace('Z', '+00:00'))
            except (ValueError, AttributeError):
                return v
        return v

    @field_validator("prediction", mode="before")
    @classmethod
    def _validate_prediction(cls, v):
        """Validate and convert prediction dict to PredictionDoc if possible."""
        if isinstance(v, dict) and v:
            try:
                return PredictionDoc(**v)
            except Exception:
                # If validation fails, return as dict (will be stored as-is)
                return v
        return v or {}
    
    @field_validator("execution_result", mode="before")
    @classmethod
    def _validate_execution_result(cls, v):
        """Validate and convert execution_result dict to ExecutionResultDoc if possible."""
        if v is None:
            return None
        if isinstance(v, dict) and v:
            try:
                return ExecutionResultDoc(**v)
            except Exception:
                # If validation fails, return as dict (will be stored as-is)
                return v
        return v
    
    @field_validator("trade_outcome", mode="before")
    @classmethod
    def _validate_trade_outcome(cls, v):
        """Validate and convert trade_outcome dict to TradeOutcomeDoc if possible."""
        if isinstance(v, dict) and v:
            try:
                return TradeOutcomeDoc(**v)
            except Exception:
                # If validation fails, return as dict (will be stored as-is)
                return v
        return v or {}


class SlotCandleMatchDoc(_BaseDoc):
    """Represents the matching between time slots and candle data for comprehensive tracking."""
    
    asset: str
    slot_id: int
    slot_timestamp: datetime
    gpt_response_id: str
    trigger_check_id: str | None = None
    level_drawn_at: datetime
    level_value: float  # Full precision, no rounding
    direction: str
    trigger_met: bool
    trigger_timestamp: datetime | None = None
    trigger_candle_timestamp: datetime | None = None
    trigger_candle_open: float | None = None
    trigger_candle_high: float | None = None
    trigger_candle_low: float | None = None
    trigger_candle_close: float | None = None
    trigger_candle_volume: float | None = None
    entry_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    confidence: float | None = None
    pattern: str | None = None
    agent_id: str
    prompt_hash: str
    broker_type: str | None = None
    gpt_mode: str | None = None
    trade_executed: bool = False
    trade_id: str | None = None

    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("slot_timestamp", "level_drawn_at", "trigger_timestamp", "trigger_candle_timestamp")
    @classmethod
    def _ensure_utc(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo != timezone.utc:
            raise ValueError("timestamp must be UTC")
        return v


class ComprehensiveTradeDoc(_BaseDoc):
    """Enhanced trade document with comprehensive tracking and full precision."""
    
    symbol: str
    asset: str  # Additional field for clarity
    quantity: float
    price: float  # Full precision, no rounding
    entry_price: float  # Full precision, no rounding
    exit_price: float | None = None  # Full precision, no rounding
    stop_loss: float | None = None  # Full precision, no rounding
    take_profit: float | None = None  # Full precision, no rounding
    agent_id: str | None = None
    prompt_hash: str | None = None
    gpt_response_id: str | None = None
    cycle_id: str | None = None
    slot_candle_match_id: str | None = None
    trade_type: Literal["paper", "live"] = Field("live", description="Type of trade - paper or live")
    side: str | None = None
    pattern: str | None = None
    confidence: str | None = None
    broker_type: str | None = None
    gpt_mode: str | None = None
    signal_received_at: datetime
    trade_executed_at: datetime
    trade_closed_at: datetime | None = None
    pnl: float | None = None
    fees: float | None = None
    exit_fees: float | None = None  # Fees paid on exit
    slippage: float | None = None
    trade_status: Literal["open", "closed", "cancelled"] = "open"
    exit_reason: str | None = None  # "TP", "SL", "manual", etc.
    # Broker position ticket (MT5: position id == opening order ticket). Lets
    # exit updates match the exact position instead of "latest open doc for
    # symbol" (root cause of cross-contaminated exit rows).
    position_ticket: str | None = None
    # For partial-exit legs: Mongo _id of the parent decision trade doc, so
    # per-decision P&L (parent + partials) is computable.
    parent_trade_id: str | None = None
    trade_reason: str | None = None
    trigger_reason: str | None = None
    ai_cost: dict | None = None
    broker_cost: dict | None = None

    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("signal_received_at", "trade_executed_at", "trade_closed_at")
    @classmethod
    def _ensure_utc(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo != timezone.utc:
            raise ValueError("timestamp must be UTC")
        return v


class HourlyCandleDoc(_BaseDoc):
    """Represents 1-hour candle data with comprehensive metadata."""
    
    asset: str
    symbol: str
    slot_id: int
    slot_timestamp: datetime
    open: float  # Full precision, no rounding
    high: float  # Full precision, no rounding
    low: float  # Full precision, no rounding
    close: float  # Full precision, no rounding
    volume: float | None = None
    candle_timestamp: datetime
    candle_interval: Literal["1h"] = "1h"
    market_session: str | None = None  # "pre_market", "regular", "post_market"
    gpt_signals_count: int = 0
    triggers_met_count: int = 0
    trades_executed_count: int = 0

    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("slot_timestamp", "candle_timestamp")
    @classmethod
    def _ensure_utc(cls, v: datetime) -> datetime:
        if v.tzinfo != timezone.utc:
            raise ValueError("timestamp must be UTC")
        return v


class ExpertInsight(BaseModel):
    """Represents insights and validation results from a single expert."""
    
    name: Literal['trend', 'breakout', 'reversal']
    market_analysis: dict  # Raw market analysis from expert
    proposal: dict | None = None  # Structured proposal if any
    validation: dict  # Validation results including rr_check, anti_scalp, window, etc.
    passed: bool
    error: str | None = None
    duration_ms: float | None = None  # Changed from int to float for precision
    
    model_config = ConfigDict(extra="forbid", strict=True)


class JudgeOutcome(BaseModel):
    """Represents the judge's decision and rationale."""
    
    selected: str | None = None  # Expert name or 'flat'
    rationale: str | None = None
    final_signal: dict | None = None  # Normalized decision or flat
    validation_summary: dict
    
    model_config = ConfigDict(extra="forbid", strict=True)


class MultiExpertCycleDoc(BaseModel):
    """Represents a complete multi-expert decision cycle with detailed tracking."""
    
    _id: ObjectId | None = None
    cycle_id: str  # Unique identifier for this cycle
    mode: Literal['multi_expert', 'normal', 'multi_expert_code_sniper']
    agent_id: str
    asset: str
    symbol: str
    started_at: datetime
    completed_at: datetime | None = None
    experts: list[ExpertInsight] = []
    judge: JudgeOutcome | None = None
    status: Literal['success', 'failed', 'skipped', 'flat', 'completed'] = 'skipped'
    error: str | None = None
    version: int = 1
    ai_cost: dict | None = None
    
    model_config = ConfigDict(extra="allow", strict=True)
    
    @field_validator("started_at", "completed_at")
    @classmethod
    def _ensure_utc(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo != timezone.utc:
            # Convert to UTC if timezone is present, otherwise assume UTC
            if v.tzinfo is not None:
                return v.astimezone(timezone.utc)
            else:
                # Assume naive datetime is UTC
                return v.replace(tzinfo=timezone.utc)
        return v


__all__ = [
    "TradeDoc",
    "OrderDoc",
    "FillDoc",
    "SignalDoc",
    "CandleDoc",
    "DailyMetricDoc",
    "BrokerSessionDoc",
    "WalletSnapshotDoc",
    "AgentRunDoc",
    "GptResponseDoc",
    "TriggerCheckDoc",
    "GptCallSummaryDoc",
    "SlotCandleMatchDoc",
    "ComprehensiveTradeDoc",
    "HourlyCandleDoc",
    "ExpertInsight",
    "JudgeOutcome",
    "MultiExpertCycleDoc",
    "ComprehensiveBacktestResultDoc",
    "BacktestConfigDoc",
    "BacktestIterationDoc",
    "PredictionDoc",
    "ExecutionResultDoc",
    "TradeOutcomeDoc",
]
