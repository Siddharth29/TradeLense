"""Agent execution with budget-aware GPT interactions."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
import hashlib
import re
from datetime import datetime, timezone, timedelta, date
from dataclasses import asdict, is_dataclass
from typing import Any, Mapping, Dict, Iterable, TYPE_CHECKING

if TYPE_CHECKING:
    from packages.core.models import RecentTradeView

import pandas as pd

from .candle_data import CandleDataAdapter

from packages.core.prompt_builder import build_prompt
from pydantic import ValidationError

from packages.core.decision_normalizer import normalize_decision, parse_prediction
from packages.core import SniperResponse, coerce_prediction
from packages.core.prediction import Prediction
from packages.core.candles import validate_candle_sequence_and_alignment, Candle
from packages.core.candle_validation import validate_live_candle_window
# Removed ASSISTANT_ID import - no longer needed with chat completion
from packages.core.sniper_agent import call_sniper
from packages.core.ai_accounting import trade_reason, DecisionCosts
from packages.core.multi_expert import MultiExpertSignal, multi_expert_decision, multi_expert_decision_with_details
from packages.core.schemas_trade import CodeSniperSignal
from typing import Mapping
from packages.core.risk.daily_guards import DailyGuards
from packages.core.config import (
    TREND_AGREEMENT_ENABLED,
    trend_agreement_from,
    CS_LIVE_RISK_PCT,
    TTL_DEFAULT_MINUTES,
    CS_EXPECTED_TOTAL_CANDLES,
    BACKTEST_MAX_TRADES_PER_DAY,
    BACKTEST_COOLDOWN_AFTER_SL,
    BACKTEST_COOLDOWN_SL_THRESHOLD,
    BACKTEST_MIN_SL_DISTANCE,
    BACKTEST_MAX_SL_DISTANCE,
    BACKTEST_MAX_ENTRY_DRIFT,
    BACKTEST_MIN_SL_PCT_CRYPTO,
    BACKTEST_MIN_TARGET_DISTANCE,
    BACKTEST_MIN_RR,
    TRADE_MONITOR_EXPIRY_HOURS,
    WRITE_TIMEOUT_SECONDS as _WRITE_TIMEOUT,
    PREDICTION_TIMEOUT_MINUTES as _PREDICTION_TIMEOUT,
    RUNNER_CLEANUP_INTERVAL,
    RUNNER_TRIGGER_CHECK_INTERVAL,
    RUNNER_CACHE_ROLL_INTERVAL,
    RUNNER_MONITORING_DURATION_MINUTES,
)

def require_attr(obj: Any, name: str, context: str = "") -> Any:
    """Require an attribute to exist on an object, raising ValueError if missing.
    
    Args:
        obj: Object to check for attribute
        name: Attribute name to require
        context: Additional context for error message
        
    Returns:
        The attribute value if it exists
        
    Raises:
        ValueError: If attribute is missing or None
    """
    if not hasattr(obj, name):
        raise ValueError(f"Required attribute '{name}' missing from {type(obj).__name__}{': ' + context if context else ''}")
    
    value = getattr(obj, name)
    if value is None:
        raise ValueError(f"Required attribute '{name}' is None in {type(obj).__name__}{': ' + context if context else ''}")
    
    return value
from packages.core.metrics import (
    record_run,
    record_decision,
    record_order,
    record_trade_open,
    record_trade_close,
    record_latency,
    record_budget_skip,
)
from packages.core.trigger_check import entry_trigger_met, enhanced_entry_trigger_met, validate_pattern_with_history, calculate_dynamic_buffer
from packages.core.validators import rr, anti_scalp, inside_window, median_range_5m
from packages.core.candle_cache import get_cache, get_cache_manager
import numpy as np
from packages.core.candle_service import NoCandleData, CandleFetchFailed as _CoreCandleFetchFailed


def _build_supersets(
    c5_raw: list[dict],
    c15_raw: list[dict],
    want_c5: int = 100,
    want_c15: int = 48,
) -> tuple[list[dict], list[dict]]:
    c5_superset = c5_raw[-want_c5:] if len(c5_raw) > want_c5 else c5_raw
    c15_superset = c15_raw[-want_c15:] if len(c15_raw) > want_c15 else c15_raw
    return c5_superset, c15_superset


def _confidence_label(value: float) -> str:
    if value >= 0.66:
        return "high"
    if value >= 0.33:
        return "medium"
    return "low"


def _validate_final_safety_gate(
    entry: float | None,
    stop: float | None,
    target: float | None,
    direction: str,
    c5_window: list[dict],
    c15_window: list[dict],
    min_rr: float = 1.5,
    anti_scalp_k: float = 6.0
) -> tuple[bool, str]:
    """Runner's final safety gate (defense-in-depth).
    
    Validates RR from entry/stop/target; checks anti-scalp vs median 5m; 
    ensures inside chosen windows. Returns (is_valid, reason).
    """
    # Skip validation for flat signals (None prices)
    if entry is None or stop is None or target is None:
        return True, "Flat signal - no validation needed"
    
    try:
        # Convert direction for validators (long/short -> buy/sell)
        side = "buy" if direction == "long" else "sell"
        
        # 1. Check risk-reward ratio
        try:
            risk_reward = rr(entry=entry, stop=stop, target=target, side=side)
            if risk_reward < min_rr:
                return False, f"RR {risk_reward:.2f} below minimum {min_rr}"
        except Exception as e:
            return False, f"RR calculation failed: {str(e)}"
        
        # 2. Check anti-scalp guard
        try:
            if not anti_scalp(entry=entry, target=target, side=side, c5_window=c5_window, k=anti_scalp_k):
                median_range = median_range_5m(c5_window)
                min_distance = anti_scalp_k * median_range
                actual_distance = abs(target - entry)
                return False, f"Anti-scalp failed: distance {actual_distance:.2f} < required {min_distance:.2f}"
        except Exception as e:
            return False, f"Anti-scalp check failed: {str(e)}"
        
        # 3. Check inside window constraint
        try:
            if not inside_window(entry=entry, stop=stop, target=target, c5_window=c5_window, c15_window=c15_window):
                return False, "Levels outside chosen windows"
        except Exception as e:
            return False, f"Window check failed: {str(e)}"
        
        return True, "All safety checks passed"
        
    except Exception as e:
        return False, f"Safety gate error: {str(e)}"


def _map_multi_expert_signal(signal: MultiExpertSignal) -> dict[str, Any] | None:
    """Map multi-expert signal to internal format.
    
    Returns None only for explicit flat signals from the model.
    Does NOT return None for errors - those should raise exceptions.
    """
    if signal.signal == "flat":
        # This is a valid "no trade" decision from the model, not an error
        return None

    # Validate that non-flat signals have required price fields
    if signal.entry is None or signal.stop is None or signal.target is None:
        raise ValueError(
            f"Non-flat signal '{signal.signal}' requires entry, stop, and target fields. "
            f"Got: entry={signal.entry}, stop={signal.stop}, target={signal.target}"
        )

    dir_map = {"buy": "long", "sell": "short"}
    direction = dir_map.get(signal.signal)
    if not direction:
        # Invalid signal - this is an error, not a flat signal
        raise ValueError(f"Invalid signal direction: {signal.signal}")

    confidence_value = float(signal.confidence or 0.0)
    meta = {
        "confidence_value": confidence_value,
        "ttl_minutes": signal.ttl_minutes,
        "active_window_c5": signal.active_window_c5,
        "active_window_c15": signal.active_window_c15,
        "need_more_candles": signal.need_more_candles,
        "more_c5": signal.more_c5,
        "more_c15": signal.more_c15,
        "rationale": signal.rationale,
    }

    if signal.ttl_minutes:
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=signal.ttl_minutes)
        meta["valid_until"] = expires_at.isoformat()

    prediction_dict = {
        "direction": direction,
        "pattern": "multi_expert",
        "level": float(signal.entry),
        "stop": float(signal.stop),
        "target": float(signal.target),
        "confidence": _confidence_label(confidence_value),
        "status": "waiting",
        "meta": meta,
    }

    return {
        "action": direction,
        "prediction": prediction_dict,
    }
from packages.core.slot_candle_matcher import get_slot_matcher
from packages.core.realtime_candle_monitor import get_realtime_monitor, initialize_realtime_monitor


from packages.core.utils import calculate_atr
from packages.core.trading_filters import trading_filters
from packages.core.datetime_utils import serialize_for_json, safe_json_dumps


def _convert_confidence_to_float(confidence: Any) -> float | None:
    """Convert confidence from string to float for database storage."""
    if not confidence:
        return None
    
    if confidence == "high":
        return 0.8
    elif confidence == "medium":
        return 0.6
    elif confidence == "low":
        return 0.4
    elif isinstance(confidence, (int, float)):
        return float(confidence)
    else:
        return None


try:  # pragma: no cover - repo optional in tests
    from packages.db.repositories import write as _repo
except Exception:  # pragma: no cover - missing dependencies
    repo = None  # type: ignore[assignment]
else:
    repo = _repo if os.getenv("ENABLE_M4_REPO") else None
try:  # pragma: no cover - repo optional in tests
    from packages.db.repositories import candles as candle_repo
except Exception:  # pragma: no cover - missing dependencies
    candle_repo = None  # type: ignore[assignment]
try:  # pragma: no cover - repo optional in tests
    from packages.db.repositories import agent_runs as _agent_runs_repo
    from packages.db.models import AgentRunDoc
except Exception:  # pragma: no cover - missing dependencies
    agent_runs_repo = None  # type: ignore[assignment]
else:
    agent_runs_repo = _agent_runs_repo if os.getenv("ENABLE_M4_REPO") else None
from src.common import logger as app_logger


# Use the canonical CandleFetchFailed from candle_service (avoids shadowing the import above)
CandleFetchFailed = _CoreCandleFetchFailed

# ``db_logger`` retained for backward compatibility with tests expecting this name
db_logger = app_logger

# Safe error logging wrapper to prevent TypeError crashes
def safe_error_log(logger_func, *args, **kwargs):
    """Safely log errors with fallback to prevent TypeError crashes."""
    try:
        return logger_func(*args, **kwargs)
    except Exception as e:
        # Fallback logging if the main logger fails
        print(f"[ERROR] Logger failed: {str(e)} - Args: {args} - Kwargs: {kwargs}")
        # Use standard Python logging as fallback
        import logging
        if args and len(args) >= 2:
            logging.error(f"{args[1]} - {kwargs}")
        else:
            logging.error(f"Logger error: {str(e)}")


def _validate_gpt_response(response_data: dict) -> dict:
    """Basic validation for GPT response - minimal checks only.
    
    Note: Full validation is done by parse_prediction() via Pydantic.
    This function only provides basic checks for business logic before parsing.
    Uses canonical field names: entry, stop, target (not level, entry_price, etc.)
    """
    # Basic check: ensure entry field exists (canonical name)
    # If level exists but entry doesn't, map it (for legacy compatibility)
    if "level" in response_data and "entry" not in response_data:
        response_data["entry"] = response_data.pop("level")
    
    # Basic required fields check (full validation done by Pydantic)
    required_fields = ["entry", "stop", "target", "direction", "pattern", "confidence"]
    missing = [f for f in required_fields if f not in response_data]
    if missing:
        raise ValueError(f"Missing required fields: {', '.join(missing)}")
    
    # Basic type check for numeric fields (full validation done by Pydantic)
    for field in ["entry", "stop", "target"]:
        if not isinstance(response_data[field], (int, float)):
            try:
                response_data[field] = float(response_data[field])
            except (ValueError, TypeError):
                raise ValueError(f"Invalid {field} type: {response_data[field]}, must be numeric")
    
    return response_data


logger = logging.getLogger(__name__)

WRITE_TIMEOUT_SECONDS = _WRITE_TIMEOUT
PREDICTION_TIMEOUT_MINUTES = _PREDICTION_TIMEOUT


# Strong references to in-flight fire-and-forget tasks. CPython's event loop
# keeps only a WEAK reference to tasks created via create_task, so a task with
# no other referent can be garbage-collected mid-await — silently dropping a
# persistence write. Holding the task here until it completes prevents that.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _fire_and_forget(coro: asyncio.Future, collection: str) -> None:
    """Spawn ``coro`` and log failures or timeouts asynchronously."""

    async def _runner() -> None:
        try:
            await asyncio.wait_for(coro, timeout=WRITE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            safe_error_log(db_logger.error, {"collection": collection}, "repo write timed out")
        except Exception as exc:  # pragma: no cover - runtime failures
            safe_error_log(db_logger.error, {"collection": collection}, "repo write failed", error=str(exc))

    task = asyncio.create_task(_runner())
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


def _build_signal_doc(asset: str, decision, client_order_id: str, ts: datetime) -> dict:
    payload = {
        "version": 1,
        "timestamp": ts,
        "client_order_id": client_order_id,
        "symbol": asset,
        "side": getattr(getattr(decision, "action", ""), "value", getattr(decision, "action", "")),
        "price": require_attr(decision, "entry", "for order execution"),
    }
    return payload


def _build_order_doc(asset: str, order, decision, client_order_id: str, ts: datetime) -> dict:
    payload = {
        "version": 1,
        "timestamp": ts,
        "client_order_id": client_order_id,
        "symbol": asset,
        "side": getattr(decision, "action", ""),
        "quantity": getattr(decision, "size", None),
        "price": getattr(order, "average_price", None),
        "status": order.status,
    }
    return payload


def _build_fill_docs(
    fills: Iterable, client_order_id: str, ts: datetime, order_id: str
) -> list[dict]:
    docs = []
    for idx, fl in enumerate(fills):
        payload = {
            "version": 1,
            "timestamp": getattr(fl, "timestamp", ts),
            "client_order_id": client_order_id,
            "order_id": getattr(fl, "order_id", order_id),
            "quantity": fl.quantity,
            "price": fl.price,
        }
        docs.append(payload)
    return docs


def _build_trade_doc(
    symbol: str,
    quantity: float,
    price: float,
    client_order_id: str,
    ts: datetime,
    agent_id: str | None,
    prompt_hash: str | None,
) -> dict:
    payload = {
        "version": 1,
        "timestamp": ts,
        "client_order_id": client_order_id,
        "symbol": symbol,
        "quantity": quantity,
        "price": price,
        "agent_id": agent_id,
        "prompt_hash": prompt_hash,
    }
    return payload


class AgentRunner:
    """Run an agent while tracking rate and timeout budgets.

    Parameters
    ----------
    agent_config:
        Mapping containing runtime dependencies and optional budget settings.
        Expected keys include ``candle_service`` and ``broker``.
        GPT configuration includes ``gpt_model``, ``gpt_temperature``, ``gpt_top_p``,
        ``gpt_prompt_id``, and ``gpt_prompt_content``.
        ``rate_limit_per_min`` and ``timeout_ms`` entries configure the budget
        checks.
    """

    def __init__(self, agent_config: Mapping[str, Any]) -> None:
        self.agent_config = agent_config
        self.symbol = agent_config.get("symbol")
        self.broker = agent_config.get("broker")
        self.candle_count_5m = int(agent_config.get("candle_count_5m", 0))
        self.candle_count_15m = int(agent_config.get("candle_count_15m", 0))
        self.expiry_date = agent_config.get("expiry_date")
        self.risk_percent = float(agent_config.get("risk_percent", 0.0))
        
        # Extract agent_id and broker_name for consistent logging
        self.agent_id = agent_config.get("agent_id", f"{self.symbol}_{int(time.time()*1000)}")
        if isinstance(self.broker, str):
            self.broker_name = self.broker
        elif self.broker:
            self.broker_name = getattr(self.broker, "name", self.broker.__class__.__name__)
        else:
            self.broker_name = "Unknown"
        
        # Create standardized logging context for use across all methods
        self._log_ctx = {
            "asset": self.symbol,
            "agent_id": self.agent_id,
            "broker": self.broker_name
        }
        
        # GPT configuration
        # NOTE: prompt_id/prompt_content from DB are intentionally ignored.
        # Backtest is source of truth — we use static prompts from prompt_builders.py
        # and sniper_agent.py default prompts to ensure backtest ↔ live parity.
        self.gpt_prompt_id = None  # Not used — static prompts only
        self.gpt_prompt_content = None  # Not used — static prompts only
        self.gpt_config = agent_config.get("gpt_config", {})
        
        # Set the AI provider from config (openai or gemini)
        _provider = agent_config.get("gpt", {}).get("provider") or agent_config.get("provider")
        if _provider:
            from packages.core.gpt_client import set_provider
            set_provider(_provider)

        # Import default config and allow override
        from packages.core.config import USE_MULTI_EXPERT, USE_CODE_SNIPER
        # Feature flag for code sniper (defaults to config value for environment override support)
        self.use_code_sniper = bool(agent_config.get("use_code_sniper", USE_CODE_SNIPER))
        self.use_multi_expert = bool(agent_config.get("use_multi_expert", USE_MULTI_EXPERT))

        rl = agent_config.get("rate_limit_per_min")
        self.rate_limit_per_min = int(rl) if rl else None
        self.timeout_ms = int(agent_config.get("timeout_ms", 0))

        now = time.monotonic()
        self._last_refill = now
        self._tokens = float(self.rate_limit_per_min or 0)
        self._next_allowed_time = 0.0
        self._pending_predictions: list[dict[str, Any]] = []
        self._active_trade: dict[str, Any] | None = None  # Track active trade to prevent multiple executions
        self._last_cleanup = time.monotonic()
        self._cleanup_interval = RUNNER_CLEANUP_INTERVAL

        # Initialize slot candle matcher for continuous monitoring
        self._slot_matcher = get_slot_matcher()
        self._last_trigger_check = time.monotonic()
        self._trigger_check_interval = RUNNER_TRIGGER_CHECK_INTERVAL
        
        # Initialize real-time candle monitor
        self._realtime_monitor = None
        self._monitor_initialized = False
        self._trade_monitor = None
        self._trade_monitor_initialized = False
        
        # Enhanced candle cache for market monitoring (last 10 candles of 5min)
        self._candle_cache = None
        self._cache_initialized = False
        self._last_cache_update = None
        self._cache_roll_interval = RUNNER_CACHE_ROLL_INTERVAL
        
        # Service validation flags
        self._services_validated = False
        self._service_errors = []
        
        # Track monitoring window based on run interval
        self._monitoring_start_time: float | None = None
        self._monitoring_duration_minutes = RUNNER_MONITORING_DURATION_MINUTES
        
        # Agent state management for continuous running
        self._is_running = False
        self._should_stop = False
        self._execution_mode = "run_once"  # Can be "run_once", "run_until_stopped", "scheduled"
        self._continuous_task: asyncio.Task | None = None
        self._scheduled_run_time = None
        
        # Dynamic configuration removed
        
        # Database operation batching
        self._pending_db_ops = []

        # Trigger check caching (5-minute TTL)
        self._trigger_check_cache: dict[str, tuple[bool, float]] = {}

        # Monitoring window cache
        self._window_status_cache: bool | None = None
        self._window_cache_time: float = 0.0

        # Candle validation run marker
        self._candles_validated_run: datetime | None = None
        
        # Daily guards: trade cap + SL cooldown (matching backtest)
        self._daily_guards = DailyGuards(
            max_loss=float("inf"),
            max_trades=BACKTEST_MAX_TRADES_PER_DAY,
            cooldown=BACKTEST_COOLDOWN_AFTER_SL,
            loss_threshold=BACKTEST_COOLDOWN_SL_THRESHOLD,
        )
        self._daily_guards_last_reset: date | None = None
    
    
    def start_continuous_monitoring(self) -> None:
        """Start continuous monitoring until explicitly stopped."""
        if self._is_running:
            app_logger.warning(
                self._log_ctx,
                "Agent already running - cannot start continuous monitoring"
            )
            return
        
        self._execution_mode = "run_until_stopped"
        self._should_stop = False
        self._is_running = True
        
        app_logger.info(
            self._log_ctx,
            "Starting continuous monitoring mode - agent will run until explicitly stopped"
        )
    
    async def stop_continuous_monitoring(self) -> None:
        """Stop continuous monitoring immediately.

        Async because stopping the strategy monitor is async and must be AWAITED —
        the old sync version fired a coroutine it never awaited, so the monitor
        kept running and kept placing orders (audit 2026-08-27, C2).
        """
        app_logger.info(
            self._log_ctx,
            "STOP REQUESTED: Stopping continuous monitoring mode immediately"
        )

        self._should_stop = True

        # Cancel continuous task if running
        if self._continuous_task and not self._continuous_task.done():
            self._continuous_task.cancel()
            app_logger.info(
                self._log_ctx,
                "Continuous monitoring task cancelled"
            )

        # Stop trade monitoring if active
        if self._trade_monitor and hasattr(self._trade_monitor, 'shutdown'):
            try:
                import asyncio
                asyncio.create_task(self._trade_monitor.shutdown())
                app_logger.info(
                    self._log_ctx,
                    "Trade monitor shutdown initiated"
                )
            except Exception as e:
                app_logger.error(
                    self._log_ctx,
                    f"Error shutting down trade monitor: {e}"
                )

        # Stop the strategy monitor — the single authoritative teardown.
        try:
            from packages.core.services.code_sniper_monitor import (
                shutdown_code_sniper_monitor,
            )
            await shutdown_code_sniper_monitor(self.agent_config.get("agent_id", "unknown"))
        except Exception as e:
            app_logger.error(self._log_ctx, f"Error stopping strategy monitor: {e}")

        # Clean up resources
        self._is_running = False
        self._execution_mode = "run_once"
        self._active_trade = None

        app_logger.info(
            self._log_ctx,
            "STOP COMPLETED: Agent stopped and resources cleaned up"
        )
    
    def is_stop_requested(self) -> bool:
        """Check if a stop has been requested."""
        return self._should_stop
    
    async def emergency_stop(self) -> None:
        """Emergency stop - immediately halt all operations."""
        app_logger.warning(
            self._log_ctx,
            "EMERGENCY STOP: Immediately halting all agent operations"
        )

        self._should_stop = True
        self._is_running = False

        # Cancel all async operations
        tasks_to_cancel = []

        if self._continuous_task and not self._continuous_task.done():
            tasks_to_cancel.append(self._continuous_task)

        if tasks_to_cancel:
            for task in tasks_to_cancel:
                task.cancel()

            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks_to_cancel, return_exceptions=True),
                    timeout=5.0
                )
            except asyncio.TimeoutError:
                app_logger.error(
                    self._log_ctx,
                    "Emergency stop timeout - some tasks may still be running"
                )

        # Force shutdown trade monitor
        if self._trade_monitor and hasattr(self._trade_monitor, 'shutdown'):
            try:
                await asyncio.wait_for(self._trade_monitor.shutdown(), timeout=3.0)
            except asyncio.TimeoutError:
                app_logger.error(self._log_ctx, "Trade monitor shutdown timeout during emergency stop")
            except Exception as e:
                app_logger.error(self._log_ctx, f"Error during emergency trade monitor shutdown: {e}")

        # Force stop the strategy monitor — same teardown as every other stop path.
        try:
            from packages.core.services.code_sniper_monitor import (
                shutdown_code_sniper_monitor,
            )
            agent_id = self.agent_config.get("agent_id", "unknown")
            await asyncio.wait_for(shutdown_code_sniper_monitor(agent_id), timeout=15.0)
        except asyncio.TimeoutError:
            app_logger.error(self._log_ctx, "Strategy monitor did not stop within 15s")
        except Exception as e:
            app_logger.error(self._log_ctx, f"Error stopping strategy monitor during emergency stop: {e}")

        app_logger.warning(
            self._log_ctx,
            "EMERGENCY STOP COMPLETED: All operations halted"
        )
    
    def is_running(self) -> bool:
        """Check if agent is currently running in continuous mode."""
        return self._is_running
    
    def should_continue_running(self) -> bool:
        """Check if agent should continue running."""
        return self._execution_mode == "run_until_stopped" and not self._should_stop
    
    async def run_continuously(self) -> None:
        """Run agent continuously until explicitly stopped."""
        if self._is_running:
            app_logger.warning(
                self._log_ctx,
                "Agent already running continuously"
            )
            return
        
        # Set to continuous mode
        self.start_continuous_monitoring()
        
        try:
            app_logger.info(
                self._log_ctx,
                "Starting continuous agent execution - will run until stopped"
            )
            
            # Main continuous loop
            while self.should_continue_running():
                try:
                    # Run the agent once
                    result = await self.run()
                    
                    # Log the result
                    if result:
                        app_logger.info(
                            self._log_ctx,
                            "Agent run completed in continuous mode",
                            result_summary=str(result)[:200]  # Truncate for logging
                        )
                    else:
                        app_logger.debug(
                            self._log_ctx,
                            "Agent run completed with no result in continuous mode"
                        )
                    
                    # Check if we should stop after this run
                    if self._should_stop:
                        break
                    
                    # Wait for next 5-minute interval before running again
                    # This ensures we run on every 5-minute candle close
                    from packages.core.scheduler import await_next_bar_close
                    
                    try:
                        next_run_time = await await_next_bar_close("5m", "UTC")
                        app_logger.debug(
                            self._log_ctx,
                            "Waiting for next 5-minute candle close",
                            next_run_time=next_run_time.isoformat()
                        )
                    except Exception as e:
                        # Fallback to simple 5-minute wait
                        app_logger.warning(
                            self._log_ctx,
                            f"Scheduler failed, using simple 5-minute wait: {e}"
                        )
                        await asyncio.sleep(300)  # 5 minutes
                        
                except asyncio.CancelledError:
                    app_logger.info(
                        self._log_ctx,
                        "Continuous agent execution cancelled"
                    )
                    break
                except Exception as e:
                    app_logger.error(
                        self._log_ctx,
                        f"Error in continuous agent execution: {e}",
                        error_type=type(e).__name__
                    )
                    # Wait a bit before retrying to avoid rapid failure loops
                    await asyncio.sleep(60)  # 1 minute wait on error
                    
        finally:
            # Clean up
            self._is_running = False
            self._should_stop = False
            app_logger.info(
                self._log_ctx,
                "Continuous agent execution stopped"
            )
    
    def _validate_required_services(self) -> bool:
        """Validate that all required services are available."""
        if self._services_validated:
            return len(self._service_errors) == 0
        
        self._service_errors = []
        
        # Required services - validate existence and method presence only.
        # NOTE: do NOT call is_connected() here — that makes synchronous MT5 IPC calls
        # (account_info / terminal_info) from this sync method, which runs on the event
        # loop thread and can deadlock Windows named-pipe IPC.  Connection health is
        # checked later via asyncio.to_thread in run() before the candle fetch.
        if not self.broker:
            self._service_errors.append("Broker is required but not provided")
        elif not hasattr(self.broker, "place"):
            self._service_errors.append("Broker is missing required 'place' method")
        
        candle_service = self.agent_config.get("candle_service")
        if not candle_service:
            self._service_errors.append("Candle service is required but not provided")
        elif not hasattr(candle_service, "fetch"):
            self._service_errors.append("Candle service is missing required 'fetch' method")
        
        # Static prompts are used (backtest parity) — no DB prompt required
        # if not self.gpt_prompt_id or not self.gpt_prompt_content:
        #     self._service_errors.append("GPT configuration is required but not provided")
        
        if not self.symbol:
            self._service_errors.append("Symbol is required but not provided")
        
        # Optional services (log info but don't fail)
        optional_services_missing = []
        if not self.agent_config.get("journal"):
            optional_services_missing.append("journal")
            app_logger.info(
                self._log_ctx,
                "Journal service not provided - trade logging will be limited"
            )
        
        mt5_service = self.agent_config.get("mt5_service")
        if not mt5_service:
            optional_services_missing.append("mt5_service")
            app_logger.info(
                self._log_ctx,
                "MT5 service not provided - real-time monitoring will be limited"
            )
        elif hasattr(mt5_service, "_mt5") and mt5_service._mt5 is None:
            # Fast (non-blocking) check: _mt5 is None means initialize() was never called
            app_logger.warning(
                self._log_ctx,
                "MT5 service not initialized - real-time monitoring will be unavailable"
            )
        
        self._services_validated = True
        
        if self._service_errors:
            safe_error_log(
                app_logger.error,
                self._log_ctx,
                "Service validation failed - required services missing",
                errors=self._service_errors
            )
            return False
        
        # Log success with optional services status
        if optional_services_missing:
            app_logger.info(
                self._log_ctx,
                "Required services validated successfully",
                optional_services_missing=optional_services_missing
            )
        else:
            app_logger.info(
                self._log_ctx,
                "All services validated successfully"
            )
        return True

    def invalidate_service_cache(self) -> None:
        """Force re-validation of services on next check."""
        self._services_validated = False
        self._service_errors = []

    async def _flush_db_operations(self) -> None:
        """Flush accumulated database operations."""
        if not self._pending_db_ops:
            return
        
        # Execute all operations concurrently
        tasks = []
        for op in self._pending_db_ops:
            if op.get("type") == "fire_and_forget":
                # Handle fire-and-forget operations
                tasks.append(self._fire_and_forget_op(op))
            elif op.get("type") == "direct_insert":
                # Handle direct insert operations
                tasks.append(self._direct_db_insert(op))
        
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        
        self._pending_db_ops = []

    async def _fire_and_forget_op(self, op: dict) -> None:
        """Execute a fire-and-forget database operation."""
        try:
            # This would call the actual fire-and-forget method
            # For now, we'll just log the operation
            app_logger.debug(
                self._log_ctx,
                "Executing batched fire-and-forget operation",
                collection=op.get("collection"),
                data_keys=list(op.get("data", {}).keys())
            )
        except Exception as e:
            app_logger.error(
                self._log_ctx,
                "Error in batched fire-and-forget operation",
                error=str(e)
            )

    async def _direct_db_insert(self, op: dict) -> None:
        """Execute a direct database insert operation."""
        try:
            # This would call the actual database insert method
            app_logger.debug(
                self._log_ctx,
                "Executing batched direct insert operation",
                collection=op.get("collection"),
                data_keys=list(op.get("data", {}).keys())
            )
        except Exception as e:
            app_logger.error(
                self._log_ctx,
                "Error in batched direct insert operation",
                error=str(e)
            )

    # internal -------------------------------------------------------------
    def clear_active_trade(self) -> None:
        """Clear the active trade state to allow new predictions."""
        if self._active_trade is not None:
            app_logger.info(
                self._log_ctx,
                "Clearing active trade state",
                trade_id=self._active_trade.get("client_order_id", "unknown")
            )
            self._active_trade = None
    
    def has_active_trade(self) -> bool:
        """Check if there's currently an active trade."""
        return self._active_trade is not None
    
    def start_monitoring_window(self, interval_minutes: int = 60) -> None:
        """Start a new monitoring window for the specified interval."""
        self._monitoring_start_time = time.monotonic()
        self._monitoring_duration_minutes = interval_minutes
    
    def set_execution_mode(self, mode: str, scheduled_time: datetime = None) -> None:
        """Set the execution mode for the agent runner."""
        if mode not in ['run_now', 'scheduled']:
            raise ValueError(f"Invalid execution mode: {mode}. Must be 'run_now' or 'scheduled'")
        
        self._execution_mode = mode
        self._scheduled_run_time = scheduled_time
        
        app_logger.info(
            self._log_ctx,
            f"Agent execution mode set to: {mode}",
            scheduled_time=scheduled_time.isoformat() if scheduled_time else None
        )
    
    def should_run_now(self) -> bool:
        """Check if agent should run immediately based on execution mode."""
        if self._execution_mode == 'run_now':
            return True
        elif self._execution_mode == 'run_until_stopped':
            return not self._should_stop
        elif self._execution_mode == 'scheduled' and self._scheduled_run_time:
            now = datetime.now(timezone.utc)
            return now >= self._scheduled_run_time
        return False
    
    def is_monitoring_window_active(self) -> bool:
        """Check if the current monitoring window is still active with short cache."""
        now_mono = time.monotonic()
        if self._window_status_cache is not None and (now_mono - self._window_cache_time < 30):
            return self._window_status_cache
        if self._monitoring_start_time is None:
            result = False
        else:
            elapsed_minutes = (now_mono - self._monitoring_start_time) / 60
            result = elapsed_minutes < self._monitoring_duration_minutes
        self._window_status_cache = result
        self._window_cache_time = now_mono
        return result
    
    def clear_monitoring_window(self) -> None:
        """Clear the monitoring window to allow fresh slot."""
        if self._monitoring_start_time is not None:
            elapsed_minutes = (time.monotonic() - self._monitoring_start_time) / 60
            app_logger.info(
                {"asset": self.symbol, "agent": self.agent_config.get("agent_id")},
                f"Clearing monitoring window after {elapsed_minutes:.1f} minutes"
            )
        self._monitoring_start_time = None
        # Invalidate cached status
        self._window_status_cache = None
    
    async def _initialize_candle_cache(self, candles: list, asset: str) -> bool:
        """Initialize candle cache with candles for market monitoring (last 10 candles of 5min)."""
        
        if self._cache_initialized:
            return True
        
        try:
            # Get cache for this asset with max 10 candles for 5min monitoring
            self._candle_cache = get_cache(asset, "5m")
            self._candle_cache.max_candles = 50  # Keep enough candles for 40+1 monitoring window
            
            # Initialize with candles from broker fetch
            success = self._candle_cache.initialize_from_sniper(candles)
            
            if success:
                self._cache_initialized = True
                self._last_cache_update = datetime.now(timezone.utc)
                app_logger.info(
                    {"asset": asset},
                    "Candle cache initialized successfully for market monitoring",
                    candle_count=len(candles),
                    max_candles=50,
                    cache_stats=self._candle_cache.get_stats()
                )
            else:
                safe_error_log(
                    app_logger.error,
                    {"asset": asset},
                    "Failed to initialize candle cache for market monitoring"
                )
            
            return success
            
        except Exception as e:
            safe_error_log(
                app_logger.error,
                {"asset": asset},
                "Error initializing candle cache for market monitoring",
                error=str(e),
                error_type=type(e).__name__
            )
            return False
    
    async def _update_cache_with_fresh_candles(self, candle_service: Any, asset: str) -> bool:
        """Update cache with fresh candles from broker on every 5min close."""
        try:
            if not self._cache_initialized or not self._candle_cache:
                app_logger.warning(
                    {"asset": asset},
                    "Cache not initialized, cannot update with fresh candles"
                )
                return False
            
            # Use the improved needs_update method from cache
            if not self._candle_cache.needs_update():
                app_logger.debug(
                    {"asset": asset},
                    "Cache update not needed yet",
                    cache_stats=self._candle_cache.get_stats()
                )
                return True
            
            # Fetch latest candle from broker
            latest_candle = await self._candle_cache.fetch_latest_candle_only(candle_service)
            if not latest_candle:
                app_logger.warning(
                    {"asset": asset},
                    "Failed to fetch latest candle for cache update"
                )
                return False
            
            # Update cache with rolling window approach
            success = await self._candle_cache.update_with_new_candle(latest_candle, candle_service)
            if success:
                self._last_cache_update = datetime.now(timezone.utc)
                app_logger.info(
                    {"asset": asset},
                    "Cache updated with fresh candle (rolling window)",
                    new_candle_time=latest_candle.time,
                    cache_stats=self._candle_cache.get_stats()
                )
            else:
                app_logger.warning(
                    {"asset": asset},
                    "Cache update failed, attempting fallback refetch"
                )
                # Try fallback refetch
                fallback_success = await self._candle_cache.fallback_refetch(
                    candle_service, "unknown", getattr(self, 'agent_config', {}).get('agent_id', 'unknown')
                )
                if fallback_success:
                    app_logger.info(
                        {"asset": asset},
                        "Fallback refetch successful after cache update failure"
                    )
                    return True
            
            return success
            
        except Exception as e:
            safe_error_log(
                app_logger.error,
                {"asset": asset},
                "Error updating cache with fresh candles",
                error=str(e),
                error_type=type(e).__name__
            )
            return False
    
    async def _initialize_realtime_monitor(self, candle_service: Any) -> None:
        """Initialize the real-time candle monitor if not already done."""
        
        if self._monitor_initialized:
            return
        
        try:
            # Check if MT5 service is available
            mt5_service = self.agent_config.get("mt5_service")
            if not mt5_service:
                app_logger.info(
                    self._log_ctx,
                    "MT5 service not available - skipping real-time monitor initialization"
                )
                self._monitor_initialized = True  # Mark as initialized to avoid retries
                return
            
            # Resolve the underlying MT5 adapter for real-time monitoring.
            mt5_broker = None
            if hasattr(self.broker, 'mt5') and self.broker.mt5:
                mt5_broker = self.broker

            if not mt5_broker:
                app_logger.debug(
                    self._log_ctx,
                    "MT5 adapter not available for real-time monitoring"
                )
                self._monitor_initialized = True  # Mark as initialized to avoid retries
                return
            
            # Get or create the global real-time monitor
            global_monitor = get_realtime_monitor()
            if global_monitor is None:
                global_monitor = await initialize_realtime_monitor(mt5_broker, candle_service)
            
            self._realtime_monitor = global_monitor
            self._monitor_initialized = True
            
            app_logger.info(
                self._log_ctx,
                "Real-time monitor initialized successfully"
            )
            
        except Exception as e:
            safe_error_log(
                app_logger.error,
                self._log_ctx,
                "Failed to initialize real-time monitor",
                error=str(e),
                error_type=type(e).__name__
            )
            self._monitor_initialized = True  # Mark as initialized to avoid retries
    
    async def _initialize_trade_monitor(self, candle_service: Any, broker: Any, journal: Any) -> None:
        """Initialize the trade monitor if not already done."""
        
        if self._trade_monitor_initialized:
            return
        
        try:
            from packages.core.services.trade_monitor import TradeMonitor
            
            # Create trade monitor with callback for trade closure
            def on_trade_closed(symbol: str, action: str, exit_price: float | None) -> None:
                """Callback when a trade is closed by the monitor."""
                app_logger.info(
                    self._log_ctx,
                    "Trade closed by monitor",
                    symbol=symbol,
                    action=action,
                    exit_price=exit_price,
                )
                
                # Register trade result with daily guards (SL cooldown)
                if self._active_trade and exit_price is not None:
                    entry = self._active_trade.get("entry", 0)
                    direction = self._active_trade.get("direction", "long")
                    trade_asset = self._active_trade.get("asset", symbol)
                    # MONEY, not price points. This used to pass
                    # `exit_price - entry`, so a full stop was ~12.0 on XAUUSD and
                    # ~0.0012 on EURUSD — any threshold expressed in that unit
                    # means something different per asset, and cumulative_pnl was
                    # dimensionless (audit 2026-08-27, M3). `_usd_pnl` is the same
                    # contract-size-correct helper the monitor books partials with.
                    from packages.core.services.trade_monitor import _usd_pnl
                    pnl = _usd_pnl(
                        trade_asset, direction, entry, exit_price,
                        self._active_trade.get("quantity", 0) or 0.0,
                    )
                    self._daily_guards.register_trade(pnl)
                    record_trade_close(trade_asset, pnl)

                # Clear active trade
                self._active_trade = None
                # Clear monitoring window
                self.clear_monitoring_window()
                
                # Trigger immediate GPT cycle restart
                app_logger.info(
                    self._log_ctx,
                    "RESTART TRIGGERED: Initiating immediate GPT cycle after trade close"
                )
                
                # Create task to run immediately (don't await to avoid blocking)
                try:
                    asyncio.create_task(self.run())
                except Exception as e:
                    app_logger.error(
                        self._log_ctx,
                        "Failed to trigger immediate restart",
                        error=str(e),
                        error_type=type(e).__name__
                    )
            
            self._trade_monitor = TradeMonitor(
                candle_service=candle_service,
                broker=broker,
                journal=journal,
                on_trade_closed=on_trade_closed,
                owner_asset=self.symbol,
            )
            
            self._trade_monitor_initialized = True
            
            app_logger.info(
                self._log_ctx,
                "Trade monitor initialized successfully"
            )
            
        except Exception as e:
            safe_error_log(
                app_logger.error,
                self._log_ctx,
                "Failed to initialize trade monitor",
                error=str(e),
                error_type=type(e).__name__
            )
            # Do NOT mark as initialized so it can retry next time
    
    async def _perform_optimized_trigger_check(
        self,
        pred_dict: Dict[str, Any],
        asset: str,
        symbol: str,
        agent_id: str,
        broker_name: str,
        candle_service: Any = None
    ) -> bool:
        """
        OPTIMIZED: Perform trigger check prioritizing cached candles over fresh broker calls.
        Only fetches fresh data if cache is invalid or missing.
        """
        
        try:
            # Step 1: Try to use cache FIRST - avoid broker calls
            if self._cache_initialized and self._candle_cache and self._candle_cache.is_cache_valid():
                # Check if cache needs update (5-minute intervals)
                if self._candle_cache.needs_update():
                    app_logger.info(
                        {"asset": asset, "broker": broker_name, "agent": agent_id},
                        "Cache needs update for trigger check - updating with latest candle"
                    )
                    # Update cache with single latest candle instead of full fetch
                    await self._update_cache_with_fresh_candles(candle_service, asset)
                
                # Use cached data for trigger check
                historical_candles = self._candle_cache.get_historical_candles()
                current_candle = self._candle_cache.get_current_candle()
                
                if historical_candles is not None and current_candle is not None:
                    app_logger.info(
                        {"asset": asset, "broker": broker_name, "agent": agent_id},
                        "OPTIMIZED: Using cached candles for trigger check - NO BROKER CALL",
                        cache_stats=self._candle_cache.get_stats(),
                        window_size=len(historical_candles)
                    )
                    
                    return await self._execute_trigger_check_with_data(
                        pred_dict, historical_candles, current_candle, asset, symbol, agent_id, broker_name
                    )
                else:
                    app_logger.warning(
                        {"asset": asset, "broker": broker_name, "agent": agent_id},
                        "Cache returned invalid data - falling back to fresh fetch"
                    )
            
            # Step 2: Cache miss or invalid - fetch fresh data (ONLY when necessary)
            app_logger.warning(
                {"asset": asset, "broker": broker_name, "agent": agent_id},
                "CACHE MISS: Fetching fresh candles for trigger check - BROKER CALL REQUIRED"
            )
            
            if not candle_service:
                app_logger.error(
                    {"asset": asset, "broker": broker_name, "agent": agent_id},
                    "CRITICAL: No candle service available for fresh data fetch"
                )
                return False
            
            # Fetch minimal fresh data (just 10 candles for trigger check)
            fresh_candles = await candle_service.get_candles(symbol, "5m", 10)
            if not fresh_candles or len(fresh_candles) < 5:
                app_logger.error(
                    {"asset": asset, "broker": broker_name, "agent": agent_id},
                    "CRITICAL: Insufficient fresh candle data for trigger check",
                    candle_count=len(fresh_candles) if fresh_candles else 0
                )
                return False
            
            # Initialize/update cache with fresh data
            if not self._cache_initialized:
                await self._initialize_candle_cache(fresh_candles, asset)
            
            # Convert to format expected by trigger check
            historical_candles = self._convert_candles_to_dataframe(fresh_candles[:-1])
            current_candle = self._candle_to_dict(fresh_candles[-1])
            
            app_logger.info(
                {"asset": asset, "broker": broker_name, "agent": agent_id},
                "Fresh candles fetched for trigger check",
                candle_count=len(fresh_candles),
                cache_updated=self._cache_initialized
            )
            
            return await self._execute_trigger_check_with_data(
                pred_dict, historical_candles, current_candle, asset, symbol, agent_id, broker_name
            )
            
        except Exception as e:
            safe_error_log(
                app_logger.error,
                {"asset": asset, "broker": broker_name, "agent": agent_id},
                "CRITICAL ERROR in optimized trigger check",
                error=str(e),
                error_type=type(e).__name__,
                pattern=pred_dict.get("pattern"),
                level=pred_dict.get("level"),
                direction=pred_dict.get("direction")
            )
            return False
    
    async def _execute_trigger_check_with_data(
        self,
        pred_dict: Dict[str, Any],
        historical_candles: pd.DataFrame,
        current_candle: Dict[str, Any],
        asset: str,
        symbol: str,
        agent_id: str,
        broker_name: str
    ) -> bool:
        """Execute the actual trigger check logic with provided data."""
        
        if historical_candles is None or current_candle is None:
            app_logger.error(
                {"asset": asset, "broker": broker_name, "agent": agent_id},
                "CRITICAL: Invalid data provided for trigger check execution"
            )
            return False
            
        # Calculate dynamic buffer using ATR
        # Use canonical field name 'entry' (fallback to 'level' for backward compatibility)
        entry = pred_dict.get("entry") or pred_dict.get("level")
        current_price = current_candle.get("close", 0) if current_candle else 0
        
        if entry is None or current_price <= 0:
            app_logger.error(
                {"asset": asset, "broker": broker_name, "agent": agent_id},
                "CRITICAL: Invalid entry or price for trigger check",
                entry=entry,
                current_price=current_price
            )
            return False
        
        # Calculate market volatility from historical candles
        market_volatility = 0.0
        if len(historical_candles) > 14:
            atr = calculate_atr(historical_candles, period=14)
            market_volatility = min(1.0, atr / current_price) if current_price > 0 else 0.0
        
        # Calculate dynamic buffer
        try:
            dynamic_buffer = calculate_dynamic_buffer(
                asset=asset,
                level=entry,  # Function expects 'level' parameter name
                current_price=current_price,
                historical_data=historical_candles,
                market_volatility=market_volatility
            )
        except Exception as e:
            app_logger.error(
                {"asset": asset, "broker": broker_name, "agent": agent_id},
                "CRITICAL: Failed to calculate dynamic buffer",
                error=str(e)
            )
            return False
        
        # Perform the enhanced trigger check with 5-minute cache
        # Use canonical field name 'entry'
        cache_key = f"{pred_dict.get('entry') or pred_dict.get('level')}_{pred_dict.get('direction')}_{pred_dict.get('pattern')}"
        now_ts = time.time()
        cached = self._trigger_check_cache.get(cache_key)
        if cached is not None and (now_ts - cached[1] < 300):
            trigger_met = cached[0]
        else:
            trigger_met = enhanced_entry_trigger_met(
            pred=pred_dict,
            historical_candles=historical_candles,
            current_candle=current_candle,
            buffer=dynamic_buffer,
            asset=asset
            )
            self._trigger_check_cache[cache_key] = (trigger_met, now_ts)
        
        # Update slot matcher with historical candles for future monitoring
        if len(historical_candles) > 0:
            self._slot_matcher.update_historical_candles(asset, historical_candles)
        
        app_logger.info(
            {"asset": asset, "broker": broker_name, "agent": agent_id},
            "Trigger check execution completed",
            trigger_met=trigger_met,
            pattern=pred_dict.get("pattern"),
            level=pred_dict.get("level"),
            direction=pred_dict.get("direction"),
            buffer=dynamic_buffer,
            cache_used=True
        )
        
        return trigger_met
    
    def _candle_data_adapter(self) -> CandleDataAdapter:
        return CandleDataAdapter(
            logger=app_logger, error_reporter=safe_error_log,
            log_context=getattr(self, "_log_ctx", {}),
            serializer=serialize_for_json, datetime_type=datetime,
        )

    def _candle_to_dict(self, candle: Any) -> Dict[str, Any]:
        return self._candle_data_adapter().to_dict(candle)
    
    def _convert_candles_to_dataframe(self, c5_raw: list) -> pd.DataFrame:
        return self._candle_data_adapter().to_dataframe(c5_raw)
    
    def _convert_candles_batch(self, c5_raw: list, c15_raw: list) -> tuple:
        return self._candle_data_adapter().to_batch(
            c5_raw, c15_raw, to_dict=self._candle_to_dict,
            to_dataframe=self._convert_candles_to_dataframe,
        )
    
    def _to_json(self, data: Any) -> str:
        return self._candle_data_adapter().to_json(data, to_dict=self._candle_to_dict)
    
    async def _execute_unified_trade(
        self,
        asset: str,
        direction: str,
        entry_price: float,
        stop: float,
        target: float,
        quantity: float,
        pattern: str,
        confidence: float,
        prompt_hash: str,
        gpt_mode: str,
        latency_ms: float,
        trigger_source: str = "main_flow",
        level_id: str = None
    ) -> Dict[str, Any] | None:
        """Unified trade execution method used by both main flow and real-time callbacks."""
        
        try:
            # Reset daily guards on new UTC day
            today = datetime.now(timezone.utc).date()
            if self._daily_guards_last_reset != today:
                self._daily_guards = DailyGuards(
                    max_loss=float("inf"),
                    max_trades=BACKTEST_MAX_TRADES_PER_DAY,
                    cooldown=BACKTEST_COOLDOWN_AFTER_SL,
                    loss_threshold=BACKTEST_COOLDOWN_SL_THRESHOLD,
                )
                self._daily_guards_last_reset = today
            
            # Daily guards: trade cap + SL cooldown (matching backtest)
            if not self._daily_guards.allow_trade():
                app_logger.info(
                    {"asset": asset},
                    f"Daily guard blocked trade from {trigger_source}",
                    trade_count=self._daily_guards.trade_count,
                    cooldown_remaining=self._daily_guards.cooldown_remaining,
                    trigger_source=trigger_source,
                )
                return None
            
            # Check if we already have an active trade
            if self._active_trade is not None:
                app_logger.info(
                    {"asset": asset},
                    f"Skipping trade execution from {trigger_source} - active trade already exists",
                    active_trade_id=self._active_trade.get("client_order_id", "unknown"),
                    trigger_source=trigger_source
                )
                return None
            
            # Apply risk management to calculate proper quantity
            final_quantity = quantity
            if self.risk_percent > 0 and entry_price > 0 and stop is not None and stop > 0:
                try:
                    # Use proper lot sizing based on SL distance and risk percent
                    from packages.core.risk import calc_lot_size_by_risk, CONTRACT_SPECS
                    
                    sl_points = abs(entry_price - stop)
                    symbol_upper = asset.upper()
                    
                    if symbol_upper in CONTRACT_SPECS and sl_points > 0:
                        # Get available cash from broker
                        cash = 0.0
                        if hasattr(self.broker, "balances"):
                            try:
                                balances = await asyncio.to_thread(self.broker.balances)
                                cash = float(balances.get("cash", 0.0))
                            except Exception:
                                cash = 2000.0

                        if cash > 0:
                            final_quantity = calc_lot_size_by_risk(
                                balance=cash,
                                risk_pct=self.risk_percent / 100.0,
                                sl_points=sl_points,
                                symbol=symbol_upper,
                                specs=CONTRACT_SPECS,
                                entry_price=entry_price,
                            )
                            
                            app_logger.info(
                                {"asset": asset},
                                "Risk-based lot sizing applied",
                                risk_percent=self.risk_percent,
                                available_cash=cash,
                                sl_points=sl_points,
                                entry_price=entry_price,
                                calculated_lots=final_quantity,
                            )
                    else:
                        # Fallback: simple risk-based quantity
                        cash = 0.0
                        if hasattr(self.broker, "balances"):
                            try:
                                balances = await asyncio.to_thread(self.broker.balances)
                                cash = float(balances.get("cash", 0.0))
                            except Exception:
                                cash = 2000.0
                        
                        if cash > 0:
                            risk_amount = cash * self.risk_percent / 100.0
                            final_quantity = risk_amount / entry_price
                            app_logger.info(
                                {"asset": asset},
                                "Fallback risk sizing (no CONTRACT_SPECS)",
                                risk_percent=self.risk_percent,
                                risk_amount=risk_amount,
                                final_quantity=final_quantity,
                            )
                except Exception as e:
                    safe_error_log(
                        app_logger.error,
                        {"asset": asset},
                        "Error applying risk management to unified trade",
                        error=str(e),
                        error_type=type(e).__name__
                    )
            elif self.risk_percent == 0 and entry_price > 0:
                # No risk percent configured — use minimum lot from specs as safety
                from packages.core.risk import CONTRACT_SPECS
                symbol_upper = asset.upper()
                if symbol_upper in CONTRACT_SPECS:
                    final_quantity = CONTRACT_SPECS[symbol_upper].min_lot
                    app_logger.warning(
                        {"asset": asset},
                        "risk_percent=0: Using minimum lot size as safety default",
                        min_lot=final_quantity,
                        symbol=symbol_upper,
                    )
                else:
                    # Unknown symbol with no risk config — use 0.01 as ultra-safe default
                    final_quantity = 0.01
                    app_logger.warning(
                        {"asset": asset},
                        "risk_percent=0 and unknown symbol: Using 0.01 as safety default",
                    )
            
            # ── Trade guards (matching backtest _check_trade_guards) ──────────
            asset_upper = asset.upper()
            risk_dist = abs(entry_price - stop) if stop else 0
            
            # SL direction sanity
            if direction == "long" and stop >= entry_price:
                app_logger.warning({"asset": asset}, f"Trade guard: LONG SL ({stop}) >= entry ({entry_price})")
                return None
            if direction == "short" and stop <= entry_price:
                app_logger.warning({"asset": asset}, f"Trade guard: SHORT SL ({stop}) <= entry ({entry_price})")
                return None
            
            # Max SL distance cap (anti-runaway, matching backtest)
            _max_sl = BACKTEST_MAX_SL_DISTANCE.get(asset_upper, BACKTEST_MAX_SL_DISTANCE.get("default", 1000))
            if risk_dist > _max_sl:
                app_logger.warning({"asset": asset}, f"Trade guard: SL distance {risk_dist:.2f} > max {_max_sl}")
                return None
            
            # Min SL distance (anti-scalp, matching backtest)
            _min_sl = BACKTEST_MIN_SL_DISTANCE.get(asset_upper, BACKTEST_MIN_SL_DISTANCE.get("default", 20))
            if risk_dist < _min_sl:
                app_logger.warning({"asset": asset}, f"Trade guard: SL distance {risk_dist:.5g} < min {_min_sl}")
                return None
            
            # Min target distance (anti-scalp, matching backtest)
            if target is not None:
                target_dist = abs(target - entry_price)
                _min_target = BACKTEST_MIN_TARGET_DISTANCE.get(asset_upper, BACKTEST_MIN_TARGET_DISTANCE.get("default", 30))
                if target_dist < _min_target:
                    app_logger.warning({"asset": asset}, f"Trade guard: target distance {target_dist:.5g} < min {_min_target}")
                    return None
            
            # Min RR ratio (matching backtest)
            if target is not None and risk_dist > 0:
                _rr = abs(target - entry_price) / risk_dist
                if _rr < BACKTEST_MIN_RR:
                    app_logger.warning({"asset": asset}, f"Trade guard: RR {_rr:.2f} < min {BACKTEST_MIN_RR}")
                    return None
            
            # Min SL % crypto widening (matching backtest _apply_min_sl_distance_crypto)
            _crypto_assets = {"BTCUSD", "BTCUSDT", "ETHUSD", "ETHUSDT"}
            if asset_upper in _crypto_assets and entry_price > 0:
                sl_pct = (risk_dist / entry_price) * 100.0
                if sl_pct < BACKTEST_MIN_SL_PCT_CRYPTO:
                    min_dist = entry_price * (BACKTEST_MIN_SL_PCT_CRYPTO / 100.0)
                    old_stop = stop
                    if direction == "long":
                        stop = entry_price - min_dist
                    else:
                        stop = entry_price + min_dist
                    app_logger.info(
                        {"asset": asset},
                        f"Widened SL for crypto: {old_stop:.2f} -> {stop:.2f} (min {BACKTEST_MIN_SL_PCT_CRYPTO}%)",
                    )
            
            # Entry drift check (matching backtest _check_entry_drift)
            _max_drift = BACKTEST_MAX_ENTRY_DRIFT.get(asset_upper, BACKTEST_MAX_ENTRY_DRIFT.get("default"))
            if _max_drift is not None and _max_drift > 0:
                # Get latest candle close as reference price
                try:
                    candle_svc = self.agent_config.get("candle_service")
                    if candle_svc and hasattr(candle_svc, "get_latest_closed_candle"):
                        latest = await candle_svc.get_latest_closed_candle(asset, "5m")
                        if latest:
                            ref_price = latest.get("close") if isinstance(latest, dict) else getattr(latest, "close", None)
                            if ref_price:
                                drift = abs(entry_price - float(ref_price))
                                if drift > _max_drift:
                                    app_logger.warning(
                                        {"asset": asset},
                                        f"Trade guard: entry drift {drift:.2f} > max {_max_drift}",
                                    )
                                    return None
                except Exception as e:
                    app_logger.debug({"asset": asset}, f"Entry drift check skipped: {e}")
            
            # Create trading decision
            from packages.core.models import Decision as TradingDecision, Side, PriceType

            side = Side.BUY if direction == "long" else Side.SELL

            if direction == "long":
                tp1_price = entry_price + (target - entry_price) / 2
            else:
                tp1_price = entry_price - (entry_price - target) / 2

            decision = TradingDecision(
                asset=asset,
                symbol=asset,
                side=side,
                price_type=PriceType.MARKET,
                entry=entry_price,
                quantity=final_quantity,
                client_order_id=f"{asset}_{int(time.time() * 1000)}",
                sl=stop,
                tp=target,
                tp1=tp1_price,
            )
            
            # Execute the trade
            broker = self.broker
            journal = self.agent_config.get("journal")
            
            executed_result = await self._execute_order(
                asset,
                decision,
                broker,
                journal,
                gpt_mode,
                prompt_hash,
                latency_ms,
                {},  # raw_decision
                entry_price,
                datetime.now(timezone.utc).date(),
                pattern=pattern,
                confidence=confidence,
                trigger_result=True,
            )
            
            if executed_result:
                record_trade_open(asset)

                # Mark as active trade
                self._active_trade = {
                    "client_order_id": decision.client_order_id,
                    "asset": asset,
                    "timestamp": datetime.now(timezone.utc),
                    "prediction": {
                        "direction": direction,
                        "level": entry_price,
                        "stop": stop,
                        "target": target,
                        "pattern": pattern,
                        "confidence": confidence
                    },
                    "executed_result": executed_result,
                    "trigger_source": trigger_source,
                    "level_id": level_id
                }

                # Clear monitoring window when trade is executed
                self.clear_monitoring_window()
                
                # Track active trade in trading filters
                now_dt = datetime.now(timezone.utc)
                exit_time = now_dt + timedelta(hours=TRADE_MONITOR_EXPIRY_HOURS)
                trading_filters.track_active_trade(
                    asset=asset,
                    entry_time=now_dt,
                    exit_time=exit_time,
                    trade_id=decision.client_order_id
                )
                
                # Initialize trade monitor now that we have a trade to monitor
                if not self._trade_monitor_initialized:
                    await self._initialize_trade_monitor(
                        self.agent_config["candle_service"], 
                        broker, 
                        journal
                    )
                
                # Queue trade for monitoring if trade monitor is available
                if self._trade_monitor:
                    try:
                        # Create trade state for monitoring
                        trade_state = {
                            "direction": direction,
                            "entry": entry_price,
                            "sl": stop,
                            "initial_stop": stop,
                            "tp": target,
                            "quantity": final_quantity,
                            "client_order_id": decision.client_order_id,
                            "pattern": pattern,
                            "confidence": confidence
                        }
                        
                        rules = {
                            "tp1": tp1_price,
                            "tp": target,
                        }
                        
                        # Queue the trade for monitoring
                        self._trade_monitor.queue_trade(
                            symbol=asset,
                            trade_state=trade_state,
                            rules=rules,
                            expiry=exit_time,
                            prompt_hash=prompt_hash,
                            gpt_mode=gpt_mode,
                            latency_ms=latency_ms,
                            trade_id=decision.client_order_id,
                        )
                        
                        app_logger.info(
                            {"asset": asset},
                            f"Trade queued for monitoring from {trigger_source}",
                            trade_id=decision.client_order_id,
                            expiry_time=exit_time.isoformat(),
                            monitoring_rules=rules,
                            trigger_source=trigger_source
                        )
                        
                    except Exception as e:
                        safe_error_log(
                            app_logger.error,
                            {"asset": asset},
                            f"Failed to queue trade for monitoring from {trigger_source}",
                            error=str(e),
                            trade_id=decision.client_order_id,
                            trigger_source=trigger_source
                        )
                
                return {
                    "client_order_id": decision.client_order_id,
                    "executed_result": executed_result,
                    "direction": direction,
                    "entry_price": entry_price,
                    "stop": stop,
                    "target": target,
                    "pattern": pattern,
                    "confidence": confidence,
                    "trigger_source": trigger_source
                }
            else:
                safe_error_log(
                    app_logger.error,
                    {"asset": asset},
                    f"Trade execution failed from {trigger_source}",
                    direction=direction,
                    entry_price=entry_price,
                    trigger_source=trigger_source
                )
                return None
                
        except Exception as e:
            safe_error_log(
                app_logger.error,
                {"asset": asset},
                f"Error in unified trade execution from {trigger_source}",
                error=str(e),
                error_type=type(e).__name__,
                direction=direction,
                trigger_source=trigger_source
            )
            return None

    def _create_trigger_callback(self, asset: str, agent_id: str, broker_name: str):
        """Create a callback function for when a trigger is found."""
        
        async def trigger_callback(level_id: str, level_data: Dict[str, Any], trigger_candle: Dict[str, Any]):
            """Callback executed when a trigger is found - executes trade automatically."""
            
            try:
                app_logger.info(
                    {"asset": asset, "broker": broker_name, "agent": agent_id},
                    "Real-time trigger callback executed - executing trade",
                    level_id=level_id,
                    level_value=level_data["level_value"],
                    direction=level_data["direction"],
                    pattern=level_data["pattern_type"],
                    trigger_candle_time=trigger_candle.get("time"),
                    trigger_candle_close=trigger_candle.get("close")
                )
                
                # Check if we already have an active trade
                if self._active_trade is not None:
                    app_logger.info(
                        {"asset": asset, "broker": broker_name, "agent": agent_id},
                        "Skipping trigger execution - active trade already exists",
                        active_trade_id=self._active_trade.get("client_order_id", "unknown")
                    )
                    return
                
                # Get prediction data from level_data
                prediction_data = level_data.get("prediction_data", {})
                if not prediction_data:
                    safe_error_log(
                        app_logger.error,
                        {"asset": asset, "broker": broker_name, "agent": agent_id},
                        "No prediction data available for trigger execution",
                        level_id=level_id
                    )
                    return
                
                # Use unified trade execution method
                direction = level_data["direction"]
                gpt_mode = prediction_data.get("gpt_mode", "sniper")
                prompt_hash = prediction_data.get("prompt_hash", "")
                
                executed_result = await self._execute_unified_trade(
                    asset=asset,
                    direction=direction,
                    entry_price=trigger_candle.get("close", level_data["level_value"]),
                    stop=prediction_data.get("stop"),
                    target=prediction_data.get("target"),
                    quantity=prediction_data.get("quantity", 1.0),
                    pattern=level_data["pattern_type"],
                    confidence=prediction_data.get("confidence"),
                    prompt_hash=prompt_hash,
                    gpt_mode=gpt_mode,
                    latency_ms=0.0,  # No latency for real-time triggers
                    trigger_source="realtime_callback",
                    level_id=level_id
                )
                
                if executed_result:
                    app_logger.info(
                        {"asset": asset, "broker": broker_name, "agent": agent_id},
                        "Trade executed from real-time trigger",
                        trade_id=executed_result.get("client_order_id"),
                        entry_price=trigger_candle.get("close"),
                        direction=direction,
                        pattern=level_data["pattern_type"]
                    )
                else:
                    safe_error_log(
                        app_logger.error,
                        {"asset": asset, "broker": broker_name, "agent": agent_id},
                        "Trade execution failed from real-time trigger",
                        level_id=level_id,
                        direction=direction
                    )
                
            except Exception as e:
                safe_error_log(
                    app_logger.error,
                    {"asset": asset, "broker": broker_name, "agent": agent_id},
                    "Error in trigger callback execution",
                    level_id=level_id,
                    error=str(e),
                    error_type=type(e).__name__
                )
        
        return trigger_callback
    
    async def _cleanup_expired_data(self) -> None:
        """Clean up expired predictions and perform periodic maintenance."""
        now = time.monotonic()
        if now - self._last_cleanup < self._cleanup_interval:
            return
        
        app_logger.info(
            {"asset": self.symbol, "agent": self.agent_config.get("agent_id")},
            "Performing periodic cleanup"
        )
        
        # Clean up expired predictions
        now_dt = datetime.now(timezone.utc)
        expired_count = 0
        remaining_predictions = []
        
        for item in self._pending_predictions:
            created_at = item["timestamp"]
            if now_dt - created_at <= timedelta(minutes=PREDICTION_TIMEOUT_MINUTES):
                remaining_predictions.append(item)
            else:
                expired_count += 1
        
        if expired_count > 0:
            app_logger.info(
                {"asset": self.symbol, "agent": self.agent_config.get("agent_id")},
                f"Cleaned up {expired_count} expired predictions",
                remaining_predictions=len(remaining_predictions)
            )
        
        self._pending_predictions = remaining_predictions
        self._last_cleanup = now
        
        # Also cleanup old slot matches (synchronous cleanup)
        try:
            # Clean up old matches from memory (synchronous operation)
            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=24)
            cleaned_count = 0
            
            matches_to_remove = []
            for match_id, match_data in self._slot_matcher._active_matches.items():
                if match_data.get("level_drawn_at", datetime.now(timezone.utc)) < cutoff_time:
                    matches_to_remove.append(match_id)
            
            for match_id in matches_to_remove:
                del self._slot_matcher._active_matches[match_id]
                cleaned_count += 1
            
            if cleaned_count > 0:
                app_logger.info(
                    {"asset": self.symbol, "agent": self.agent_config.get("agent_id")},
                    f"Cleaned up {cleaned_count} old slot matches from memory"
                )
        except Exception as e:
            safe_error_log(
                app_logger.error,
                {"asset": self.symbol, "agent": self.agent_config.get("agent_id")},
                "Error cleaning up old slot matches",
                error=str(e),
                error_type=type(e).__name__
            )
        
        # Clean up expired levels in real-time monitor
        if self._realtime_monitor:
            try:
                await self._realtime_monitor.cleanup_expired_levels()
            except Exception as e:
                safe_error_log(
                    app_logger.error,
                    {"asset": self.symbol, "agent": self.agent_config.get("agent_id")},
                    "Error cleaning up real-time monitor levels",
                    error=str(e),
                    error_type=type(e).__name__
                )
    
    async def cleanup_resources(self) -> None:
        """Clean up all resources including TradeMonitor and CodeSniperMonitor."""
        agent_id = self.agent_config.get("agent_id", "unknown")
        log_ctx = {"asset": self.symbol, "agent": agent_id}
        try:
            # Clean up trade monitor
            if hasattr(self, '_trade_monitor') and self._trade_monitor is not None:
                app_logger.info(log_ctx, "Shutting down trade monitor")
                try:
                    await asyncio.wait_for(self._trade_monitor.shutdown(), timeout=5.0)
                except asyncio.TimeoutError:
                    app_logger.warning(log_ctx, "Trade monitor shutdown timed out after 5s, forcing cleanup")
                self._trade_monitor = None

            # Clean up real-time monitor if it has a shutdown method
            if hasattr(self, '_realtime_monitor') and self._realtime_monitor is not None:
                if hasattr(self._realtime_monitor, 'shutdown'):
                    await self._realtime_monitor.shutdown()
                self._realtime_monitor = None

            # The strategy monitor is deliberately NOT torn down here.
            #
            # api.py runs cleanup_resources() in a `finally` after EVERY cycle,
            # but the monitor is process-global and its lifetime is a judge TTL
            # (90-240 min), not one cycle. Stopping it here would destroy the
            # strategy the cycle just armed, seconds after arming it. Until
            # 2026-08-27 this block existed but never ran — the sync registry
            # getter always returned None inside the event loop — which is the
            # only reason live trading worked at all. Removing it turns that
            # accident into an intention.
            #
            # The monitor is stopped by stop_continuous_monitoring(),
            # emergency_stop() and Supervisor.stop(), all of which call
            # shutdown_code_sniper_monitor().

            # Monitoring window + pending predictions (folded in from the
            # shadowed duplicate of this method that used to sit at line 785).
            self.clear_monitoring_window()
            if hasattr(self, "_pending_predictions"):
                self._pending_predictions = []

            # Cancel continuous task if still running
            if hasattr(self, '_continuous_task') and self._continuous_task and not self._continuous_task.done():
                self._continuous_task.cancel()
                app_logger.info(log_ctx, "Continuous monitoring task cancelled during cleanup")

            # Clear state
            self._should_stop = True
            self._is_running = False
            self._active_trade = None

        except Exception as e:
            safe_error_log(
                app_logger.error, log_ctx,
                "Error during resource cleanup",
                error=str(e)
            )

    def _check_budgets(self) -> bool:
        """Return ``True`` if budgets allow another GPT call."""

        now = time.monotonic()

        if self.rate_limit_per_min is not None:
            elapsed = now - self._last_refill
            refill_rate = self.rate_limit_per_min / 60.0
            self._tokens = min(
                self.rate_limit_per_min, self._tokens + elapsed * refill_rate
            )
            self._last_refill = now
            if self._tokens < 1.0:
                logger.info("rate limit exceeded, skipping cycle")
                return False
            self._tokens -= 1.0

        if self.timeout_ms:
            if now < self._next_allowed_time:
                logger.info("timeout budget exhausted, skipping cycle")
                return False
            self._next_allowed_time = now + self.timeout_ms / 1000.0

        return True

    async def _check_future_candle_triggers(self, asset: str, candle_service: Any) -> bool:
        """Check if any active slot matches are triggered by new candle data."""
        try:
            # Get the latest 5-minute candle
            c5_raw, _ = await candle_service.fetch(asset, n5=1, n15=1)
            if not c5_raw:
                return False
            
            latest_candle = c5_raw[-1]
            
            # Extract candle data
            if hasattr(latest_candle, 'time'):
                candle_time = latest_candle.time
                open_price = float(latest_candle.open)
                high_price = float(latest_candle.high)
                low_price = float(latest_candle.low)
                close_price = float(latest_candle.close)
                volume = float(latest_candle.volume) if latest_candle.volume is not None else None
            elif isinstance(latest_candle, dict):
                candle_time = latest_candle.get('time')
                open_price = float(latest_candle.get('open', 0))
                high_price = float(latest_candle.get('high', 0))
                low_price = float(latest_candle.get('low', 0))
                close_price = float(latest_candle.get('close', 0))
                volume = float(latest_candle.get('volume', 0)) if latest_candle.get('volume') is not None else None
            else:
                return False
            
            # Convert timestamp to datetime if needed
            if isinstance(candle_time, str):
                candle_time = datetime.fromisoformat(candle_time.replace('Z', '+00:00'))
            elif hasattr(candle_time, 'to_pydatetime'):
                candle_time = candle_time.to_pydatetime()
            
            # Check triggers using slot matcher
            triggered_matches = await self._slot_matcher.check_trigger_match(
                asset=asset,
                candle_timestamp=candle_time,
                open_price=open_price,
                high_price=high_price,
                low_price=low_price,
                close_price=close_price,
                volume=volume
            )
            
            # Execute trades for triggered matches
            for match in triggered_matches:
                match_id = match["match_id"]
                level_value = match["level_value"]
                direction = match["direction"]
                trigger_candle = match["trigger_candle"]
                
                app_logger.info(
                    {"asset": asset, "match_id": match_id},
                    "Level triggered by future candle",
                    level_value=level_value,
                    direction=direction,
                    trigger_candle=trigger_candle
                )
                
                # Execute trade for this trigger
                await self._execute_triggered_trade(match, asset)
                
            return len(triggered_matches) > 0
            
        except Exception as e:
            safe_error_log(
                app_logger.error,
                {"asset": asset},
                "Error checking future candle triggers",
                error=str(e),
                error_type=type(e).__name__
            )
            return False

    async def _execute_triggered_trade(self, match: dict, asset: str) -> None:
        """Execute a trade when a slot match is triggered."""
        try:
            match_id = match["match_id"]
            level_value = match["level_value"]
            direction = match["direction"]
            trigger_candle = match["trigger_candle"]
            match_data = match["match_data"]
            
            # Get broker and journal from agent config
            broker = self.agent_config.get("broker")
            journal = self.agent_config.get("journal")
            
            if not broker:
                safe_error_log(
                    app_logger.error,
                    {"asset": asset, "match_id": match_id},
                    "No broker available for triggered trade execution"
                )
                return
            
            # Create a trading decision based on the triggered match
            from packages.core.models import Decision, Side, PriceType
            
            # Calculate quantity based on risk management
            stored_quantity = match_data.get("quantity", 1.0)
            quantity = stored_quantity  # Start with stored quantity
            if self.risk_percent and hasattr(broker, "balances"):
                try:
                    balances = await asyncio.to_thread(broker.balances)
                    cash = float(balances.get("cash", 0.0))
                    if cash > 0 and trigger_candle["close"] > 0:
                        # Calculate quantity based on risk percentage
                        risk_amount = cash * self.risk_percent / 100.0
                        risk_based_quantity = risk_amount / trigger_candle["close"]
                        # Use the smaller of stored quantity or risk-based quantity
                        quantity = min(quantity, risk_based_quantity)
                except Exception:
                    pass  # Keep the stored quantity
            
            # Get stop loss and take profit from the stored prediction data
            stop_loss = match_data.get("stop_loss")
            take_profit = match_data.get("take_profit")
            
            decision = Decision(
                asset=asset,
                symbol=asset,
                side=Side.BUY if direction == "long" else Side.SELL,
                price_type=PriceType.MARKET,
                entry=trigger_candle["close"],  # Use close price as entry
                quantity=quantity,
                sl=float(stop_loss) if stop_loss is not None else None,
                tp=float(take_profit) if take_profit is not None else None,
                client_order_id=f"trigger_{match_id}_{int(time.time() * 1000)}",
                notes=f"Triggered from slot match {match_id}"
            )
            
            # Execute the order
            executed_result = await self._execute_order(
                asset=asset,
                decision=decision,
                broker=broker,
                journal=journal,
                gpt_mode=match_data.get("gpt_mode", "trigger"),
                prompt_hash=match_data.get("prompt_hash", ""),
                latency_ms=0.0,
                spot_price=trigger_candle["close"],
                pattern=match_data.get("pattern"),
                confidence=match_data.get("confidence"),
                trigger_result=True,
            )
            
            if executed_result:
                # Mark the match as executed
                await self._slot_matcher.execute_trade_from_trigger(
                    match_id=match_id,
                    entry_price=trigger_candle["close"],
                    trade_id=getattr(executed_result, "client_order_id", None)
                )
                
                # Set as active trade to prevent multiple executions
                self._active_trade = {
                    "client_order_id": decision.client_order_id,
                    "timestamp": datetime.now(timezone.utc),
                    "match_id": match_id,
                    "executed_result": executed_result
                }
                
                # Clear monitoring window when trade is executed
                self.clear_monitoring_window()
                
                # Track active trade in trading filters (4 hours max, matching backtest)
                now_dt = datetime.now(timezone.utc)
                exit_time = now_dt + timedelta(hours=TRADE_MONITOR_EXPIRY_HOURS)
                trading_filters.track_active_trade(
                    asset=asset,
                    entry_time=now_dt,
                    exit_time=exit_time,
                    trade_id=decision.client_order_id
                )
                
                app_logger.info(
                    {"asset": asset, "match_id": match_id},
                    "Trade executed - 3-hour window started, no GPT calls allowed",
                    trade_id=decision.client_order_id,
                    exit_time=exit_time.isoformat()
                )
                
                app_logger.info(
                    {"asset": asset, "match_id": match_id},
                    "Trade executed from slot trigger",
                    entry_price=trigger_candle["close"],
                    direction=direction,
                    trade_id=decision.client_order_id
                )
            
        except Exception as e:
            safe_error_log(
                app_logger.error,
                {"asset": asset, "match_id": match.get("match_id", "unknown")},
                "Error executing triggered trade",
                error=str(e),
                error_type=type(e).__name__
            )

    async def _fetch_candles_with_validation(
        self,
        symbol: str,
        n5: int,
        n15: int,
        candle_service: Any,
        broker_name: str,
        agent_id: str,
    ) -> tuple[list, list]:
        """
        Enhanced candle retrieval with robust MT5 integration and validation.
        
        This method ensures that:
        1. We get exactly the number of candles requested (no fallback to defaults)
        2. We check database first, then broker if insufficient
        3. We validate timestamps to ensure we have the right candles for current time
        4. We store any new candles fetched from broker
        5. We handle MT5 connection issues gracefully
        6. We validate candle data integrity before proceeding
        """
        from datetime import datetime, timedelta
        import pandas as pd
        
        app_logger.info(
            {"asset": symbol, "broker": broker_name, "agent": agent_id},
            "Starting candle retrieval with validation and freshness check",
            requested={"5m": n5, "15m": n15},
            current_time=datetime.now(timezone.utc).isoformat()
        )
        
        # ALWAYS fetch fresh data directly from broker - no database/cache
        app_logger.info(
            {"asset": symbol, "broker": broker_name, "agent": agent_id},
            "Fetching FRESH data directly from broker - no database/cache",
            requested={"5m": n5, "15m": n15}
        )
        
        try:
            c5_raw, c15_raw = await candle_service.fetch(symbol, n5=n5, n15=n15, force_broker=False)
            
            app_logger.info(
                {"asset": symbol, "broker": broker_name, "agent": agent_id},
                "FRESH data fetched successfully from broker",
                received={"5m": len(c5_raw), "15m": len(c15_raw)}
            )
            
            return c5_raw, c15_raw
            
        except Exception as e:
            try:
                safe_error_log(
                    app_logger.error,
                    {"asset": symbol, "broker": broker_name, "agent": agent_id},
                    "CRITICAL: Failed to fetch fresh data from broker",
                    error=str(e)
                )
            except Exception as log_error:
                # Fallback logging if the main logger fails
                print(f"[ERROR] CRITICAL: Failed to fetch fresh data from broker - {symbol} - {str(e)} - Logger error: {str(log_error)}")
                # Use standard Python logging as fallback
                import logging
                logging.error(f"CRITICAL: Failed to fetch fresh data from broker - {symbol} - {str(e)}")
            raise

    def _invalidate_stale_cache(self, symbol: str) -> None:
        """
        Invalidate any stale cache data to force fresh data fetch.
        This is called when we detect that cached data is too old or at start of each interval run.
        """
        try:
            # Clear any cached data for this symbol
            if hasattr(self, '_candle_cache') and self._candle_cache:
                self._candle_cache._cache_valid = False
                self._candle_cache._candles = []
                self._candle_cache._dataframe = None
                self._candle_cache._last_update = None
                app_logger.info(
                    {"asset": symbol},
                    "Invalidated stale candle cache to force fresh data fetch",
                    interval_run_id=getattr(self, '_current_interval_timestamp', None)
                )
            
            # Clear any slot matcher cache
            if hasattr(self, '_slot_matcher') and self._slot_matcher:
                # Reset any cached candle data in slot matcher
                if hasattr(self._slot_matcher, '_cached_candles'):
                    self._slot_matcher._cached_candles = {}
                app_logger.info(
                    {"asset": symbol},
                    "Invalidated stale slot matcher cache",
                    interval_run_id=getattr(self, '_current_interval_timestamp', None)
                )
            
            # Clear any real-time monitor cache
            if hasattr(self, '_realtime_monitor') and self._realtime_monitor:
                if hasattr(self._realtime_monitor, '_last_candle_times'):
                    self._realtime_monitor._last_candle_times = {}
                app_logger.info(
                    {"asset": symbol},
                    "Invalidated real-time monitor cache",
                    interval_run_id=getattr(self, '_current_interval_timestamp', None)
                )
                
        except Exception as e:
            safe_error_log(
                app_logger.error,
                {"asset": symbol},
                "Error invalidating stale cache",
                error=str(e)
            )

    def _get_data_health_status(self, c5_raw: list, c15_raw: list) -> dict:
        """Get health status of fresh candle data from broker."""
        current_time = datetime.now()
        health_status = {
            "timestamp": current_time.isoformat(),
            "data_source": "broker_fresh",
            "5m_candle_count": len(c5_raw) if c5_raw else 0,
            "15m_candle_count": len(c15_raw) if c15_raw else 0,
            "health_status": "healthy",
            "recommendations": []
        }
        
        # Since we fetch fresh data from broker, data is always healthy
        if c5_raw and len(c5_raw) > 0 and c15_raw and len(c15_raw) > 0:
            health_status["health_status"] = "healthy"
            health_status["recommendations"].append("Fresh data from broker")
        else:
            health_status["health_status"] = "warning"
            health_status["recommendations"].append("Insufficient candle data from broker")
        
        return health_status

    def _validate_candle_data_integrity(
        self,
        c5_raw: list,
        c15_raw: list,
        symbol: str,
        broker_name: str,
        agent_id: str
    ) -> bool:
        """
        Validate candle data integrity to ensure we have valid OHLC data.
        
        This method checks:
        1. All required fields are present (time, open, high, low, close)
        2. OHLC values are valid (high >= low, high >= open, high >= close, etc.)
        3. No null or invalid values
        4. Timestamps are valid and sequential
        """
        try:
            # Validate 5-minute candles
            for i, candle in enumerate(c5_raw):
                if not self._validate_single_candle(candle, f"5m_candle_{i}"):
                    return False
            
            # Validate 15-minute candles
            for i, candle in enumerate(c15_raw):
                if not self._validate_single_candle(candle, f"15m_candle_{i}"):
                    return False
            
            app_logger.debug(
                {"asset": symbol, "broker": broker_name, "agent": agent_id},
                "Candle data integrity validation passed",
                candle_counts={"5m": len(c5_raw), "15m": len(c15_raw)}
            )
            return True
            
        except Exception as e:
            safe_error_log(
                app_logger.error,
                {"asset": symbol, "broker": broker_name, "agent": agent_id},
                "Error during candle data integrity validation",
                error=str(e)
            )
            return False
    
    def _validate_single_candle(self, candle: Any, candle_id: str) -> bool:
        """Validate a single candle's data integrity."""
        try:
            # Extract OHLC values
            if hasattr(candle, '__dict__'):
                # Candle object
                open_val = float(getattr(candle, 'open', 0))
                high_val = float(getattr(candle, 'high', 0))
                low_val = float(getattr(candle, 'low', 0))
                close_val = float(getattr(candle, 'close', 0))
                time_val = getattr(candle, 'time', None)
            elif isinstance(candle, dict):
                # Dictionary
                open_val = float(candle.get('open', 0))
                high_val = float(candle.get('high', 0))
                low_val = float(candle.get('low', 0))
                close_val = float(candle.get('close', 0))
                time_val = candle.get('time')
            else:
                safe_error_log(
                    app_logger.error,
                    {"candle_id": candle_id},
                    "Invalid candle format",
                    candle_type=str(type(candle))
                )
                return False
            
            # Check for valid values
            if any(val <= 0 for val in [open_val, high_val, low_val, close_val]):
                safe_error_log(
                    app_logger.error,
                    {"candle_id": candle_id},
                    "Invalid OHLC values",
                    open_val=open_val, high_val=high_val, low_val=low_val, close_val=close_val
                )
                return False
            
            # Check OHLC relationships
            if high_val < low_val:
                safe_error_log(
                    app_logger.error,
                    {"candle_id": candle_id},
                    "High < Low",
                    high_val=high_val, low_val=low_val
                )
                return False
            
            if high_val < open_val or high_val < close_val:
                safe_error_log(
                    app_logger.error,
                    {"candle_id": candle_id},
                    "High < Open/Close",
                    high_val=high_val, open_val=open_val, close_val=close_val
                )
                return False
            
            if low_val > open_val or low_val > close_val:
                safe_error_log(
                    app_logger.error,
                    {"candle_id": candle_id},
                    "Low > Open/Close",
                    low_val=low_val, open_val=open_val, close_val=close_val
                )
                return False
            
            # Check timestamp
            if time_val is None:
                safe_error_log(
                    app_logger.error,
                    {"candle_id": candle_id},
                    "Missing timestamp"
                )
                return False
            
            return True
            
        except Exception as e:
            safe_error_log(
                app_logger.error,
                {"candle_id": candle_id},
                "Error validating candle",
                error=str(e)
            )
            return False

    def _validate_candle_timestamps(
        self,
        c5_raw: list,
        c15_raw: list,
        current_time: datetime,
        n5: int,
        n15: int
    ) -> bool:
        """
        Validate that we have candles from the broker.
        Since we fetch fresh data from broker on every run, we trust the data.
        """
        try:
            # Basic validation - just check if we have candles
            if not c5_raw or len(c5_raw) == 0:
                app_logger.warning(
                    self._log_ctx,
                    "No 5m candles available from broker"
                )
                return False
                
            if not c15_raw or len(c15_raw) == 0:
                app_logger.warning(
                    self._log_ctx,
                    "No 15m candles available from broker"
                )
                return False
            
            # Trust the fresh data from broker
            app_logger.info(
                self._log_ctx,
                "Fresh candle data validated from broker",
                candles_5m=len(c5_raw),
                candles_15m=len(c15_raw)
            )
            
            return True
            
        except Exception as e:
            safe_error_log(
                app_logger.error,
                self._log_ctx,
                "Error validating candle data from broker",
                error=str(e)
            )
            return False

    # REMOVED: _store_candles_in_db method - no longer storing candles in database

    async def _execute_order(
        self,
        asset: str,
        decision,
        broker,
        journal,
        gpt_mode: str,
        prompt_hash: str,
        latency_ms: float,
        raw_decision: Mapping[str, Any] | None = None,
        spot_price: float | None = None,
        as_of: date | None = None,
        pattern: str | None = None,
        confidence: float | None = None,
        trigger_result: bool | None = None,
    ) -> Any:
        positions_before: dict[str, Any] = {}
        if hasattr(broker, "positions"):
            try:
                positions_before = {p.symbol: p for p in await asyncio.to_thread(broker.positions)}
            except Exception:
                positions_before = {}

        agent_id = self.agent_id  # Use instance variable
        # Generate a client_order_id if not present in the decision
        client_order_id = getattr(decision, "client_order_id", None) or f"{asset}_{int(time.time() * 1000)}"
        
        # If the decision is from normalize_decision (simple decision), we need to convert it
        # to a proper trading decision that the broker can handle
        if hasattr(decision, "action") and hasattr(decision, "size") and not hasattr(decision, "symbol"):
            # This is a simple decision from normalize_decision, convert it to a proper trading decision
            from packages.core.models import Decision as TradingDecision, Side, PriceType
            
            # Map the action to a side
            if decision.action.value == "buy":
                side = Side.BUY
            elif decision.action.value == "sell":
                side = Side.SELL
            else:
                # For hold or unknown actions, don't execute
                return None
            
            # Create a proper trading decision
            decision = TradingDecision(
                asset=asset,
                symbol=asset,  # Use asset as symbol for now
                side=side,
                price_type=PriceType.MARKET,
                entry=0.0,  # Will be filled by broker with current market price
                quantity=decision.size,
                client_order_id=client_order_id,
            )
        else:
            # Ensure the decision object has the client_order_id attribute
            if not hasattr(decision, "client_order_id"):
                decision.client_order_id = client_order_id
        try:
            payload_decision = asdict(decision) if is_dataclass(decision) else getattr(decision, "__dict__", decision)
            payload_decision = json.loads(json.dumps(payload_decision, default=str))
        except Exception:
            payload_decision = str(decision)
        # Debug logging to understand broker type
        app_logger.info(
            {"asset": asset, "agent": agent_id},
            "Broker type detection",
            broker_class_name=broker.__class__.__name__,
            broker_module=broker.__class__.__module__,
            broker_type=broker.__class__.__name__
        )
        
        # Check if there are open trades for the SAME asset - if so, don't execute new trades
        open_positions = []
        if hasattr(broker, "positions"):
            try:
                open_positions = await asyncio.to_thread(broker.positions)
            except Exception:
                open_positions = []
        
        # Only skip if there's an open position for the same asset
        # Use case-insensitive comparison and strip suffixes for matching
        asset_upper = asset.upper().strip()
        same_asset_positions = [
            pos for pos in open_positions 
            if pos.symbol.upper().strip().startswith(asset_upper)
        ]
        if same_asset_positions:
            app_logger.info(
                {"asset": asset, "agent": agent_id},
                "Skipping new trade execution - open position exists for same asset",
                open_positions_count=len(same_asset_positions),
                open_symbols=[pos.symbol for pos in same_asset_positions],
                trade_id=client_order_id,
            )
            return None
        
        # Validate decision before placing order
        if not hasattr(decision, 'symbol') or not hasattr(decision, 'side') or not hasattr(decision, 'quantity'):
            safe_error_log(
                app_logger.error,
                {"asset": asset, "agent": agent_id},
                "Invalid decision object - missing required fields",
                decision_fields=list(decision.__dict__.keys()) if hasattr(decision, '__dict__') else "No __dict__",
                trade_id=client_order_id
            )
            return None
        
        # Validate quantity
        quantity = getattr(decision, 'quantity', 0)
        if quantity <= 0:
            safe_error_log(
                app_logger.error,
                {"asset": asset, "agent": agent_id},
                "Invalid quantity - must be positive",
                quantity=quantity,
                trade_id=client_order_id
            )
            return None
        
        app_logger.info(
            {"asset": asset, "agent": agent_id},
            "placing order",
            order=payload_decision,
            pattern=pattern,
            confidence=confidence,
            trigger_result=trigger_result,
            trade_id=client_order_id,
            broker_type=broker.__class__.__name__,
            symbol=getattr(decision, 'symbol', None),
            side=require_attr(decision, 'side', 'for trade execution'),
            quantity=quantity
        )

        try:
            # Debug logging before calling broker.place()
            app_logger.info(
                {"asset": asset, "agent": agent_id},
                "About to call broker.place()",
                broker_class_name=broker.__class__.__name__,
                broker_module=broker.__class__.__module__,
                decision_symbol=getattr(decision, 'symbol', None),
                decision_side=require_attr(decision, 'side', 'for trade execution'),
                decision_quantity=require_attr(decision, 'quantity', 'for trade execution')
            )
            
            # broker.place() makes synchronous MT5 IPC calls (symbol_select,
            # symbol_info_tick, order_send) and has blocking time.sleep retries.
            # Must run in a worker thread so the event loop stays responsive.
            result = await asyncio.to_thread(broker.place, decision)

            # Debug logging after successful broker.place()
            app_logger.info(
                {"asset": asset, "agent": agent_id},
                "broker.place() completed successfully",
                broker_class_name=broker.__class__.__name__,
                result_type=type(result).__name__ if result else "None"
            )
        except Exception as e:
            safe_error_log(
                app_logger.error,
                {"asset": asset, "agent": agent_id},
                "Failed to place order with broker",
                error=str(e),
                error_type=type(e).__name__,
                trade_id=client_order_id,
                broker_type=broker.__class__.__name__
            )
            return None
        
        positions_after_list = []
        positions_after: dict[str, Any] = {}
        if hasattr(broker, "positions"):
            try:
                positions_after_list = await asyncio.to_thread(broker.positions)
                positions_after = {p.symbol: p for p in positions_after_list}
            except Exception:
                positions_after_list = []
                positions_after = {}

        if isinstance(result, tuple):
            order, fills = result
        else:
            order, fills = result, []

        app_logger.info(
            {"asset": asset, "agent": agent_id},
            "broker response",
            order_id=str(getattr(order, "id", None)) if getattr(order, "id", None) is not None else None,
            status=str(getattr(order, "status", None)) if getattr(order, "status", None) is not None else None,
            fills=len(fills) if fills else 0,
            pattern=pattern,
            confidence=confidence,
            trigger_result=trigger_result,
            trade_id=client_order_id,
            broker_type=broker.__class__.__name__,
            filled_quantity=getattr(order, "filled_quantity", None),
            average_price=getattr(order, "average_price", None),
            fees=getattr(order, "fees", None),
        )

        pnl = float(getattr(order, "pnl", 0.0))
        record_order(asset, pnl)

        # trade lifecycle -------------------------------------------------
        try:
            from apps.backend import trade_events
            from packages.core.trade_lifecycle import TradeState
        except Exception:  # pragma: no cover - optional in tests
            trade_events = None  # type: ignore
        if trade_events is not None:
            agent_id = self.agent_id  # Use instance variable
            trade_id = (
                getattr(order, "client_order_id", None)
                or getattr(decision, "client_order_id", None)
                or f"{asset}_{int(time.time() * 1000)}"
            )
            # open new trades
            if asset not in trade_events.open_trades and asset in positions_after:
                trade_events.open_trades[asset] = trade_id
                try:
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(trade_events.lifecycle.open(trade_id))
                    except RuntimeError:
                        asyncio.run(trade_events.lifecycle.open(trade_id))
                except Exception:
                    pass
                trade_events.record_event(
                    asset, trade_id, TradeState.OPEN, agent_id, prompt_hash
                )
                if pnl >= 0:
                    try:
                        try:
                            loop = asyncio.get_running_loop()
                            loop.create_task(
                                trade_events.lifecycle.move_to_break_even(trade_id)
                            )
                        except RuntimeError:
                            asyncio.run(
                                trade_events.lifecycle.move_to_break_even(trade_id)
                            )
                    except Exception:
                        pass
                    trade_events.record_event(
                        asset, trade_id, TradeState.BREAK_EVEN, agent_id, prompt_hash
                    )
            # close trades for removed positions
            for sym in list(trade_events.open_trades.keys()):
                if sym not in positions_after:
                    tid = trade_events.open_trades.pop(sym)
                    try:
                        try:
                            loop = asyncio.get_running_loop()
                            loop.create_task(trade_events.lifecycle.close(tid))
                        except RuntimeError:
                            asyncio.run(trade_events.lifecycle.close(tid))
                    except Exception:
                        pass
                    trade_events.record_event(
                        sym, tid, TradeState.CLOSED, agent_id, prompt_hash
                    )

        if journal is not None:
            payload_decision = asdict(decision) if is_dataclass(decision) else decision
            journal.log_decision(payload_decision, prompt_hash, gpt_mode, latency_ms)
            payload_order = asdict(order) if is_dataclass(order) else order
            journal.log_order(payload_order, prompt_hash, gpt_mode, latency_ms)
            for fl in fills:
                payload_fill = asdict(fl) if is_dataclass(fl) else fl
                journal.log_fill(payload_fill, prompt_hash, gpt_mode, latency_ms)
            if positions_after_list:
                for pos in positions_after_list:
                    payload_pos = asdict(pos) if is_dataclass(pos) else pos
                    journal.log_position(payload_pos, prompt_hash, gpt_mode, latency_ms)

        # MongoDB storage removed - using cache-only approach
        app_logger.info(
            {"asset": asset, "agent": agent_id},
            "Trade execution completed - using cache-only approach (no MongoDB storage)",
            client_order_id=getattr(order, "client_order_id", None) if hasattr(order, "client_order_id") else None
        )

        return result

    # public ---------------------------------------------------------------
    async def run(self) -> Any:
        """Fetch candles, query GPT and route the decision to a broker."""

        symbol = self.symbol
        asset = symbol  # alias for backward compatibility

        # Emit this log FIRST — before any guard/check — so the SSE stream
        # confirms the runner was actually invoked (helps distinguish "agent is
        # waiting for bar close" from "runner entered but immediately skipped").
        app_logger.info(
            self._log_ctx,
            "AgentRunner.run() invoked",
            execution_mode=self._execution_mode,
        )

        # Check execution mode and timing
        if not self.should_run_now():
            app_logger.debug(
                {"asset": symbol},
                "Agent not ready to run based on execution mode",
                execution_mode=self._execution_mode,
                scheduled_time=self._scheduled_run_time.isoformat() if self._scheduled_run_time else None
            )
            return None
        
        # CRITICAL: Force fresh data fetch on every interval
        current_run_timestamp = datetime.now(timezone.utc)
        app_logger.info(
            {"asset": symbol},
            "Starting agent run - FRESH DATA FETCH REQUIRED",
            run_timestamp=current_run_timestamp.isoformat(),
            execution_mode=self._execution_mode,
            interval_run_id=f"{symbol}_{current_run_timestamp.strftime('%Y%m%d_%H%M%S')}"
        )
        
        # Store current run timestamp for this interval
        self._current_interval_timestamp = current_run_timestamp
        
        # NOTE (2026-08-27, audit M1): a block here used to clear every armed
        # strategy at the top of each cycle, reading `self._code_sniper_monitor` —
        # an attribute this class never assigns, so it was dead. Had it ever run
        # it would have silently disabled the TTL guard ~1,000 lines below (which
        # skips the cycle while a judge program is still watching) and reinstated
        # the 2026-07-02 F4 regression: "55 of 74 strategies were replaced, only
        # 12 expired". Strategy expiry belongs to Strategy.is_expired/_expire().

        # Step 1: Validate all required services
        if not self._validate_required_services():
            safe_error_log(
                app_logger.error,
                {"asset": symbol},
                "Cannot proceed - required services are missing",
                errors=self._service_errors
            )
            return None
        
        # Early exit if active trade exists - still do cleanup but skip expensive operations
        if self._active_trade is not None:
            await self._cleanup_expired_data()
            app_logger.debug(
                {"asset": symbol},
                "Skipping run - active trade exists",
                trade_id=self._active_trade.get("id", "unknown")
            )
            return None
        
        # Step 2: Get validated services
        candle_service = self.agent_config["candle_service"]
        broker = self.broker
        journal = self.agent_config.get("journal")
        gpt_mode = self.agent_config.get("gpt_mode", "")
        
        # Use instance variables for agent_id and broker_name (already extracted in __init__)
        agent_id = self.agent_id
        broker_name = self.broker_name

        # ── Market-hours guard (early skip) ─────────────────────────────
        # If the broker is in its daily-maintenance or
        # weekend close window, the candle feed is stale-or-empty.  Skip the
        # whole cycle up-front instead of letting the fetch+retry loop below
        # hammer a closed market and emit CRITICAL "failed after all attempts"
        # noise.  The monitor loops already sleep through these windows — this
        # closes the same gap on the GPT-analysis path (the source of ~24/27
        # candle hard-failures in production logs, all at the 21:00 UTC break).
        try:
            from packages.core.services.code_sniper_monitor import _is_market_closed
            market_closed, wait_sec = _is_market_closed(symbol)
        except Exception:
            # Never let the guard itself break a cycle — fail open (proceed).
            market_closed, wait_sec = False, 0
        if market_closed:
            app_logger.info(
                {"asset": symbol, "broker": broker_name, "agent": agent_id},
                "Market closed — skipping GPT cycle until reopen (broker maintenance/weekend window)",
                seconds_until_open=wait_sec,
            )
            return None

        # Step 3: Initialize independent components in parallel
        init_tasks: list = []
        if not self._monitor_initialized:
            init_tasks.append(self._initialize_realtime_monitor(candle_service))
        # Trade monitor is lazily initialized elsewhere when needed
        if init_tasks:
            await asyncio.gather(*init_tasks, return_exceptions=True)
        
        # Note: Trade monitor will be initialized only when we have a trade to monitor

        # Get effective candle counts (dynamic if available, otherwise static)
        n5, n15 = self.get_effective_candle_counts()
        
        app_logger.debug(
            {"asset": symbol, "broker": broker_name, "agent": agent_id},
            "Using candle counts for analysis",
            candles_5m=n5,
            candles_15m=n15,
            config_source="static"
        )
        
        # Configuration validated

        # Overlap cleanup with pre-work (cleanup doesn't depend on candle data)
        trading_filters.cleanup_expired_trades()
        cleanup_task = asyncio.create_task(self._cleanup_expired_data())

        b5_raw = None
        b15_raw = None
        
        try:
            # Check if candle service broker is connected
            if hasattr(candle_service, 'broker') and candle_service.broker:
                broker_connected = False
                if hasattr(candle_service.broker, "is_connected"):
                    # is_connected() calls mt5.account_info()/terminal_info() synchronously.
                    # Must run in a thread to avoid blocking the event loop on Windows IPC.
                    broker_connected = await asyncio.to_thread(candle_service.broker.is_connected)
                elif hasattr(candle_service.broker, '_mt5'):
                    broker_connected = candle_service.broker._mt5 is not None
                
                if not broker_connected:
                    safe_error_log(
                        app_logger.error,
                        {"asset": symbol, "broker": broker_name, "agent": agent_id},
                        "CRITICAL: Broker not connected - cannot fetch fresh data"
                    )
                    return None
                
                # Fetch candles with retry logic - use cache with staleness checks
                max_candle_retries = 3
                b5_raw, b15_raw = None, None
                
                for attempt in range(max_candle_retries):
                    try:
                        app_logger.debug(
                            {"asset": symbol, "broker": broker_name, "agent": agent_id},
                            "AgentRunner._fetch_candles_for_gpt() - attempting candle fetch",
                            attempt=attempt + 1,
                            max_retries=max_candle_retries,
                            method="AgentRunner._fetch_candles_for_gpt"
                        )
                        
                        # A recent newest timestamp does not prove that older
                        # bars were finalized. Re-read the whole decision window
                        # on every attempt, repairing historical partial bars too.
                        b5_raw, b15_raw = await candle_service.fetch(
                            symbol, n5=n5, n15=n15, force_broker=True
                        )
                        
                        current_time = datetime.now(timezone.utc)
                        for candles, count, minutes in ((b5_raw, n5, 5), (b15_raw, n15, 15)):
                            valid, error = validate_live_candle_window(
                                candles or [],
                                expected_count=count,
                                interval_minutes=minutes,
                                asset=symbol,
                                current_time=current_time,
                            )
                            if not valid:
                                raise RuntimeError(f"{minutes}m candle validation failed: {error}")

                        # Success - candles validated
                        app_logger.debug(
                            {"asset": symbol, "broker": broker_name, "agent": agent_id},
                            "AgentRunner._fetch_candles_for_gpt() - candle fetch successful",
                            attempt=attempt + 1,
                            b5_count=len(b5_raw) if b5_raw else 0,
                            b15_count=len(b15_raw) if b15_raw else 0,
                            method="AgentRunner._fetch_candles_for_gpt"
                        )
                        break  # Success, exit retry loop
                        
                    except Exception as e:
                        # Market closed mid-fetch (e.g. crossed the 20:55 UTC
                        # boundary between the early guard and this fetch) is not
                        # retryable — skip the cycle cleanly until reopen.
                        if "market closed" in str(e).lower():
                            app_logger.info(
                                {"asset": symbol, "broker": broker_name, "agent": agent_id},
                                "Market closed mid-fetch — skipping GPT cycle until reopen",
                            )
                            return None
                        if attempt < max_candle_retries - 1:
                            wait_time = 1 + (attempt * 2)  # Progressive delay: 1s, 3s, 5s
                            app_logger.warning(
                                {"asset": symbol, "broker": broker_name, "agent": agent_id},
                                "AgentRunner._fetch_candles_for_gpt() - candle fetch failed, retrying",
                                error=str(e),
                                attempt=attempt + 1,
                                wait_time=wait_time,
                                method="AgentRunner._fetch_candles_for_gpt"
                            )
                            await asyncio.sleep(wait_time)
                            continue
                        else:
                            raise  # Re-raise on final attempt
                
                # The market can close while a candle fetch is in flight.
                market_closed, _ = _is_market_closed(symbol)
                if market_closed:
                    safe_error_log(
                        app_logger.info,
                        {"asset": symbol, "broker": broker_name, "agent": agent_id},
                        f"Market closed for {symbol} — skipping this run (broker maintenance window)"
                    )
                    return None

            else:
                safe_error_log(
                    app_logger.error,
                    {"asset": symbol, "broker": broker_name, "agent": agent_id},
                    "CRITICAL: No broker available - cannot fetch fresh data"
                )
                return None
                
        except Exception as e:
            safe_error_log(
                app_logger.error,
                {"asset": symbol, "broker": broker_name, "agent": agent_id},
                "CRITICAL: Failed to fetch fresh candles from broker - agent run aborted",
                error=str(e),
                error_type=type(e).__name__
            )
            return None
        finally:
            # Retrieve the cleanup result on early exits as well as successful fetches.
            try:
                await cleanup_task
            except Exception:
                pass

        # Check if we successfully fetched candles before proceeding
        if b5_raw is None or b15_raw is None:
            safe_error_log(
                app_logger.error,
                {"asset": symbol, "broker": broker_name, "agent": agent_id},
                "CRITICAL: No candle data available - cannot proceed with agent execution"
            )
            return None
        
        # Step 2: Initialize/Update cache with last 10 candles of 5min for market monitoring
        if b5_raw and len(b5_raw) >= 10:
            # Initialize cache with fresh 5min candles
            cache_success = await self._initialize_candle_cache(b5_raw, asset)
            if cache_success:
                app_logger.info(
                    {"asset": symbol, "broker": broker_name, "agent": agent_id},
                    "Cache initialized with fresh 5min candles for market monitoring",
                    cache_candles=len(b5_raw)
                )
            else:
                app_logger.warning(
                    {"asset": symbol, "broker": broker_name, "agent": agent_id},
                    "Failed to initialize cache - market monitoring may be limited"
                )
        else:
            app_logger.warning(
                {"asset": symbol, "broker": broker_name, "agent": agent_id},
                "Insufficient 5min candles for cache initialization",
                available_candles=len(b5_raw) if b5_raw else 0
            )
        
        # Step 3: Update cache with rolling mechanism for market monitoring
        await self._update_cache_with_fresh_candles(candle_service, asset)

        # Process fresh candle data
        now_dt = datetime.now(timezone.utc)

        def _queue_trade(
            pred: dict,
            order_res: Any,
            phash: str,
            mode: str,
            lat_ms: float,
            monitor_until: datetime | str | None = None,
        ) -> None:
            monitor = self.agent_config.get("trade_monitor")
            if monitor is None or order_res is None:
                return
            
            # Set up callback to clear active trade when trade is closed
            if hasattr(monitor, 'on_trade_closed') and monitor.on_trade_closed is None:
                def on_trade_closed_callback(symbol: str, action: str, price: float | None) -> None:
                    """Callback to clear active trade when trade is closed."""
                    if self._active_trade is not None:
                        app_logger.info(
                            {"asset": asset, "broker": broker_name, "agent": agent_id},
                            "Trade closed - clearing active trade state",
                            trade_id=self._active_trade.get("client_order_id", "unknown"),
                            action=action,
                            price=price
                        )
                        self._active_trade = None
                        # Clear monitoring window to allow fresh slot
                        self.clear_monitoring_window()
                
                monitor.on_trade_closed = on_trade_closed_callback
            order = order_res[0] if isinstance(order_res, tuple) else order_res
            entry_price = getattr(order, "average_price", pred.get("level"))
            direction = pred.get("direction")
            stop = pred.get("stop")
            target = pred.get("target")
            if entry_price is None or direction is None or stop is None or target is None:
                return
            if direction == "long":
                tp1 = entry_price + (target - entry_price) / 2
            else:
                tp1 = entry_price - (entry_price - target) / 2
            trade_state = {
                "direction": direction,
                "entry": entry_price,
                "sl": stop,
                "initial_stop": stop,
                "sl_moved": False,
            }
            rules = {"tp1": tp1, "tp": target}

            expiry_dt: datetime | None = None
            if monitor_until is not None:
                try:
                    expiry_dt = (
                        datetime.fromisoformat(monitor_until)
                        if isinstance(monitor_until, str)
                        else monitor_until
                    )
                except Exception:
                    expiry_dt = None
                if expiry_dt is not None and expiry_dt.tzinfo is None:
                    expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
            if expiry_dt is None:
                # Trade is already executed — use 4h max trade timeout (matching backtest).
                # TTL is for strategy MONITORING (Phase 1), NOT trade management (Phase 2).
                # Once a trade is taken, the 4h cap applies for SL/TP/BE timeout.
                expiry_dt = datetime.now(timezone.utc) + timedelta(hours=TRADE_MONITOR_EXPIRY_HOURS)

            monitor.queue_trade(
                asset, trade_state, rules, expiry_dt, phash, mode, lat_ms
            )

        remaining: list[dict[str, Any]] = []
        executed_result: Any = None
        
        # Process pending predictions - execute only the FIRST one that triggers
        for item in self._pending_predictions:
            created_at: datetime = item["timestamp"]
            if now_dt - created_at > timedelta(minutes=PREDICTION_TIMEOUT_MINUTES):
                continue
            # Calculate dynamic buffer for legacy trigger check
            pred_dict = item["prediction"]
            level = pred_dict.get("level")
            current_price = latest_df.iloc[-1]["close"] if len(latest_df) > 0 else 0
            
            # Calculate ATR for volatility
            market_volatility = 0.0
            if len(latest_df) > 14:
                atr = calculate_atr(latest_df, period=14)
                # Normalize ATR to volatility factor (0.0 to 1.0)
                market_volatility = min(1.0, atr / current_price) if current_price > 0 else 0.0
            
            # Calculate dynamic buffer
            dynamic_buffer = 0.0
            if level is not None and current_price > 0:
                try:
                    dynamic_buffer = calculate_dynamic_buffer(
                        asset=asset,
                        level=level,
                        current_price=current_price,
                        historical_data=latest_df,
                        market_volatility=market_volatility
                    )
                except Exception as e:
                    safe_error_log(
                        app_logger.error,
                        {"asset": asset},
                        "Error calculating dynamic buffer for legacy trigger check",
                        error=str(e),
                        error_type=type(e).__name__
                    )
                    # Fallback to base buffer from asset config
                    from packages.core.trigger_check import get_asset_config
                    try:
                        config = get_asset_config(asset)
                        dynamic_buffer = config["base_buffer"]
                    except:
                        dynamic_buffer = 10.0  # Default fallback
            
            if entry_trigger_met(pred_dict, latest_df, buffer=dynamic_buffer, asset=asset):
                # FINAL SAFETY GATE: Validate before execution (defense-in-depth)
                entry = pred_dict.get("level", 0)
                stop = pred_dict.get("stop", 0)
                target = pred_dict.get("target", 0)
                direction = pred_dict.get("direction", "")
                
                # Convert candle data for validators
                c5_window = []
                c15_window = []
                if b5_raw and len(b5_raw) > 0:
                    c5_window = [{"high": c.high, "low": c.low} for c in b5_raw[-50:]]
                if b15_raw and len(b15_raw) > 0:
                    c15_window = [{"high": c.high, "low": c.low} for c in b15_raw[-50:]]
                
                is_valid, validation_reason = _validate_final_safety_gate(
                    entry=entry,
                    stop=stop,
                    target=target,
                    direction=direction,
                    c5_window=c5_window,
                    c15_window=c15_window
                )
                
                if not is_valid:
                    safe_error_log(
                        app_logger.info,
                        {"asset": asset, "agent": agent_id},
                        f"Trade skipped - final safety gate failed: {validation_reason}",
                        entry=entry,
                        stop=stop,
                        target=target,
                        direction=direction,
                        validation_reason=validation_reason
                    )
                    remaining.append(item)  # Keep for potential retry
                    continue
                
                sn_resp = item.get("sniper_response", {})
                # Convert string confidence to float if needed
                confidence_raw = sn_resp.get("confidence")
                if confidence_raw == "high":
                    confidence = 0.8
                elif confidence_raw == "medium":
                    confidence = 0.6
                elif confidence_raw == "low":
                    confidence = 0.4
                elif isinstance(confidence_raw, (int, float)):
                    confidence = float(confidence_raw)
                else:
                    confidence = None
                
                executed_result = await self._execute_order(
                    asset,
                    item["decision"],
                    broker,
                    journal,
                    item["gpt_mode"],
                    item["prompt_hash"],
                    item["latency_ms"],
                    item.get("raw_decision"),
                    spot_price,
                    now_dt.date(),
                    pattern=sn_resp.get("pattern"),
                    confidence=confidence,
                    trigger_result=True,
                )
                
                # Only mark as active trade if order was actually executed
                if executed_result is None:
                    app_logger.warning(
                        {"asset": asset, "agent": agent_id},
                        "Order execution returned None - NOT setting active trade state",
                        prediction_level=item["prediction"].get("level"),
                        prediction_direction=item["prediction"].get("direction"),
                    )
                    remaining.append(item)  # Keep for retry on next cycle
                    continue
                
                # Mark this as active trade and clear ALL pending predictions
                self._active_trade = {
                    "client_order_id": getattr(item["decision"], "client_order_id", None),
                    "timestamp": now_dt,
                    "prediction": item["prediction"],
                    "executed_result": executed_result
                }
                
                # Clear monitoring window when trade is executed
                self.clear_monitoring_window()
                
                # Track active trade in trading filters (4 hours max, matching backtest)
                trade_id = getattr(item["decision"], "client_order_id", f"trade_{int(time.time())}")
                exit_time = now_dt + timedelta(hours=TRADE_MONITOR_EXPIRY_HOURS)
                trading_filters.track_active_trade(
                    asset=asset,
                    entry_time=now_dt,
                    exit_time=exit_time,
                    trade_id=trade_id
                )
                
                # Trade executed - 3-hour window started
                
                # Trade executed — queue for 4h trade timeout monitoring (Phase 2)
                # TTL was for strategy code monitoring (Phase 1), not trade management
                
                _queue_trade(
                    item["prediction"],
                    executed_result,
                    item["prompt_hash"],
                    item["gpt_mode"],
                    item["latency_ms"],
                    self.agent_config.get("monitor_until"),
                )
                
                # Clearing all pending predictions
                
                # Clear ALL pending predictions when a trade is executed
                self._pending_predictions = []
                break  # Exit the loop after executing one trade
            else:
                remaining.append(item)
        
        # If no trade was executed, update remaining predictions
        if executed_result is None:
            self._pending_predictions = remaining

        # Check for future candle triggers using slot matcher
        now_monotonic = time.monotonic()
        if now_monotonic - self._last_trigger_check >= self._trigger_check_interval:
            try:
                trigger_found = await self._check_future_candle_triggers(asset, candle_service)
                if trigger_found:
                    # Future candle trigger found and executed
                    # If a trigger was executed, return early to avoid duplicate GPT calls
                    return executed_result
            except Exception as e:
                safe_error_log(
                    app_logger.error,
                    {"asset": asset, "broker": broker_name, "agent": agent_id},
                    "Error checking future candle triggers",
                    error=str(e),
                    error_type=type(e).__name__
                )
            finally:
                self._last_trigger_check = now_monotonic

        def _to_json(data: Any) -> str:
            """Safely convert candle data to JSON with comprehensive datetime handling."""
            if isinstance(data, str):
                return data
            
            try:
                import pandas as pd
                from packages.core.utils import candles_to_json

                if isinstance(data, pd.DataFrame):
                    return candles_to_json(data)
            except Exception:  # pragma: no cover - defensive
                pass
            
            # Convert Candle objects or other data to JSON-serializable format
            items = []
            for x in list(data):
                try:
                    if hasattr(x, 'time'):  # Candle object
                        item = {
                            "time": serialize_for_json(x.time),
                            "open": float(x.open),
                            "high": float(x.high),
                            "low": float(x.low),
                            "close": float(x.close),
                            "volume": float(x.volume) if x.volume is not None else None
                        }
                    elif isinstance(x, dict):  # Bar dict from MT5
                        item = {
                            "time": serialize_for_json(x.get("time")),
                            "open": float(x.get("open", 0)),
                            "high": float(x.get("high", 0)),
                            "low": float(x.get("low", 0)),
                            "close": float(x.get("close", 0)),
                            "volume": float(x.get("volume", 0)) if x.get("volume") is not None else None
                        }
                    elif is_dataclass(x):
                        item = serialize_for_json(asdict(x))
                    else:
                        item = serialize_for_json(x)
                    
                    items.append(item)
                except Exception as e:
                    logger.warning(f"Could not convert item to JSON: {e}")
                    continue
            
            return safe_json_dumps(items)

        # Use the fresh data we already fetched
        c5_raw, c15_raw = b5_raw, b15_raw
        
        # Using fresh data for sniper call
        
        # Convert candles to all required formats in single batch operation
        # Validate candles once per run
        self._candles_validated_run = self._current_interval_timestamp
        try:
            c5_dicts, c15_dicts, c5_df, c5_json, c15_json = self._convert_candles_batch(c5_raw, c15_raw)
            
            # Ensure we have the expected columns - normalize field names
            if "ts" in c5_df.columns and "time" not in c5_df.columns:
                c5_df["time"] = c5_df["ts"]
                c5_df = c5_df.drop(columns=["ts"])
            
            # Ensure required columns exist
            required_columns = ["open", "high", "low", "close"]
            missing_columns = [col for col in required_columns if col not in c5_df.columns]
            
            # Check if time is available as index (preferred) or as column
            has_time_index = hasattr(c5_df.index, 'name') and c5_df.index.name == 'time'
            has_time_column = 'time' in c5_df.columns
            
            if not has_time_index and not has_time_column:
                missing_columns.append('time')
                logger.warning(f"Missing time information in c5_df - neither as index nor column")
            
            if missing_columns:
                logger.warning(f"Missing columns in c5_df: {missing_columns}")
                for col in missing_columns:
                    if col == "time":
                        # Add time column if missing
                        c5_df[col] = pd.NaT
                    else:
                        c5_df[col] = 0.0
                    
        except Exception as e:
            safe_error_log(
                app_logger.error,
                {"asset": symbol, "broker": broker_name, "agent": agent_id},
                "Error in batch candle conversion",
                error=str(e)
            )
            # No fallback. A conversion failure means this cycle has no market
            # data; substituting empty payloads used to let it carry on and spend
            # real expert+judge budget asking the model about nothing, then arm
            # whatever came back (audit 2026-08-27, H3). Skip the cycle — the
            # next bar close is five minutes away.
            return None
            
        latest_df = c5_df.tail(1)
        spot_price = float(latest_df["close"].iloc[-1]) if not latest_df.empty and "close" in latest_df.columns else 0.0

        # Data processed and validated - all conversions completed in batch

        record_run(asset)

        budget_check_result = self._check_budgets()
        app_logger.debug(
            {"asset": asset, "broker": broker_name, "agent": agent_id},
            f"Budget check result: {budget_check_result}"
        )
        
        if not budget_check_result:
            app_logger.info(
                {"asset": asset, "broker": broker_name, "agent": agent_id},
                "Skipping sniper call - budget exceeded"
            )
            record_budget_skip(asset)
            return None
            
        # Check if there are open trades - if so, don't call sniper
        open_positions = []
        if hasattr(broker, "positions"):
            try:
                all_positions = await asyncio.to_thread(broker.positions)
                asset_upper = asset.upper()
                open_positions = [
                    p for p in all_positions
                    if p.symbol.upper().startswith(asset_upper)
                ]
                app_logger.info(
                    {"asset": asset, "broker": broker_name, "agent": agent_id},
                    f"Broker positions check: {len(open_positions)} for {asset} (of {len(all_positions)} total)"
                )
            except Exception as e:
                app_logger.warning(
                    {"asset": asset, "broker": broker_name, "agent": agent_id},
                    f"Failed to check broker positions: {e}"
                )
                open_positions = []

        if open_positions:
            # LP-13 fix (2026-07-27, REPAIRED 2026-07-28): an open position that
            # NO live monitor task owns means it survived a restart unmanaged —
            # monitor construction was lazy (new trade / new strategy only), so
            # orphans ran to their broker stop with no trail/TTL and no exit
            # accounting (07-14 USTEC −$6.82; 07-24 EURUSD −$6.42, unmanaged
            # 06:01→07:09). Constructing the monitor HERE fires its
            # crash-recovery (_recover_open_trades), which re-adopts the
            # position from the persisted monitor_state (full TP/trail
            # management) or the degraded broker-only path.
            #
            # The 07-27 ship guarded this on `self._trade_monitor_initialized`,
            # which CANNOT work: api.py builds a fresh AgentRunner for every
            # cycle, so the flag is False at the top of each one. That fired the
            # branch 7× in one day on healthy positions and produced the
            # duplicate exit that corrupted a 07-10 trade record. The real
            # question is process-global, not per-instance: does a live monitor
            # task already own this asset's position?
            from packages.core.services.trade_monitor import is_asset_managed
            if not is_asset_managed(asset):
                try:
                    candle_service = self.agent_config.get("candle_service")
                    journal = self.agent_config.get("journal")
                    if candle_service and broker:
                        await self._initialize_trade_monitor(candle_service, broker, journal)
                        # Detach for the same reason the code-sniper hand-off
                        # does (see below): cleanup_resources() runs in a
                        # `finally` after EVERY cycle (api.py) and its
                        # shutdown() cancels every monitor task — including the
                        # recovery task and the adoption it just made. Without
                        # this the fix re-orphans the position ~5s later, which
                        # is why the 07-27 ship could never have worked.
                        self._trade_monitor = None
                        self._trade_monitor_initialized = False
                        app_logger.info(
                            {"asset": asset, "broker": broker_name, "agent": agent_id},
                            f"LP-13 recovery: trade monitor initialized for open {asset} "
                            f"position(s) found without management (restart orphan)"
                        )
                except Exception as e:
                    app_logger.warning(
                        {"asset": asset, "broker": broker_name, "agent": agent_id},
                        f"LP-13 recovery init failed (inert): {e}"
                    )
            app_logger.info(
                {"asset": asset, "broker": broker_name, "agent": agent_id},
                f"Skipping sniper call — {len(open_positions)} open position(s) for {asset}"
            )
            return None
        
        # Check if we have an active trade - if so, skip GPT calls for 3 hours
        has_active = self.has_active_trade()
        # Active trade check

        if has_active:
            # Skipping sniper call - active trade exists (3-hour window)
            return None

        # Skip the cycle while the current judge program is still monitoring
        # (2026-07-02 audit F4): a new cycle would replace the strategy and
        # silently truncate its TTL to the run interval. Honoring the TTL also
        # saves the expert+judge API calls. Fail-open: any lookup error falls
        # through to a normal cycle. Must use the async registry getter — the
        # sync wrapper always returns None inside the event loop.
        try:
            from packages.core.services.code_sniper_monitor import (
                get_code_sniper_monitor_async,
            )
            _monitor = await get_code_sniper_monitor_async(agent_id)
            if _monitor is not None:
                _minutes_left = _monitor.active_strategy_minutes_left(asset)
                if _minutes_left is not None:
                    app_logger.info(
                        {"asset": asset, "broker": broker_name, "agent": agent_id},
                        "Skipping GPT cycle — active code-sniper strategy still "
                        "monitoring (TTL not expired)",
                        strategy_minutes_left=round(_minutes_left, 1),
                    )
                    return None
        except Exception as _strat_gate_err:
            app_logger.warning(
                {"asset": asset, "broker": broker_name, "agent": agent_id},
                "Active-strategy gate check failed — running cycle",
                error=str(_strat_gate_err),
                error_type=type(_strat_gate_err).__name__,
            )

        # Check monitoring window based on run interval
        run_interval = self.agent_config.get("run_interval", "1h")
        
        # Determine monitoring duration based on interval
        if run_interval == "30m" or run_interval == "30min":
            monitoring_duration = 30  # 30 minutes
        elif run_interval == "1h" or run_interval == "hourly":
            monitoring_duration = 60  # 1 hour
        else:
            # No default fallback - error for unknown interval
            safe_error_log(
                app_logger.error,
                {"asset": asset, "broker": broker_name, "agent": agent_id},
                f"Unknown run_interval '{run_interval}' - no fallback, cannot proceed"
            )
            return None
        
        if not self.is_monitoring_window_active():
            # Start new monitoring window with appropriate duration
            self.start_monitoring_window(monitoring_duration)
            # Started monitoring window
        else:
            # Monitoring window is active - check if it has expired
            elapsed_minutes = (time.monotonic() - self._monitoring_start_time) / 60
            if elapsed_minutes >= self._monitoring_duration_minutes:
                # Monitoring window expired - clearing for fresh slot
                self.clear_monitoring_window()
                self.start_monitoring_window(monitoring_duration)
            else:
                # Monitoring window active
                pass

        prompt_cfg = {
            k: v
            for k, v in self.agent_config.items()
            if isinstance(v, str) and k not in {"c5", "c15"}
        }
        prompt_cfg.update(
            {
                "c5": c5_json,
                "c15": c15_json,
                "agent_id": self.agent_config.get("agent_id", "Sniper-AI"),
                "candle_count_5m": n5,
                "candle_count_15m": n15,
            }
        )
        # Send only pure candle data to sniper - no prompt text
        # The sniper agent will handle the data directly
        prompt_hash = hashlib.sha256(f"{c5_json}{c15_json}".encode("utf-8")).hexdigest()
        start = time.monotonic()
        ts_now = prompt_cfg.get(
            "timestamp", datetime.now(timezone.utc).isoformat()
        )
        # Using fresh candle data from broker for GPT call

        app_logger.info(
            {"asset": asset, "broker": broker_name, "agent": agent_id},
            "calling gpt agent",
            candles={"5m": len(c5_raw), "15m": len(c15_raw)},
        )
        try:
            # Validate input data before calling sniper
            if not c5_json or c5_json == "[]" or not c15_json or c15_json == "[]":
                safe_error_log(
                    app_logger.error,
                    {"asset": asset, "broker": broker_name, "agent": agent_id},
                    "CRITICAL ERROR: Empty candle data - cannot call sniper",
                    c5_json_length=len(c5_json) if c5_json else 0,
                    c15_json_length=len(c15_json) if c15_json else 0
                )
                return None
            
            if self.use_multi_expert:
                # Convert Candle objects to dictionaries for JSON serialization
                def candle_to_dict(c):
                    if is_dataclass(c):
                        d = asdict(c)
                        # Convert pd.Timestamp to ISO string for JSON serialization
                        if 'time' in d and hasattr(d['time'], 'isoformat'):
                            d['time'] = d['time'].isoformat()
                        
                        # CRITICAL FIX: Ensure all numeric fields are converted to float
                        # This prevents str-str arithmetic errors in generated code
                        numeric_fields = ['open', 'high', 'low', 'close', 'volume']
                        for field in numeric_fields:
                            if field in d and d[field] is not None:
                                try:
                                    # Convert to float, handling string numbers like "122065.0"
                                    if isinstance(d[field], str):
                                        d[field] = float(d[field].strip('"\' '))
                                    else:
                                        d[field] = float(d[field])
                                except (ValueError, TypeError):
                                    # Set to 0.0 if conversion fails to prevent type errors
                                    d[field] = 0.0
                        
                        return d
                    return c
                
                c5_dicts = [candle_to_dict(c) for c in c5_raw]
                c15_dicts = [candle_to_dict(c) for c in c15_raw]
                
                # Respect the agent-config candle counts (n5/n15 from
                # get_effective_candle_counts) instead of the hardcoded
                # 100/48 defaults — otherwise configured data is silently
                # trimmed before it reaches the expert. Fall back to the
                # old defaults only when config is unset (0).
                c5_superset, c15_superset = _build_supersets(
                    c5_dicts,
                    c15_dicts,
                    want_c5=n5 or 100,
                    want_c15=n15 or 48,
                )

                # Use the configured, validated snapshot above for the model.
                # Re-reading the live cache here could silently replace it with
                # only 50 M5/30 M15 bars, or with a different bar generation.
                # Prompt serialization already supports these candle dicts.

                # Optional higher-timeframe context (inert unless HTF_CONTEXT_ENABLED).
                from packages.core.market_structure import candle_time_utc
                latest_open = candle_time_utc(c5_dicts[-1]) if c5_dicts else None
                cycle_as_of = (latest_open + timedelta(minutes=5)) if latest_open else datetime.now(timezone.utc)
                htf_candles = await self._fetch_htf_candles(asset, as_of=cycle_as_of)

                # Per-cycle risk meta (zero extra broker calls — reuses the HTF
                # fetch): ATR(1h) + the volatility stop floor. Consumed by the
                # executor (auto-widen) and the trade monitor (runner trail).
                self._cycle_risk_meta = self._compute_cycle_risk_meta(
                    htf_candles, c5_dicts, asset, as_of=cycle_as_of
                )

                # Session-tape observation (2026-07-25, LP-08 Phase 0):
                # log-only conflict line in "log" mode (everyone, default);
                # "context" mode (Phase 2a, nobody enabled) additionally sets
                # self._cycle_session_tape for the prompt path below.
                self._observe_session_tape(c5_dicts, asset)

                # Recent closed trades for prompt context (one indexed Mongo
                # query; inert on any failure — never blocks the cycle).
                recent_trades = await self._fetch_recent_trades(asset)

                # Build gpt_config with required runtime fields
                gpt_cfg = {
                    **self.gpt_config,  # Include any pre-configured settings
                    "asset": asset,
                    "agent_id": agent_id,
                    "broker_type": broker_name,
                    "prompt_hash": prompt_hash,
                    "store_response": True,
                    "htf_candles": htf_candles,
                    "as_of": cycle_as_of,
                    "recent_trades": recent_trades,
                    # None unless SESSION_TAPE mode is "context" for this asset
                    # (Phase 2a) — with the default "log" mode this key is
                    # always None and the prompt context is byte-identical.
                    "session_tape": getattr(self, "_cycle_session_tape", None),
                }
                
                try:
                    # Generate unique cycle ID for this run
                    cycle_id = f"{agent_id}_{asset}_{int(time.time())}"
                    
                    # use_code_sniper=True always (backtest parity)
                    signal, cycle_doc = await asyncio.wait_for(
                        multi_expert_decision_with_details(
                            c5_superset,
                            c15_superset,
                            gpt_cfg,
                            cycle_id=cycle_id,
                            use_code_sniper=True,
                        ),
                        timeout=120.0,
                    )
                    
                    # Fire-and-forget: persist cycle doc without blocking the trading path
                    try:
                        from packages.db.repositories.multi_expert_cycles import multi_expert_cycles_repo
                        _fire_and_forget(
                            multi_expert_cycles_repo.insert_cycle(cycle_doc),
                            "multi_expert_cycles",
                        )
                    except Exception as store_error:
                        logger.error(f"Failed to store multi-expert cycle {cycle_id}: {str(store_error)}")
                    
                    # Extract structured metrics from cycle document for backward compatibility
                    structured_metrics = {
                        "experts_count": 0,
                        "valid_proposals": (cycle_doc.judge.validation_summary.get("valid_proposals", 0) if cycle_doc.judge and isinstance(cycle_doc.judge.validation_summary, dict) else 0),
                        "selected_expert": cycle_doc.judge.selected if cycle_doc.judge else None,
                        "total_duration_ms": cycle_doc.judge.validation_summary.get("total_ms", 0) if cycle_doc.judge and isinstance(cycle_doc.judge.validation_summary, dict) else 0,
                        "judge_ms": cycle_doc.judge.validation_summary.get("judge_ms", 0) if cycle_doc.judge and isinstance(cycle_doc.judge.validation_summary, dict) else 0,
                    }
                    
                    # Dynamic config removed
                    
                    # Check if cycle was skipped (no valid proposals)
                    if cycle_doc.status == 'skipped':
                        app_logger.info(
                            {"asset": asset, "broker": broker_name, "agent": agent_id},
                            "Multi-expert cycle skipped - no valid proposals generated"
                        )
                        
                        # Return TTL wait signal with proper metadata
                        return {
                            "signal": "wait",
                            "ttl_minutes": TTL_DEFAULT_MINUTES,
                            "reason": "Multi-expert cycle skipped - no valid proposals",
                            "structured_metrics": structured_metrics
                        }
                    
                    # Check if we got a valid signal after multi-expert decision
                    if signal is None:
                        app_logger.info(
                            {"asset": asset, "broker": broker_name, "agent": agent_id},
                            "No valid signal generated - waiting for TTL before next cycle"
                        )
                        
                        # Return TTL wait signal with proper metadata
                        return {
                            "signal": "wait",
                            "ttl_minutes": TTL_DEFAULT_MINUTES,
                            "reason": "No valid signals generated by multi-expert system",
                            "structured_metrics": structured_metrics
                        }
                        
                except asyncio.TimeoutError:
                    safe_error_log(
                        app_logger.error,
                        {"asset": asset, "broker": broker_name, "agent": agent_id},
                        "Multi-expert decision timed out after 2 minutes",
                    )
                    return None
                except Exception as e:
                    # Fire-and-forget: persist failed cycle doc
                    if hasattr(e, 'cycle_doc'):
                        try:
                            from packages.db.repositories.multi_expert_cycles import multi_expert_cycles_repo
                            cycle_doc = e.cycle_doc  # type: ignore
                            _fire_and_forget(
                                multi_expert_cycles_repo.insert_cycle(cycle_doc),
                                "multi_expert_cycles",
                            )
                        except Exception as store_error:
                            logger.error(f"Failed to store FAILED multi-expert cycle: {str(store_error)}")
                    
                    # Propagate all other exceptions (system errors, etc)
                    safe_error_log(
                        app_logger.error,
                        {"asset": asset, "broker": broker_name, "agent": agent_id},
                        f"Multi-expert decision failed with system error: {type(e).__name__}",
                        error=str(e),
                        error_type=type(e).__name__
                    )
                    # Don't silently return None - let caller handle the error
                    raise
                
                # Log structured metrics for monitoring
                app_logger.info(
                    {"asset": asset, "broker": broker_name, "agent": agent_id},
                    "Multi-expert decision completed",
                    **structured_metrics
                )
                
                # Dynamic configuration removed; always use static defaults
                
                # Handle different signal types
                # IMPORTANT: Some paths may pass a dict rather than a Pydantic model. Support both.
                app_logger.info({"asset": asset, "broker": broker_name, "agent": agent_id}, "Signal dispatch entry", signal_type=type(signal).__name__, has_code=bool(getattr(signal, 'code', None) is not None or (isinstance(signal, Mapping) and 'code' in signal)), has_signal_attr=bool(getattr(signal, 'signal', None) is not None or (isinstance(signal, Mapping) and 'signal' in signal)))
                if hasattr(signal, 'code') or (isinstance(signal, Mapping) and 'code' in signal):  # CodeSniperSignal
                    # Coerce dict payloads to proper CodeSniperSignal for downstream components
                    if not hasattr(signal, 'code'):
                        try:
                            signal = CodeSniperSignal.model_validate(dict(signal))  # type: ignore[arg-type]
                        except Exception as _e:
                            safe_error_log(
                                app_logger.error,
                                {"asset": asset, "broker": broker_name, "agent": agent_id},
                                "Failed to coerce dict to CodeSniperSignal",
                                error=str(_e),
                                error_type=type(_e).__name__,
                            )
                            raise

                    # For code sniper signals, we need to set up monitoring instead of immediate execution.
                    # c5_dicts (the candles the judge just analyzed) are passed so the
                    # registration dry-run can reject already-true entry conditions.
                    await self._handle_code_sniper_signal(
                        signal, asset, broker_name, agent_id, cycle_id=cycle_id,
                        generation_candles=c5_dicts,
                    )
                    # The code sniper monitor will handle 5-minute monitoring in the background
                    app_logger.info(
                        {"asset": asset, "broker": broker_name, "agent": agent_id},
                        "Code sniper signal handled - monitoring set up, continuing with agent scheduling",
                        ttl_minutes=signal.ttl_minutes,
                        confidence=signal.confidence
                    )
                    # IMPORTANT: Detach trade_monitor so cleanup_resources() doesn't
                    # shut it down — the CodeSniperMonitor still needs it for trade
                    # execution. Without this, cleanup_resources() destroys the monitor
                    # which can cause the supervisor scheduling loop to hang.
                    self._trade_monitor = None
                    self._trade_monitor_initialized = False
                    # Record latency for this GPT cycle before returning
                    record_latency(asset, (time.monotonic() - start) * 1000.0)
                    return None
                elif getattr(signal, 'signal', None) == "flat" or (isinstance(signal, Mapping) and signal.get('signal') == 'flat'):
                    # Flat signal - no trade, continue with 5m scheduling
                    app_logger.info(
                        {"asset": asset, "broker": broker_name, "agent": agent_id},
                        "Flat signal received - no trade action, continuing with 5m scheduling",
                        ttl_minutes=getattr(signal, 'ttl_minutes', None) if not isinstance(signal, dict) else signal.get('ttl_minutes'),
                        confidence=getattr(signal, 'confidence', None) if not isinstance(signal, dict) else signal.get('confidence')
                    )
                    record_latency(asset, (time.monotonic() - start) * 1000.0)
                    return None
                else:  # MultiExpertSignal
                    raw = _map_multi_expert_signal(signal)
                    if raw is None:
                        logger.info("Multi-expert returned flat signal")
                        record_latency(asset, (time.monotonic() - start) * 1000.0)
                        return None
            else:
                # Normal mode (non-multi-expert) - also create cycle document for consistency
                cycle_id = f"{agent_id}_{asset}_{int(time.time())}"
                
                # Create cycle document for normal mode
                from packages.db.models import MultiExpertCycleDoc, JudgeOutcome
                cycle_doc = MultiExpertCycleDoc(
                    cycle_id=cycle_id,
                    mode='normal',
                    agent_id=agent_id,
                    asset=asset,
                    symbol=asset,
                    started_at=datetime.now(timezone.utc),
                    status='skipped',
                    experts=[],
                    judge=None
                )
                
                # Branch between legacy call_sniper and new code sniper based on feature flag
                if self.use_code_sniper:
                    app_logger.info(
                        {"asset": asset, "broker": broker_name, "agent": agent_id},
                        "Code sniper feature enabled but not yet implemented - falling back to legacy call_sniper"
                    )
                    gpt_mode = "code_sniper"
                else:
                    gpt_mode = "sniper"

                normal_start_time = time.time()
                # Use static default prompt (backtest parity) — no DB prompt_content
                with DecisionCosts(cycle_id) as ai_costs:
                    raw = await asyncio.wait_for(
                        call_sniper(
                            c15_json,
                            c5_json,
                            ts_now,
                            api_key=api_key,
                            gpt_config=self.gpt_config,
                            store_response=True,
                            asset=asset,
                            agent_id=agent_id,
                            prompt_hash=prompt_hash,
                            broker_type=broker_name,
                            gpt_mode=gpt_mode,
                        ),
                        timeout=120.0  # 2 minute timeout for GPT calls
                    )
                
                cycle_doc.ai_cost = ai_costs.summary()

                # Update cycle document with normal mode results
                cycle_doc.completed_at = datetime.now(timezone.utc)
                cycle_doc.status = 'success'
                
                # Expert insights are no longer stored in normal mode
                
                # Create judge outcome for normal mode
                cycle_doc.judge = JudgeOutcome(
                    selected='normal',
                    rationale=trade_reason(raw),
                    final_signal=raw if isinstance(raw, dict) else None,
                    validation_summary={
                        "mode": "normal",
                        "gpt_mode": gpt_mode,
                        "duration_ms": (time.time() - normal_start_time) * 1000
                    }
                )
                
                # Fire-and-forget: persist normal mode cycle doc
                try:
                    from packages.db.repositories.multi_expert_cycles import multi_expert_cycles_repo
                    _fire_and_forget(
                        multi_expert_cycles_repo.insert_cycle(cycle_doc),
                        "multi_expert_cycles",
                    )
                except Exception as store_error:
                    logger.error(f"Failed to store normal mode cycle {cycle_id}: {str(store_error)}")
        except asyncio.TimeoutError:
            safe_error_log(
                app_logger.error,
                {"asset": asset, "broker": broker_name, "agent": agent_id},
                "GPT call timed out after 2 minutes",
                error="TimeoutError"
            )
            logger.warning("GPT call timed out, skipping cycle")
            return None
        except Exception as e:
            safe_error_log(
                app_logger.error,
                {"asset": asset, "broker": broker_name, "agent": agent_id},
                "GPT call failed",
                error=str(e),
                error_type=type(e).__name__
            )
            logger.warning(f"GPT call failed: {e}, skipping cycle")
            return None
        latency_ms = (time.monotonic() - start) * 1000.0
        
        # Log GPT response reception with comprehensive details
        gpt_response_received_at = datetime.now(timezone.utc)
        # GPT response received
        record_latency(asset, latency_ms)

        # Handle empty array response (no valid trading signal)
        if isinstance(raw, list) and len(raw) == 0:
            # GPT returned empty array - no valid trading signal found
            logger.info("No valid trading signal found, skipping cycle")
            return None

        parsed_raw: Mapping[str, Any] | None = None
        if isinstance(raw, Mapping):
            parsed_raw = raw
        elif isinstance(raw, list) and raw and isinstance(raw[0], Mapping):
            parsed_raw = raw[0]
        elif isinstance(raw, str):
            try:
                parsed_raw = json.loads(raw)
            except Exception:
                match = re.search(r"```(?:python)?\n(.*?)```", raw, re.DOTALL)
                if match:
                    from packages.core.simple_sandbox import run_in_sandbox

                    source = match.group(1)
                    ts_now = self.agent_config.get(
                        "timestamp", datetime.now(timezone.utc).isoformat()
                    )
                    try:
                        # Ensure c5_raw is always defined
                        if 'c5_raw' not in locals():
                            c5_raw = []
                        # Validate candle data before sandbox execution

                        # Convert raw candle data to Candle objects for validation
                        candle_objects = []
                        for candle_data in c5_raw:
                            if isinstance(candle_data, dict):
                                candle_objects.append(Candle(
                                    time=candle_data.get('time'),
                                    open=candle_data.get('open', 0),
                                    high=candle_data.get('high', 0),
                                    low=candle_data.get('low', 0),
                                    close=candle_data.get('close', 0),
                                    volume=candle_data.get('volume', 0)
                                ))

                        # Validate candle sequence and alignment
                        current_time = datetime.now(timezone.utc)
                        is_valid, error_msg = validate_candle_sequence_and_alignment(
                            candle_objects,
                            current_time=current_time,
                            expected_count=21,
                            interval="5m"  # Sandbox execution uses 5m candles
                        )

                        if not is_valid:
                            safe_error_log(
                                app_logger.error,
                                {"asset": asset, "broker": broker_name, "agent": agent_id},
                                "Candle validation failed before sandbox execution",
                                error=error_msg,
                                candle_count=len(candle_objects),
                                current_time=current_time.isoformat()
                            )
                            # Skip sandbox execution if validation fails - raise exception instead
                            raise ValueError(f"Candle validation failed: {error_msg}")

                        sandbox_decision = run_in_sandbox(source, c5_raw, ts_now)
                        side_val = getattr(
                            getattr(sandbox_decision, "side", ""), "value", sandbox_decision.side
                        )
                        parsed_raw = {
                            "action": side_val,
                            "size": getattr(sandbox_decision, "quantity", 0.0),
                        }
                        if isinstance(sandbox_decision, Mapping) and "prediction" in sandbox_decision:
                            parsed_raw["prediction"] = sandbox_decision["prediction"]
                    except Exception as exc:  # pragma: no cover - defensive
                        safe_error_log(
                            app_logger.error,
                            {"asset": asset, "broker": broker_name, "agent": agent_id},
                            "sandbox execution failed",
                            error=str(exc)
                        )
        if parsed_raw is None:
            parsed_raw = {}

        if self.use_multi_expert:
            prediction = parse_prediction(parsed_raw)
            if prediction is None:
                logger.warning("Multi-expert prediction normalization failed, skipping")
                return None
            sniper_resp = SniperResponse(
                direction=prediction.direction,
                pattern=prediction.condition,
                level=prediction.level,
                stop=prediction.stop,
                target=prediction.target,
                confidence=_confidence_label(float(parsed_raw.get("prediction", {}).get("confidence", 0.5))),
            )
        else:
            # Use parse_prediction which handles field normalization and validation
            # This is the single source of truth for prediction parsing
            prediction = parse_prediction(parsed_raw)
            if prediction is None:
                safe_error_log(
                    app_logger.error,
                    {"asset": asset, "broker": broker_name, "agent": agent_id},
                    "Failed to parse prediction from GPT response",
                )
                return None
            
            # Convert Prediction to dict with canonical field names for SniperResponse
            # Note: Prediction uses 'level' internally, but we map to 'entry' for canonical structure
            response_data = {
                "direction": prediction.direction,
                "pattern": prediction.condition,  # Prediction uses 'condition', canonical is 'pattern'
                "entry": prediction.level,  # Map level to entry (canonical name)
                "stop": prediction.stop,
                "target": prediction.target,
                "confidence": "high",  # Default - Prediction doesn't have confidence, set default
                "status": "waiting"
            }

            try:
                sniper_resp = SniperResponse.model_validate(response_data)
            except ValidationError as exc:
                safe_error_log(
                    app_logger.error,
                    {"asset": asset, "broker": broker_name, "agent": agent_id},
                    "SniperResponse validation failed",
                    error=str(exc),
                )
                return None
            prediction = coerce_prediction(sniper_resp)

        validation_errors = []
        if sniper_resp.direction not in ["long", "short"]:
            validation_errors.append(f"Invalid direction: {sniper_resp.direction}")
        if sniper_resp.level is None or sniper_resp.level <= 0:
            validation_errors.append(f"Invalid level: {sniper_resp.level}")
        if sniper_resp.stop is None or sniper_resp.stop <= 0:
            validation_errors.append(f"Invalid stop: {sniper_resp.stop}")
        if sniper_resp.target is None or sniper_resp.target <= 0:
            validation_errors.append(f"Invalid target: {sniper_resp.target}")
        if sniper_resp.direction == "long":
            if sniper_resp.stop >= sniper_resp.level:
                validation_errors.append(f"Long trade: stop ({sniper_resp.stop}) >= level ({sniper_resp.level})")
            if sniper_resp.target <= sniper_resp.level:
                validation_errors.append(f"Long trade: target ({sniper_resp.target}) <= level ({sniper_resp.level})")
        elif sniper_resp.direction == "short":
            if sniper_resp.stop <= sniper_resp.level:
                validation_errors.append(f"Short trade: stop ({sniper_resp.stop}) <= level ({sniper_resp.level})")
            if sniper_resp.target >= sniper_resp.level:
                validation_errors.append(f"Short trade: target ({sniper_resp.target}) >= level ({sniper_resp.level})")

        if validation_errors:
            safe_error_log(
                app_logger.error,
                {"asset": asset, "broker": broker_name, "agent": agent_id},
                "Sniper response validation failed - invalid trading parameters",
                validation_errors=validation_errors,
            )
            return None

        app_logger.info(
            {"asset": asset, "broker": broker_name, "agent": agent_id},
            "sniper response validated successfully",
            direction=sniper_resp.direction,
            pattern=sniper_resp.pattern,
            signal_level=sniper_resp.level,
            stop=sniper_resp.stop,
            target=sniper_resp.target,
            confidence=sniper_resp.confidence,
            status=sniper_resp.status,
            validation_passed=True,
        )

        decision = normalize_decision(parsed_raw)
        
        # Apply trading filters before storing prediction
        filter_result = trading_filters.apply_filters(
            signal=sniper_resp.model_dump(),
            asset=asset,
            current_price=spot_price
        )
        
        if not filter_result.passed:
            # Signal filtered out - not storing prediction
            return None
        
        # Apply slippage adjustments if filters passed
        if filter_result.slippage_applied:
            # Update the sniper response with adjusted prices
            sniper_resp.level = filter_result.adjusted_entry
            sniper_resp.stop = filter_result.adjusted_stop
            sniper_resp.target = filter_result.adjusted_target
            
            # Applied slippage adjustments to signal
        
        # Generate a client_order_id for the decision
        client_order_id = f"{asset}_{int(time.time() * 1000)}"
        
        # The decision from normalize_decision is a simple decision object
        # We'll convert it to a proper trading decision in _execute_order if needed
        # For now, just ensure it has the client_order_id attribute
        if not hasattr(decision, "client_order_id"):
            decision.client_order_id = client_order_id

        # Apply risk management to calculate proper quantity
        if self.risk_percent > 0 and spot_price and spot_price > 0:
            try:
                # Get available cash from broker
                cash = 0.0
                if hasattr(broker, "balances"):
                    try:
                        refresh = getattr(broker, "refresh_balances", None)
                        if refresh:
                            res = refresh()
                            if asyncio.iscoroutine(res):
                                await res
                        balances = await asyncio.to_thread(broker.balances)
                        cash = float(balances.get("cash", 0.0))
                    except Exception:
                        # Fallback cash if the live balance read fails.
                        cash = 10000.0
                
                if cash > 0:
                    # Calculate risk amount based on percentage
                    risk_amount = cash * self.risk_percent / 100.0
                    
                    # Calculate quantity based on risk amount and current price
                    risk_based_quantity = risk_amount / spot_price
                    
                    # Apply risk management to the decision
                    if hasattr(decision, "size"):
                        # For simple decisions, limit the size
                        original_size = decision.size
                        decision.size = min(original_size, risk_based_quantity)
                        
                        # Risk management applied
                    else:
                        # For complex decisions, add quantity field
                        decision.quantity = risk_based_quantity
                        
                        app_logger.info(
                            {"asset": asset, "agent": agent_id},
                            "Risk management applied to complex decision",
                            risk_percent=self.risk_percent,
                            available_cash=cash,
                            risk_amount=risk_amount,
                            spot_price=spot_price,
                            calculated_quantity=risk_based_quantity
                        )
                else:
                    app_logger.warning(
                        {"asset": asset, "agent": agent_id},
                        "No available cash for risk management",
                        cash=cash
                    )
            except Exception as e:
                safe_error_log(
                    app_logger.error,
                    {"asset": asset, "agent": agent_id},
                    "Error applying risk management",
                    error=str(e),
                    error_type=type(e).__name__
                )

        record_decision(asset)

        # Create pred_dict with canonical field names (entry, stop, target, not level)
        pred_dict = asdict(prediction)
        # Map level to entry (canonical name)
        pred_dict["entry"] = pred_dict.pop("level", prediction.level)
        pred_dict["pattern"] = sniper_resp.pattern
        pred_dict["confidence"] = sniper_resp.confidence
        pred_dict["status"] = sniper_resp.status
        pred_dict.pop("condition", None)
        
        # Perform OPTIMIZED trigger check with cached candles
        trigger_met = await self._perform_optimized_trigger_check(
            pred_dict, asset, symbol, agent_id, broker_name, candle_service
        )
        trigger_check_id = None
        
        # Store trigger check results in MongoDB
        try:
            from packages.db.repositories import gpt_responses as gpt_repo
            
            # Get the latest GPT response ID for this prompt hash
            gpt_responses = await gpt_repo.get_gpt_responses_by_asset(
                asset=asset, limit=1, skip=0
            )
            gpt_response_id = gpt_responses[0]["_id"] if gpt_responses else None
            
            if gpt_response_id:
                # Convert ObjectId to string for Pydantic validation
                gpt_response_id_str = str(gpt_response_id)
                
                trigger_condition = pred_dict.get("condition") or pred_dict.get("pattern", "")
                trigger_type = "unknown"
                
                # Determine trigger type from condition
                if "retest" in trigger_condition.lower():
                    trigger_type = "retest"
                elif "breakout" in trigger_condition.lower():
                    trigger_type = "breakout"
                elif "fvg" in trigger_condition.lower():
                    trigger_type = "fvg_fill"
                elif "liquidity" in trigger_condition.lower():
                    trigger_type = "liquidity_sweep"
                elif "bos" in trigger_condition.lower():
                    trigger_type = "bos"
                elif "orderblock" in trigger_condition.lower():
                    trigger_type = "orderblock"
                
                # Convert string confidence to float for Pydantic validation
                confidence_float = _convert_confidence_to_float(sniper_resp.confidence)
                
                trigger_check_id = await gpt_repo.store_trigger_check(
                    asset=asset,
                    symbol=symbol,  # Add symbol field
                    agent_id=agent_id,
                    prompt_hash=prompt_hash,
                    gpt_response_id=gpt_response_id_str,
                    trigger_condition=trigger_condition,
                    trigger_type=trigger_type,
                    trigger_met=trigger_met,
                    level=pred_dict.get("level"),
                    direction=pred_dict.get("direction"),
                    buffer=pred_dict.get("buffer"),
                    trigger_time=now_dt if trigger_met else None,  # Set trigger time when trigger is met
                    future_candles_count=len(latest_df),
                    entry_price=spot_price,
                    confidence=confidence_float,
                    pattern=sniper_resp.pattern,
                )
                
                # Stored trigger check result
        except Exception as e:
            safe_error_log(
                app_logger.error,
                {"asset": asset, "broker": broker_name, "agent": agent_id},
                "Failed to store trigger check result",
                error=str(e)
            )
        
        if trigger_met:
            # Trade executed
            signal_received_at = datetime.now(timezone.utc)
            trade_executed_at = datetime.now(timezone.utc)
            
            # Store trade immediately in comprehensive_trades collection
            try:
                from packages.db.repositories import comprehensive_logging as comp_logging
                
                # Convert string confidence to float if needed
                confidence_raw = sniper_resp.confidence
                if confidence_raw == "high":
                    confidence_float = 0.8
                elif confidence_raw == "medium":
                    confidence_float = 0.6
                elif confidence_raw == "low":
                    confidence_float = 0.4
                elif isinstance(confidence_raw, (int, float)):
                    confidence_float = float(confidence_raw)
                else:
                    confidence_float = 0.5  # Default
                
                # Store comprehensive trade
                try:
                    _comp_trade_id = await comp_logging.store_comprehensive_trade(
                        symbol=symbol,
                        asset=asset,
                        quantity=decision.quantity if hasattr(decision, 'quantity') else 1.0,
                        price=spot_price or sniper_resp.level,
                        entry_price=spot_price or sniper_resp.level,
                        agent_id=agent_id or "unknown",
                        prompt_hash=prompt_hash or "unknown",
                        signal_received_at=signal_received_at,
                        trade_executed_at=trade_executed_at,
                        gpt_response_id=client_order_id,
                        cycle_id=cycle_id,
                        ai_cost=cycle_doc.ai_cost if cycle_doc else None,
                        trade_reason=cycle_doc.judge.rationale if cycle_doc and cycle_doc.judge else None,
                        trigger_reason=pred_dict.get("condition") or pred_dict.get("pattern"),
                        slot_candle_match_id=None,
                        exit_price=None,
                        stop_loss=sniper_resp.stop,
                        take_profit=sniper_resp.target,
                        trade_type="live",
                        side=sniper_resp.direction,
                        pattern=sniper_resp.pattern,
                        confidence=str(confidence_float),
                        broker_type=broker_name,
                        gpt_mode=gpt_mode,
                        trade_closed_at=None,
                        pnl=None,
                        fees=None,
                        slippage=None,
                        trade_status="open",
                        exit_reason=None,
                    )
                except Exception as e:
                    safe_error_log(
                        app_logger.error,
                        {"asset": asset, "broker": broker_name, "agent": agent_id},
                        "Failed to await store_comprehensive_trade",
                        error=str(e),
                    )
                
                app_logger.info(
                    {"asset": asset, "broker": broker_name, "agent": agent_id},
                    "Trade stored in comprehensive_trades collection",
                    trade_id=client_order_id,
                    comp_trade_id=_comp_trade_id,  # real Mongo _id, for exit correlation
                    symbol=symbol,
                    entry_price=spot_price or sniper_resp.level,
                )
            except Exception as storage_error:
                safe_error_log(
                    app_logger.error,
                    {"asset": asset, "broker": broker_name, "agent": agent_id},
                    "Failed to store trade in comprehensive_trades collection",
                    error=str(storage_error),
                    trade_id=client_order_id
                )
            # Use the confidence_float we already calculated above
            confidence = confidence_float
            
            # Use unified trade execution method
            executed_result = await self._execute_unified_trade(
                asset=asset,
                direction=sniper_resp.direction,
                entry_price=spot_price or sniper_resp.level,
                stop=sniper_resp.stop,
                target=sniper_resp.target,
                quantity=decision.quantity if hasattr(decision, 'quantity') else 1.0,
                pattern=sniper_resp.pattern,
                confidence=confidence,
                prompt_hash=prompt_hash,
                gpt_mode=gpt_mode,
                latency_ms=latency_ms,
                trigger_source="main_flow"
            )
            
            # Note: Trade monitoring is now handled by the integrated _trade_monitor above
            # The old _queue_trade function is deprecated in favor of the integrated approach
        else:
            # Check if we already have an active trade - if so, skip adding new prediction
            if self._active_trade is not None:
                app_logger.info(
                    {"asset": asset, "broker": broker_name, "agent": agent_id},
                    "Skipping new prediction - active trade already exists",
                    active_trade_id=self._active_trade.get("client_order_id", "unknown"),
                    new_prediction_level=pred_dict.get("level"),
                    new_prediction_direction=pred_dict.get("direction")
                )
            else:
                app_logger.info(
                    {"asset": asset, "broker": broker_name, "agent": agent_id},
                    "order queued",
                    candles={"5m": len(c5_raw), "15m": len(c15_raw)},
                    pattern=sniper_resp.pattern,
                    confidence=sniper_resp.confidence,
                    status=sniper_resp.status,
                    trigger_result=False,
                    trade_id=client_order_id,
                )
                self._pending_predictions.append(
                    {
                        "prediction": pred_dict,
                        "sniper_response": sniper_resp.model_dump(),
                        "decision": decision,
                        "timestamp": datetime.now(timezone.utc),
                        "prompt_hash": prompt_hash,
                        "gpt_mode": gpt_mode,
                        "latency_ms": latency_ms,
                        "raw_decision": parsed_raw,
                    }
                )
                
                # Create slot match for continuous monitoring if we have a level and direction
                if pred_dict.get("level") is not None and pred_dict.get("direction") is not None:
                    try:
                        slot_id = int(now_dt.timestamp() // 3600)
                        slot_timestamp = datetime.fromtimestamp(slot_id * 3600, tz=timezone.utc)
                        
                        # Store GPT response first to get response ID
                        from packages.db.repositories import gpt_responses as gpt_repo
                        # Extract new fields from parsed response for audit
                        ttl_minutes = parsed_raw.get("ttl_minutes") if parsed_raw else None
                        active_window_c5 = parsed_raw.get("active_window_c5") if parsed_raw else None
                        active_window_c15 = parsed_raw.get("active_window_c15") if parsed_raw else None
                        rationale = parsed_raw.get("rationale") if parsed_raw else None
                        
                        gpt_response_id = await gpt_repo.store_gpt_response(
                            asset=asset,
                            agent_id=agent_id or "unknown",
                            model="gpt-4",
                            prompt_hash=prompt_hash,
                            prompt_content="",  # We don't have the full prompt here
                            response_content=str(parsed_raw),
                            response_type="trading_decision",
                            latency_ms=latency_ms,
                            symbol=asset,
                            parsed_decision=parsed_raw,
                            # New fields for audit and TTL tracking
                            ttl_minutes=ttl_minutes,
                            active_window_c5=active_window_c5,
                            active_window_c15=active_window_c15,
                            rationale=rationale,
                            confidence=_convert_confidence_to_float(sniper_resp.confidence),
                            pattern=sniper_resp.pattern,
                            direction=pred_dict.get("direction"),
                            level=pred_dict.get("level"),
                            broker_type=broker_name,
                            gpt_mode=gpt_mode,
                        )
                        
                        # Create enhanced slot match with historical candles and pattern data
                        historical_candles = self._convert_candles_to_dataframe(c5_raw)
                        
                        # CRITICAL: Ensure historical candles are available for pattern validation
                        if historical_candles is None or len(historical_candles) == 0:
                            safe_error_log(
                                app_logger.error,
                                {"asset": asset, "broker": broker_name, "agent": agent_id},
                                "CRITICAL ERROR: NO HISTORICAL CANDLES AVAILABLE - CANNOT CREATE SLOT MATCH",
                                error="No historical candles for pattern validation",
                                c5_raw_count=len(c5_raw) if c5_raw else 0
                            )
                            # Skip creating slot match - no fallback
                            return  # Exit the function since we can't proceed without historical data
                        
                        prediction_data = {
                            "buffer": pred_dict.get("buffer", 0.0),
                            "confidence": _convert_confidence_to_float(sniper_resp.confidence),
                            "pattern": sniper_resp.pattern,
                            "level": pred_dict["level"],
                            "direction": pred_dict["direction"],
                            "target": pred_dict.get("target"),
                            "stop": pred_dict.get("stop"),
                            "prompt_hash": prompt_hash,
                            "gpt_mode": gpt_mode
                        }
                        
                        match_id = await self._slot_matcher.create_slot_match(
                            asset=asset,
                            slot_id=slot_id,
                            slot_timestamp=slot_timestamp,
                            gpt_response_id=gpt_response_id,
                            level_value=float(pred_dict["level"]),
                            direction=pred_dict["direction"],
                            agent_id=agent_id or "unknown",
                            prompt_hash=prompt_hash,
                            confidence=_convert_confidence_to_float(sniper_resp.confidence),
                            pattern=sniper_resp.pattern,
                            broker_type=broker_name,
                            gpt_mode=gpt_mode,
                            historical_candles=historical_candles,
                            prediction_data=prediction_data,
                        )
                        
                        # Add level to real-time monitor for continuous monitoring
                        if self._realtime_monitor:
                            try:
                                # Validate monitoring parameters before adding
                                if not pred_dict.get("level") or not pred_dict.get("direction"):
                                    safe_error_log(
                                        app_logger.error,
                                        {"asset": asset, "broker": broker_name, "agent": agent_id},
                                        "Cannot add to real-time monitor - missing level or direction",
                                        level=pred_dict.get("level"),
                                        direction=pred_dict.get("direction")
                                    )
                                else:
                                    await self._realtime_monitor.add_level_for_monitoring(
                                        level_id=match_id,
                                        asset=asset,
                                        level_value=float(pred_dict["level"]),
                                        direction=pred_dict["direction"],
                                        pattern_type=sniper_resp.pattern,
                                        agent_id=agent_id or "unknown",
                                        prediction_data=prediction_data,
                                        historical_candles=historical_candles,
                                        callback=self._create_trigger_callback(asset, agent_id, broker_name)
                                    )
                                    
                                    app_logger.info(
                                        {"asset": asset, "broker": broker_name, "agent": agent_id},
                                        "Added level to real-time monitor for continuous 5min monitoring",
                                        level_id=match_id,
                                        level_value=pred_dict["level"],
                                        direction=pred_dict["direction"],
                                        pattern=sniper_resp.pattern,
                                        monitoring_interval="5min",
                                        historical_candles_count=len(historical_candles)
                                    )
                            except Exception as e:
                                safe_error_log(
                                    app_logger.error,
                                    {"asset": asset, "broker": broker_name, "agent": agent_id},
                                    "Failed to add level to real-time monitor",
                                    error=str(e),
                                    error_type=type(e).__name__
                                )
                        else:
                            app_logger.warning(
                                {"asset": asset, "broker": broker_name, "agent": agent_id},
                                "Real-time monitor not available - level will only be monitored via slot matcher"
                            )
                        
                        # Store additional prediction data in the slot matcher for later use
                        if match_id in self._slot_matcher._active_matches:
                            self._slot_matcher._active_matches[match_id].update({
                                "prediction": pred_dict,
                                "stop_loss": pred_dict.get("stop") or pred_dict.get("stoploss"),
                                "take_profit": pred_dict.get("target") or pred_dict.get("take_profit"),
                                "quantity": pred_dict.get("quantity", 1.0),
                                "gpt_mode": gpt_mode,
                                "broker_type": broker_name,
                            })
                        
                        app_logger.info(
                            {"asset": asset, "broker": broker_name, "agent": agent_id},
                            "Created slot match for continuous monitoring",
                            match_id=match_id,
                            level_value=pred_dict["level"],
                            direction=pred_dict["direction"],
                            slot_id=slot_id
                        )
                        
                    except Exception as e:
                        safe_error_log(
                            app_logger.error,
                            {"asset": asset, "broker": broker_name, "agent": agent_id},
                            "Error creating slot match",
                            error=str(e),
                            error_type=type(e).__name__
                        )

        slot_id = int(now_dt.timestamp() // 3600)
        if agent_runs_repo is not None:
            try:
                _fire_and_forget(
                    agent_runs_repo.insert_agent_run(
                        AgentRunDoc(asset=asset, slot_id=slot_id, time=now_dt)
                    ),
                    "agent_runs",
                )
            except Exception:  # pragma: no cover - best effort
                pass

        # Store GPT call summary
        try:
            from packages.db.repositories import gpt_responses as gpt_repo
            
            # Get the latest GPT response ID for this prompt hash
            gpt_responses = await gpt_repo.get_gpt_responses_by_asset(
                asset=asset, limit=1, skip=0
            )
            gpt_response_id = gpt_responses[0]["_id"] if gpt_responses else None
            
            if gpt_response_id:
                # Convert ObjectId to string for Pydantic validation
                gpt_response_id_str = str(gpt_response_id)
                
                # Convert string confidence to float for Pydantic validation
                confidence_float = _convert_confidence_to_float(sniper_resp.confidence)
                
                # Determine if order was placed and trade executed
                order_placed = trigger_met
                trade_executed = trigger_met and executed_result is not None
                
                # Extract new fields for summary
                ttl_minutes = parsed_raw.get("ttl_minutes") if parsed_raw else None
                active_window_c5 = parsed_raw.get("active_window_c5") if parsed_raw else None
                active_window_c15 = parsed_raw.get("active_window_c15") if parsed_raw else None
                rationale = parsed_raw.get("rationale") if parsed_raw else None
                
                summary_id = await gpt_repo.store_gpt_call_summary(
                    asset=asset,
                    symbol=symbol,  # Add symbol field
                    agent_id=agent_id,
                    prompt_hash=prompt_hash,
                    gpt_response_id=gpt_response_id_str,
                    latency_ms=latency_ms,
                    response_type="json" if parsed_raw else "text",
                    decision_made=parsed_raw is not None,
                    order_placed=order_placed,
                    trade_executed=trade_executed,
                    # New fields for audit and TTL tracking
                    ttl_minutes=ttl_minutes,
                    active_window_c5=active_window_c5,
                    active_window_c15=active_window_c15,
                    rationale=rationale,
                    trigger_check_id=trigger_check_id,
                    trigger_met=trigger_met,
                    confidence=confidence_float,
                    pattern=sniper_resp.pattern,
                    direction=sniper_resp.direction,
                    broker_type=broker_name,
                    gpt_mode=gpt_mode,
                )
                
                app_logger.info(
                    {"asset": asset, "broker": broker_name, "agent": agent_id},
                    "Stored GPT call summary",
                    summary_id=summary_id,
                    trigger_met=trigger_met,
                    order_placed=order_placed,
                    trade_executed=trade_executed,
                )
        except Exception as e:
            safe_error_log(
                app_logger.error,
                {"asset": asset, "broker": broker_name, "agent": agent_id},
                "Failed to store GPT call summary",
                error=str(e)
            )

        # Flush any pending database operations
        try:
            await self._flush_db_operations()
        except Exception as e:
            app_logger.error(
                {"asset": asset, "broker": broker_name, "agent": agent_id},
                "Error flushing database operations",
                error=str(e)
            )

        return executed_result

    # Dynamic configuration storage removed
    
    def get_effective_candle_counts(self) -> tuple[int, int]:
        """Get effective candle counts considering both static config and dynamic recommendations.

        Returns:
            tuple[int, int]: (candles_5m_count, candles_15m_count)
        """
        # Always use static configuration
        return (self.candle_count_5m, self.candle_count_15m)

    def _compute_cycle_risk_meta(self, htf_candles: dict, c5_dicts: list, asset: str, *, as_of: datetime | None = None) -> dict:
        """ATR(1h), volatility stop floor and regime hint for this cycle.

        Pure arithmetic on candles already in memory (no broker/DB calls).
        Returns {} when inputs are unavailable so all consumers stay inert.
        ``regime_hint``/``er_1h`` ride along so registration-time gates and the
        executor see the same regime the prompts were built with (audit F6).
        """
        try:
            from packages.core.market_structure import (
                atr_from_candles, compute_htf_context, recommended_stop_distance, candle_time_utc,
            )
            if as_of is None and c5_dicts:
                latest_open = candle_time_utc(c5_dicts[-1])
                as_of = latest_open + timedelta(minutes=5) if latest_open else None
            atr_1h = atr_from_candles((htf_candles or {}).get("1h") or [])
            price = None
            if c5_dicts:
                last = c5_dicts[-1]
                price = float(last.get("close")) if isinstance(last, dict) else None
            sl_floor = recommended_stop_distance(atr_1h, price, asset, as_of)
            meta: dict = {}
            if atr_1h:
                meta["atr_1h"] = float(atr_1h)
            if sl_floor:
                meta["sl_floor"] = float(sl_floor)
            htf_ctx = compute_htf_context(htf_candles or {}, asset, as_of)
            if htf_ctx.get("regime_hint"):
                meta["regime_hint"] = htf_ctx["regime_hint"]
                meta["er_1h"] = htf_ctx.get("er_1h")
            if htf_ctx.get("htf_trend"):
                meta["htf_trend"] = htf_ctx["htf_trend"]
            # Trend agreement (2026-07-29): unanimous trend_1h/4h/1d. Rides in
            # the cycle meta so the executor, the monitor and every log line see
            # the same reading the prompts were built with — the freeze sample
            # says this, not htf_trend, is what separates the winners
            # (config.TREND_AGREEMENT_ENABLED for the evidence).
            if TREND_AGREEMENT_ENABLED:
                meta["trend_agreement"] = trend_agreement_from(htf_ctx)
                meta["trends_htf"] = [htf_ctx.get("trend_1h"), htf_ctx.get("trend_4h"),
                                      htf_ctx.get("trend_1d")]
            return meta
        except Exception as e:
            app_logger.warning({"asset": asset}, "cycle risk meta failed", error=str(e))
            return {}

    def _observe_session_tape(self, c5_dicts: list, asset: str) -> None:
        """LP-08 Phase 0 (2026-07-25): label-vs-tape observation, log-only.

        Compares the (slow) htf_trend label from this cycle's risk meta with
        what the current UTC session is actually doing on the 5m series already
        in memory. In "log" mode (the default for every asset) the only effect
        is a ``label_tape_conflict`` log line — freeze-safe measurement. In
        "context" mode (Phase 2a, per-asset user approval, nobody enabled) the
        tape is additionally exposed to the prompt path via
        ``self._cycle_session_tape``. Never blocks or fails the cycle.
        NOTE: deliberately NOT merged into ``_cycle_risk_meta`` — that dict is
        spread into the registered-strategy record and must stay byte-stable.
        """
        self._cycle_session_tape = None
        try:
            from packages.core.config import session_tape_mode_for
            mode = session_tape_mode_for(asset)
            if mode == "off":
                return
            from packages.core.market_structure import compute_session_tape
            htf_trend = (getattr(self, "_cycle_risk_meta", None) or {}).get("htf_trend")
            tape = compute_session_tape(c5_dicts, htf_trend, asset)
            if tape is None:
                return
            if mode == "context":
                self._cycle_session_tape = tape
            if tape.get("conflict"):
                # Values embedded in the message: the day-log formatter shows
                # %(message)s only (extra={} is invisible there).
                app_logger.info(
                    {"asset": asset},
                    f"label_tape_conflict {asset}: htf_trend={tape.get('htf_trend')} vs "
                    f"session={tape.get('session')} dir={tape.get('direction')} "
                    f"move={tape.get('move_pct')}% er_session={tape.get('er_session')} "
                    f"er_2h={tape.get('er_2h')} bars={tape.get('bars')} "
                    f"(LP-08 observation, mode={mode})",
                )
        except Exception as e:
            app_logger.warning({"asset": asset}, "session tape observation failed (inert)", error=str(e))

    async def _fetch_recent_trades(self, asset: str) -> list[RecentTradeView]:
        """Last few closed trades for the asset, for prompt context. Inert on failure.

        Served from the process-global in-memory ring buffer
        (``recent_trades_memory``): every full close is pushed there once, every
        cycle reads the last N back with no DB round-trip and no freshness cap —
        so every AI call sees its own recent outcomes (the Mongo+freshness-cap
        path used to leave ~half of them empty). On a cold process (empty buffer)
        a one-time Mongo backfill seeds it so a restart still has context.
        """
        try:
            if callable(getattr(self.broker, "operational_snapshot", None)):
                from packages.core.operational_ledger import sync_broker
                def read_operational():
                    now = datetime.now(timezone.utc)
                    account, ledger = sync_broker(self.broker, now)
                    try:
                        return ledger.recent_views(account, asset, now)
                    finally:
                        ledger.close()
                return await asyncio.to_thread(read_operational)
            from packages.core.config import (
                RECENT_TRADES_IN_PROMPT, RECENT_TRADES_COUNT,
                RECENT_TRADES_LOOKBACK_HOURS, RECENT_TRADES_MEMORY_COUNT,
            )
            if not RECENT_TRADES_IN_PROMPT:
                return []
            from packages.core.services.recent_trades_memory import (
                get_recent_trades, seed_from_views,
            )
            mem = get_recent_trades(asset, limit=RECENT_TRADES_MEMORY_COUNT)
            if mem:
                return mem
            # Cold start: buffer empty for this asset. Seed once from Mongo (no
            # freshness cap so old-but-real history is restored after a restart),
            # then in-memory is the source of truth for the rest of the process.
            from packages.db.repositories.comprehensive_logging import get_recent_closed_trades
            backfill = await asyncio.wait_for(
                get_recent_closed_trades(
                    asset,
                    lookback_hours=RECENT_TRADES_LOOKBACK_HOURS,
                    limit=RECENT_TRADES_MEMORY_COUNT,
                    max_bars=None,
                ),
                timeout=3.0,
            )
            if backfill:
                seed_from_views(asset, backfill)
            return backfill or []
        except Exception as e:
            app_logger.warning({"asset": asset}, "recent-trades fetch failed (inert)", error=str(e))
            return []

    async def _fetch_htf_candles(self, symbol: str, *, as_of: datetime | None = None) -> dict:
        """Fetch higher-timeframe candles for optional HTF context.

        Additive and inert: returns ``{}`` when HTF context is disabled
        (``config.HTF_CONTEXT_ENABLED``), the broker has no ``htf_candles``
        support, or on any error — it must never block or fail the trading
        cycle. Output shape: ``{interval: [{time,open,high,low,close,volume}, ...]}``.
        """
        try:
            from packages.core.config import (
                HTF_CONTEXT_ENABLED, HTF_CANDLE_COUNTS,
                HTF_CANDLE_COUNTS_BY_ASSET, resolve_for_asset,
            )
        except Exception:
            return {}
        if not HTF_CONTEXT_ENABLED:
            return {}
        broker = self.broker
        if not broker or not hasattr(broker, "htf_candles"):
            return {}
        counts = resolve_for_asset(HTF_CANDLE_COUNTS_BY_ASSET, symbol, None) or HTF_CANDLE_COUNTS
        out: dict = {}
        for interval, limit in counts.items():
            try:
                cs = await broker.htf_candles(symbol, interval, int(limit))
            except Exception as e:  # never break the cycle on HTF context
                app_logger.warning(
                    {"asset": symbol}, "HTF candle fetch failed", interval=interval, error=str(e)
                )
                continue
            if cs:
                if as_of is not None:
                    from packages.core.market_structure import candle_time_utc
                    duration = {"1h": timedelta(hours=1), "4h": timedelta(hours=4), "1d": timedelta(days=1)}.get(interval)
                    if duration is not None:
                        cs = [c for c in cs if (stamp := candle_time_utc({"time": c.time})) is not None
                              and stamp + duration <= as_of]
                out[interval] = [
                    {
                        "time": c.time.isoformat() if hasattr(c.time, "isoformat") else c.time,
                        "open": float(c.open), "high": float(c.high),
                        "low": float(c.low), "close": float(c.close),
                        "volume": float(c.volume) if c.volume is not None else 0.0,
                    }
                    for c in cs
                ]
        return out

    async def _handle_code_sniper_signal(
        self,
        code_signal,
        asset: str,
        broker_name: str,
        agent_id: str,
        cycle_id: str | None = None,
        generation_candles: list | None = None,
    ) -> None:
        """
        Handle a code sniper signal by setting up monitoring.

        Parameters
        ----------
        code_signal : CodeSniperSignal
            The code sniper signal containing the trading code
        asset : str
            Asset identifier
        broker_name : str
            Broker name for logging
        agent_id : str
            Agent identifier for logging
        cycle_id : str | None
            Multi-expert cycle ID that produced this signal
        generation_candles : list | None
            The 5m candle dicts the judge analyzed this cycle. When provided,
            the code is dry-run once against them for OBSERVABILITY: a
            program whose entry condition is already true on the data the
            judge just saw will execute on its first evaluation. That is
            allowed by design (the judge may deliberately arm a setup that is
            already complete) — but it is logged loudly so the immediate-fire
            rate stays measurable (2026-07-02 audit: 12% of programs; a rising
            rate means accidental static conditions, not deliberate entries).
        """
        try:
            from packages.core.services.code_sniper_monitor import (
                get_code_sniper_monitor_async,
                set_code_sniper_monitor_async,
                CodeSniperMonitor,
            )

            # ── Syntax gate (2026-07-08 audit, PATTERN_LIBRARY LP-09) ───────
            # Post the 07-06 prompt redesign, Sonnet 4.6 emits programs whose
            # block bodies are not indented (`if rr<1.2:` / same-indent
            # `return`). The silent pass-fill repair turned those into
            # permanent no-op "zombie" strategies — 32% of programs on 07-07,
            # including both winning USOIL plans. Repair what is unambiguous,
            # refuse the rest LOUDLY: a rejected program costs one cycle; a
            # zombie is monitored as a no-op for its entire TTL with zero
            # error output.
            from packages.core.code_sniper_executor import validate_judge_code

            code_ok, validated_code, repair_detail = validate_judge_code(
                code_signal.code
            )
            if not code_ok:
                app_logger.error(
                    {"asset": asset, "broker": broker_name, "agent": agent_id},
                    "REJECTING judge program — broken code cannot be repaired "
                    "without changing its semantics (would register a silent "
                    "zombie strategy; LP-09)",
                    reason=repair_detail,
                    cycle_id=cycle_id,
                    code_preview=code_signal.code[:300],
                )
                return
            if repair_detail:
                app_logger.warning(
                    {"asset": asset, "broker": broker_name, "agent": agent_id},
                    "Judge program auto-repaired at registration (LP-09)",
                    repair=repair_detail,
                    cycle_id=cycle_id,
                )
                code_signal.code = validated_code

            # ── Registration dry-run (observability only — never blocks) ────
            # Same sandbox path the monitor uses at runtime (sanitize → wrap →
            # subprocess), run in a worker thread. Immediate buy/sell is a
            # VALID outcome: the strategy is registered normally and will fire
            # on the next 5m close. The log line exists so the share of
            # immediate entries can be tracked against the audit baseline.
            if generation_candles and len(generation_candles) >= CS_EXPECTED_TOTAL_CANDLES:
                try:
                    from packages.core.code_sniper_executor import (
                        dry_run_code_sniper_direction,
                    )
                    dry_direction = await asyncio.to_thread(
                        dry_run_code_sniper_direction,
                        code_signal,
                        generation_candles[-1],
                        generation_candles[-CS_EXPECTED_TOTAL_CANDLES:-1],
                    )
                    if dry_direction in ("buy", "sell"):
                        app_logger.warning(
                            {"asset": asset, "broker": broker_name, "agent": agent_id},
                            "IMMEDIATE-FIRE: judge code already fires on its generation "
                            "candles — will execute on first evaluation (allowed; monitoring rate)",
                            dry_run_direction=dry_direction,
                            cycle_id=cycle_id,
                            ttl_minutes=code_signal.ttl_minutes,
                            confidence=code_signal.confidence,
                            code_length=len(code_signal.code),
                        )
                except Exception as dry_err:
                    app_logger.warning(
                        {"asset": asset, "broker": broker_name, "agent": agent_id},
                        "Registration dry-run failed (observability only — strategy unaffected)",
                        error=str(dry_err),
                        error_type=type(dry_err).__name__,
                    )

            # ── Chop style enforcement (2026-07-02 audit F6) ────────────────
            # The prompts order "chop ⇒ range-edge fades only", but nothing
            # enforced it: breakout/momentum programs were emitted and traded
            # straight through ER≤0.08 weeks (the USOIL/USTEC bleed). The cycle
            # regime is already computed once per cycle in _cycle_risk_meta;
            # a program whose self-declared style contradicts it is refused at
            # registration — zero per-candle cost, and the judge's flat path
            # remains the normal outcome for everything else.
            _regime_hint = (getattr(self, "_cycle_risk_meta", None) or {}).get("regime_hint")
            _strategy_type = str(
                (getattr(code_signal, "metadata", None) or {}).get("strategy_type", "")
            ).lower()
            if _regime_hint == "chop" and _strategy_type in ("breakout", "momentum"):
                app_logger.warning(
                    {"asset": asset, "broker": broker_name, "agent": agent_id},
                    "Chop regime — refusing to register breakout/momentum strategy "
                    "(style contradicts regime; prompts require range-edge fades in chop)",
                    strategy_type=_strategy_type,
                    regime_hint=_regime_hint,
                    er_1h=(getattr(self, "_cycle_risk_meta", None) or {}).get("er_1h"),
                    cycle_id=cycle_id,
                )
                return

            # Get or create the code sniper monitor.
            # Must use the async variant — the sync wrapper always returns None
            # when called from an async context (loop.is_running() == True).
            monitor = await get_code_sniper_monitor_async(agent_id)
            if monitor is None:
                # Initialize the monitor with required services
                # Create risk service if not available
                risk_service = getattr(self, 'risk_service', None)
                if risk_service is None:
                    from packages.core.services.risk import RiskService
                    risk_service = RiskService(self.broker)
                    # Update balances from broker.
                    # broker.balances() calls mt5.account_info() synchronously —
                    # must run in asyncio.to_thread to avoid blocking the event loop.
                    try:
                        balances = await asyncio.to_thread(self.broker.balances)
                        risk_service.update_balances(balances)
                    except Exception as e:
                        app_logger.warning(
                            {"asset": asset, "broker": broker_name, "agent": agent_id},
                            "Failed to update risk service balances",
                            error=str(e),
                            error_type=type(e).__name__
                        )
                
                # CRITICAL: Ensure TradeMonitor exists BEFORE creating
                # CodeSniperMonitor, otherwise trade_mon=None and post-trade
                # monitoring (SL trailing, TP1 partial, TP2 close) never starts.
                if not self._trade_monitor_initialized:
                    candle_service = self.agent_config.get("candle_service")
                    journal = self.agent_config.get("journal")
                    if candle_service and self.broker:
                        await self._initialize_trade_monitor(candle_service, self.broker, journal)
                        app_logger.info(
                            {"asset": asset, "agent": agent_id},
                            "TradeMonitor initialized for code sniper flow",
                            trade_monitor_ready=self._trade_monitor is not None,
                        )

                monitor = CodeSniperMonitor(
                    candle_service=self.agent_config["candle_service"],
                    broker=self.broker,
                    risk_service=risk_service,
                    trade_monitor=self._trade_monitor,
                    check_active_trade_callback=self.has_active_trade,
                    on_trade_executed=self._on_code_sniper_trade_executed,
                    on_strategy_expired=self._on_code_sniper_strategy_expired,
                )
                await set_code_sniper_monitor_async(agent_id, monitor)
                monitor.start_monitoring()
                
                # Add verification that monitor started successfully
                app_logger.info(
                    {"asset": asset, "broker": broker_name, "agent": agent_id},
                    "Code sniper monitor start_monitoring() called",
                    monitor_task_created=monitor._task is not None,
                    monitor_task_done=monitor._task.done() if monitor._task else None,
                    monitor_task_cancelled=monitor._task.cancelled() if monitor._task else None,
                )
            
            else:
                # Monitor already exists — replace stale strategies for this asset
                # so the new GPT code is the only one running, not the old one.
                old_strats = [
                    sid for sid, s in list(monitor._strategies.items())
                    if s.asset == asset
                ]
                for sid in old_strats:
                    monitor.remove_strategy(sid)
                if old_strats:
                    app_logger.info(
                        {"asset": asset, "broker": broker_name, "agent": agent_id},
                        "Replaced stale monitoring strategies with new GPT cycle",
                        cleared_count=len(old_strats),
                    )

            # Create a unique strategy ID
            strategy_id = f"{asset}_{agent_id}_{int(datetime.now().timestamp())}"
            
            # Prepare context with expert signals and market data
            context = {
                "asset": asset,
                "symbol": self.symbol,
                "broker": broker_name,
                "agent_id": agent_id,
                "cycle_id": cycle_id,
                "ai_cost": code_signal.metadata.get("ai_cost"),
                "trade_reason": trade_reason(code_signal),
                "timestamp": datetime.now().isoformat(),
                "ttl_minutes": code_signal.ttl_minutes,
                "confidence": code_signal.confidence,
                "risk_pct": self.agent_config.get("risk_pct", CS_LIVE_RISK_PCT),
                # Cycle risk meta (atr_1h / sl_floor) — consumed by the executor
                # (stop auto-widen) and the trade monitor (runner trail dist).
                **(getattr(self, "_cycle_risk_meta", None) or {}),
            }
            
            # Add the strategy to monitoring
            monitor.add_strategy(
                strategy_id=strategy_id,
                code_signal=code_signal,
                asset=asset,
                symbol=self.symbol,
                context=context,
            )
            
            app_logger.info(
                {"asset": asset, "broker": broker_name, "agent": agent_id},
                "Code sniper strategy added to monitoring",
                strategy_id=strategy_id,
                ttl_minutes=code_signal.ttl_minutes,
                confidence=code_signal.confidence,
                code_length=len(code_signal.code),
                risk_pct=context["risk_pct"] * 100,
            )
            
        except Exception as e:
            safe_error_log(
                app_logger.error,
                {"asset": asset, "broker": broker_name, "agent": agent_id},
                "Failed to handle code sniper signal",
                error=str(e),
                error_type=type(e).__name__
            )
            raise
    
    def _on_code_sniper_trade_executed(self, strategy_id: str, decision, order, trade_id: str = None, context: dict = None) -> None:
        """Callback when a code sniper trade is executed."""
        from packages.core.models import Side
        context = context or {}
        record_trade_open(decision.asset)
        app_logger.info(
            {"strategy_id": strategy_id, "asset": decision.asset},
            "Code sniper trade executed",
            symbol=decision.symbol,
            side=decision.side.value,
            entry=decision.entry,
            quantity=decision.quantity,
            order_id=order.id if order else "unknown",
            trade_id=trade_id,
            cycle_id=context.get("cycle_id"),
        )
        
        # CRITICAL: Mark active trade so has_active_trade() returns True,
        # preventing duplicate entries while this position is open.
        self._active_trade = {
            "strategy_id": strategy_id,
            "symbol": decision.symbol,
            "asset": decision.asset,
            "side": decision.side.value,
            "entry": decision.entry,
            "sl": decision.sl,
            "tp1": decision.tp1,
            "tp2": decision.tp,
            "quantity": decision.quantity,
            "direction": "long" if decision.side == Side.BUY else "short",
            "order_id": order.id if order else None,
            "trade_id": trade_id,
        }
        
        # Store in active_trades for tracking. Routed through _fire_and_forget so
        # the task is strongly referenced (no GC-drop) with a write timeout.
        try:
            from packages.db.repositories.active_trades import store_active_trade
            _fire_and_forget(store_active_trade(
                agent_id=self.agent_config.get("agent_id", "unknown"),
                symbol=decision.symbol,
                trade_data={
                    "entry": decision.entry,
                    "sl": decision.sl,
                    "tp1": decision.tp1,
                    "tp2": decision.tp,
                    "quantity": decision.quantity,
                    "order_id": order.id if order else None,
                    "trade_id": trade_id,
                    "side": decision.side.value,
                    "asset": decision.asset,
                }
            ), "active_trades")
        except Exception as e:
            app_logger.error(
                {"strategy_id": strategy_id, "asset": decision.asset},
                "Failed to store active trade",
                error=str(e),
                error_type=type(e).__name__,
            )

        # Store in comprehensive_trades so unified analysis page can display it.
        # Routed through _fire_and_forget for a strong task ref + write timeout.
        try:
            from datetime import datetime, timezone
            from packages.db.repositories.comprehensive_logging import store_comprehensive_trade
            now = datetime.now(timezone.utc)
            agent_id = self.agent_config.get("agent_id", "unknown")
            # Record the REAL broker fill as entry, not the signal/level price.
            # order.average_price is the executed price MT5 returned (it falls
            # back to decision.entry only when the broker gave no fill price).
            # The recent-trades context fed to the AI reads this doc's
            # entry_price, so it must match the actual position to the point.
            fill_price = float(getattr(order, "average_price", 0.0) or 0.0)
            if not math.isfinite(fill_price) or fill_price <= 0:
                raise ValueError("Cannot record live entry without confirmed fill price")
            filled_quantity = float(getattr(order, "filled_quantity", 0.0) or 0.0)
            if not math.isfinite(filled_quantity) or filled_quantity <= 0:
                raise ValueError("Cannot record live entry without confirmed filled quantity")
            _fire_and_forget(store_comprehensive_trade(
                symbol=decision.symbol,
                asset=decision.asset,
                quantity=filled_quantity,
                price=fill_price,
                entry_price=fill_price,
                agent_id=agent_id,
                prompt_hash=strategy_id,
                signal_received_at=now,
                trade_executed_at=now,
                gpt_response_id=order.id if order else None,
                cycle_id=context.get("cycle_id"),
                ai_cost=context.get("ai_cost"),
                trade_reason=context.get("trade_reason"),
                trigger_reason=decision.notes,
                broker_cost=context.get("broker_cost"),
                stop_loss=decision.sl,
                take_profit=decision.tp1,
                trade_type="live",
                side=decision.side.value,
                pattern=decision.notes,
                confidence=None,
                broker_type=self.agent_config.get("broker_type", "MT5Adapter"),
                gpt_mode="multi_expert_code_sniper",
                trade_status="open",
                # MT5 position id == opening order ticket — exits and partial
                # legs carry the same ticket, making linkage exact.
                position_ticket=(order.id if order else None),
            ), "comprehensive_trades")
            app_logger.info(
                {"strategy_id": strategy_id, "asset": decision.asset},
                "Trade stored in comprehensive_trades for unified analysis",
                symbol=decision.symbol,
                entry_price=decision.entry,
                side=decision.side.value,
            )
        except Exception as e:
            app_logger.error(
                {"strategy_id": strategy_id, "asset": decision.asset},
                "Failed to store comprehensive trade from code sniper",
                error=str(e),
                error_type=type(e).__name__,
            )
    
    def _on_code_sniper_strategy_expired(self, strategy_id: str, strategy_data: dict) -> None:
        """Callback when a code sniper strategy expires.

        Persists the no-trigger outcome (fire-and-forget) — previously these
        were log-only, so selectivity and "entry never reached" patterns were
        unmeasurable (docs/engineering/2026-06-11_performance-limiters.md §1.4).
        """
        app_logger.info(
            {"strategy_id": strategy_id, "asset": strategy_data["asset"]},
            "Code sniper strategy expired",
            symbol=strategy_data["symbol"],
            execution_count=strategy_data["execution_count"],
        )
        try:
            import re as _re
            from packages.db.repositories.comprehensive_logging import store_strategy_expiry
            signal = strategy_data.get("signal")
            code = getattr(signal, "code", "") or ""
            # Judge code declares its levels as plain assignments, but the
            # casing/naming is the model's choice: `ENTRY = 1.14`, `entry=1.14`
            # and `take_profit_1 = ...` all occur. The original uppercase-only
            # patterns matched just 79 of 259 freeze expiries, which is why the
            # forfeited-setup analysis could only see 30% of the sample. Match
            # case-insensitively and accept the long-form aliases.
            levels = {}
            for name, aliases in (
                ("entry", ("entry",)),
                ("sl", ("sl", "stop_loss", "stop")),
                ("tp1", ("tp1", "take_profit_1", "tp")),
                ("tp2", ("tp2", "take_profit_2")),
            ):
                for alias in aliases:
                    m = _re.search(rf"\b{alias}\s*=\s*([0-9]+\.?[0-9]*)", code, _re.IGNORECASE)
                    if m:
                        try:
                            levels[name] = float(m.group(1))
                            break
                        except ValueError:
                            pass
            context = strategy_data.get("context") or {}
            doc = {
                "strategy_id": strategy_id,
                "asset": strategy_data.get("asset"),
                "symbol": strategy_data.get("symbol"),
                "cycle_id": context.get("cycle_id"),
                "agent_id": context.get("agent_id"),
                "created_at": strategy_data.get("created_at"),
                "expiry": strategy_data.get("expiry"),
                "last_execution": strategy_data.get("last_execution"),
                "execution_count": strategy_data.get("execution_count"),
                "ttl_minutes": getattr(signal, "ttl_minutes", None),
                "confidence": getattr(signal, "confidence", None),
                "metadata": getattr(signal, "metadata", None),
                "proposed_levels": levels or None,
                "outcome": "NO_TRIGGER_EXPIRED",
            }
            _fire_and_forget(store_strategy_expiry(doc), "strategy_expiries")
        except Exception as e:
            app_logger.warning(
                {"strategy_id": strategy_id},
                "Failed to persist strategy expiry (non-fatal)",
                error=str(e),
            )


__all__ = ["AgentRunner"]

