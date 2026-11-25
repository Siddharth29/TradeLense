"""Lean 5min candle monitor for code sniper strategies - optimized for speed & clarity."""

from __future__ import annotations

import asyncio
import logging
import math
import pandas as pd
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass, field

# Lazy import to avoid circular dependency:
# code_sniper_executor → services.risk → services → trade_monitor
# → code_sniper_monitor → code_sniper_executor (cycle)
# execute_code_sniper_strategy is resolved at call time instead.
from ..schemas_trade import CodeSniperSignal
from ..models import Decision, Side
from ..scheduler import await_next_bar_close
from ..strategy_candle_gate import CandleContractError
from ..config import (
    CSM_CANDLE_BUFFER_SEC, CSM_ERROR_RETRY_SEC, CSM_PAUSE_CHECK_SEC,
    CSM_CANDLES_REQUIRED, CSM_HISTORICAL_COUNT, CSM_TRADE_EXPIRY_HOURS,
    CSM_MAX_CONCURRENT_STRATEGIES, CSM_STRATEGY_TIMEOUT_SEC,
    CSM_CIRCUIT_BREAKER_THRESHOLD, CSM_CANDLE_STALENESS_THRESHOLD_MIN,
    CSM_HEALTH_CHECK_INTERVAL_SEC, CORRELATION_GROUPS,
)
from ..risk.account_guard import account_trading_halted
from ..risk.trade_throttle import trading_throttled
from ..risk.news_blackout import news_blackout_active
from ..asset_registry import market_close_windows, WEEKEND_CLOSE_SYMBOLS
from ..session import exness_closures

logger = logging.getLogger(__name__)


def _confirmed_execution(order: Any, fills: List[Any]) -> tuple[float, float]:
    """Return broker-confirmed entry VWAP and volume, never requested values."""
    def positive(value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0.0
        return float(value) if math.isfinite(value) and value > 0 else 0.0

    quantity = positive(getattr(order, "filled_quantity", None))
    entry = positive(getattr(order, "average_price", None))
    if quantity and entry:
        return entry, quantity  # Complete aggregate broker receipt is authoritative.
    order_id = str(getattr(order, "id", "") or "")
    if order_id and any(str(getattr(fill, "order_id", "")) != order_id for fill in (fills or [])):
        raise ValueError("Fill identity mismatch; position reconciliation required")
    valid_fills = [
        (positive(getattr(fill, "quantity", None)), positive(getattr(fill, "price", None)))
        for fill in (fills or [])
    ]
    if any(not qty or not price for qty, price in valid_fills):
        raise ValueError("Incomplete fill receipt; position reconciliation required")
    if valid_fills:
        fill_quantity = sum(qty for qty, _ in valid_fills)
        if quantity and not math.isclose(quantity, fill_quantity, rel_tol=1e-9, abs_tol=1e-12):
            raise ValueError("Fill volume mismatch; position reconciliation required")
        quantity = fill_quantity
        entry = sum(qty * price for qty, price in valid_fills) / fill_quantity
    if not quantity or not entry:
        raise ValueError("Broker response has no confirmed entry price/volume; position reconciliation required")
    return entry, quantity


# ============================================================================
# CORRELATED-INSTRUMENT COHERENCE  (see config.CORRELATION_GROUPS)
# ============================================================================

# Reverse index asset -> peers, built ONCE at import so the hot path is a single
# dict lookup. Values are tuples so `str.startswith(peers)` can run in C.
_CORRELATION_PEERS: Dict[str, tuple[str, ...]] = {}
for _members in CORRELATION_GROUPS.values():
    _upper = tuple(m.upper() for m in _members)
    for _member in _upper:
        _CORRELATION_PEERS[_member] = tuple(o for o in _upper if o != _member)
del _members, _upper, _member  # keep the module namespace clean


def _position_symbol(pos: Any) -> str:
    """Symbol of a broker position, whether it is a raw dict or a Position."""
    if isinstance(pos, dict):
        return str(pos.get("symbol") or "")
    return str(getattr(pos, "symbol", "") or "")


def _position_side(pos: Any) -> Optional[str]:
    """Best-effort "buy"/"sell" for a broker position, or None if unknowable.

    IMPORTANT: :class:`packages.core.models.Position` carries only an UNSIGNED
    ``quantity`` (MT5 ``volume`` is never negative), so the side is NOT
    recoverable from it — a ``quantity < 0`` test would compile, ship, and never
    fire once. The side lives in the raw broker dict's ``type`` field
    (MT5 ``POSITION_TYPE_BUY`` = 0, ``POSITION_TYPE_SELL`` = 1), which
    ``positions_raw()`` preserves. Returning None makes the caller skip the
    correlation check rather than guess.
    """
    raw = pos.get("type") if isinstance(pos, dict) else getattr(pos, "type", None)
    if raw is None:
        side = pos.get("side") if isinstance(pos, dict) else getattr(pos, "side", None)
        if side is None:
            return None
        side = str(getattr(side, "value", side)).lower()
        return side if side in ("buy", "sell") else None
    # bool is a subclass of int — reject it before the numeric branch.
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        code = int(raw)
        # 2..7 are PENDING ORDER types, never open positions — treat as unknown.
        return "buy" if code == 0 else "sell" if code == 1 else None
    text = str(raw).lower()
    if "buy" in text:
        return "buy"
    if "sell" in text:
        return "sell"
    return None


def _opposing_correlated_positions(
    asset: str, side: Any, open_positions: List[Any]
) -> List[Any]:
    """Open positions in a CORRELATED peer asset held on the OPPOSITE side.

    Pure and side-effect free so it is unit-testable without a broker. Returns
    [] whenever the asset has no configured peers (the common case — one dict
    lookup and out), the requested side is unknown, or no peer position's side
    can be determined. Same-side peers are intentionally NOT returned; that is a
    concentration decision, not a contradiction (see config.CORRELATION_GROUPS).
    """
    peers = _CORRELATION_PEERS.get((asset or "").upper())
    if not peers or side is None:
        return []
    want = str(getattr(side, "value", side)).lower()
    if want not in ("buy", "sell"):
        return []
    conflicts = []
    for pos in open_positions:
        symbol = _position_symbol(pos).upper()
        # tuple form of startswith is a C-level scan; also suffix-safe
        # (XAUUSDm / XAUUSD.raw both match "XAUUSD").
        if not symbol or not symbol.startswith(peers):
            continue
        pos_side = _position_side(pos)
        if pos_side is not None and pos_side != want:
            conflicts.append(pos)
    return conflicts


# ============================================================================
# MARKET HOURS
# ============================================================================

# Known daily close windows (UTC).  During these windows the broker returns
# stale or no candles.  The monitor should sleep through them instead of
# burning retries and logging CRITICAL errors.
# Format: { symbol_prefix: [(close_hour, close_minute, open_hour, open_minute)] }
MARKET_CLOSE_WINDOWS: Dict[str, List[tuple]] = market_close_windows()

# Symbols that have a full weekend closure (forex + metals).
# Crypto trades 24/7 through weekends — only has daily maintenance.
# Weekend close: Friday ~20:55 UTC -> Sunday ~22:05 UTC
_WEEKEND_CLOSE_SYMBOLS = set(WEEKEND_CLOSE_SYMBOLS)

# Pre-close buffer: close positions this many minutes BEFORE market closes
from ..entry_policy import PRE_CLOSE_BUFFER_MINUTES


def _is_market_closed(symbol: str, now: Optional[datetime] = None) -> tuple[bool, int]:
    """Check if the market is closed for the given symbol right now.

    Returns (is_closed, seconds_until_open).  When the market is open the
    second value is 0.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    closures = exness_closures(symbol, now)
    if closures is not None:
        for start, end, _ in closures:
            if start <= now < end:
                return True, int((end - now).total_seconds())
        return False, 0

    symbol_upper = symbol.upper()
    base_symbol = symbol_upper.rstrip("M.RAW#")

    # --- Weekend closure check (forex/metals only) ---
    if base_symbol in _WEEKEND_CLOSE_SYMBOLS:
        weekday = now.weekday()  # Mon=0 .. Sun=6
        # Friday after 20:55 UTC
        if weekday == 4 and (now.hour > 20 or (now.hour == 20 and now.minute >= 55)):
            # Reopen Sunday 22:05 UTC
            days_until_sunday = 2
            open_time = (now + timedelta(days=days_until_sunday)).replace(
                hour=22, minute=5, second=0, microsecond=0
            )
            remaining = int((open_time - now).total_seconds())
            return True, remaining
        # Saturday (all day closed)
        if weekday == 5:
            days_until_sunday = 1
            open_time = (now + timedelta(days=days_until_sunday)).replace(
                hour=22, minute=5, second=0, microsecond=0
            )
            remaining = int((open_time - now).total_seconds())
            return True, remaining
        # Sunday before 22:05 UTC
        if weekday == 6 and (now.hour < 22 or (now.hour == 22 and now.minute < 5)):
            open_time = now.replace(hour=22, minute=5, second=0, microsecond=0)
            remaining = int((open_time - now).total_seconds())
            return True, remaining

    # --- Daily maintenance window check ---
    windows = MARKET_CLOSE_WINDOWS.get(base_symbol)
    if not windows:
        return False, 0

    for close_h, close_m, open_h, open_m in windows:
        close_time = now.replace(hour=close_h, minute=close_m, second=0, microsecond=0)
        open_time = now.replace(hour=open_h, minute=open_m, second=0, microsecond=0)

        if open_time <= close_time:
            open_time += timedelta(days=1)

        if close_time <= now < open_time:
            remaining = int((open_time - now).total_seconds())
            return True, remaining

    return False, 0


def _is_market_closing_soon(symbol: str, buffer_minutes: int = PRE_CLOSE_BUFFER_MINUTES,
                            now: Optional[datetime] = None) -> tuple[bool, str]:
    """Check if the market is about to close within buffer_minutes.

    Returns (closing_soon, reason).  Used to force-close positions before
    gaps (daily maintenance or weekend).
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    closures = exness_closures(symbol, now)
    if closures is not None:
        for start, _, reason in closures:
            if 0 < (start - now).total_seconds() <= buffer_minutes * 60:
                return True, reason
        return False, ""
    symbol_upper = symbol.upper()
    base_symbol = symbol_upper.rstrip("M.RAW#")

    # --- Weekend pre-close (forex/metals): Friday before 20:55 UTC ---
    if base_symbol in _WEEKEND_CLOSE_SYMBOLS:
        if now.weekday() == 4:  # Friday
            friday_close = now.replace(hour=20, minute=55, second=0, microsecond=0)
            time_until_close = (friday_close - now).total_seconds()
            if 0 < time_until_close <= buffer_minutes * 60:
                return True, "weekend_close"

    # --- Daily maintenance pre-close ---
    windows = MARKET_CLOSE_WINDOWS.get(base_symbol)
    if not windows:
        return False, ""

    for close_h, close_m, _, _ in windows:
        close_time = now.replace(hour=close_h, minute=close_m, second=0, microsecond=0)
        time_until_close = (close_time - now).total_seconds()
        if 0 < time_until_close <= buffer_minutes * 60:
            return True, "daily_maintenance"

    return False, ""


# ============================================================================
# METRICS
# ============================================================================

@dataclass
class MonitorMetrics:
    """Metrics for code sniper monitor."""
    
    # Strategy execution metrics
    strategies_executed: int = 0
    strategies_successful: int = 0
    strategies_failed: int = 0
    strategies_timeout: int = 0
    strategies_disabled: int = 0
    
    # Performance metrics
    total_execution_time_ms: float = 0.0
    avg_execution_time_ms: float = 0.0
    max_execution_time_ms: float = 0.0
    
    # Candle metrics
    candle_fetches: int = 0
    candle_cache_hits: int = 0
    candle_fetch_time_ms: float = 0.0
    
    # Trade metrics
    trades_executed: int = 0
    trades_failed: int = 0
    
    # Health metrics
    last_health_check: Optional[datetime] = None
    cycles_completed: int = 0
    errors_encountered: int = 0
    
    def update_execution_time(self, execution_time_ms: float) -> None:
        """Update execution time metrics."""
        self.strategies_executed += 1
        self.total_execution_time_ms += execution_time_ms
        self.avg_execution_time_ms = self.total_execution_time_ms / self.strategies_executed
        self.max_execution_time_ms = max(self.max_execution_time_ms, execution_time_ms)
    
    def record_success(self) -> None:
        """Record successful strategy execution."""
        self.strategies_successful += 1
    
    def record_failure(self, error_type: str = "unknown") -> None:
        """Record failed strategy execution."""
        self.strategies_failed += 1
        if error_type == "TimeoutError":
            self.strategies_timeout += 1
    
    def record_candle_fetch(self, fetch_time_ms: float, cache_hit: bool = False) -> None:
        """Record candle fetch metrics."""
        self.candle_fetches += 1
        self.candle_fetch_time_ms += fetch_time_ms
        if cache_hit:
            self.candle_cache_hits += 1
    
    def record_trade(self, success: bool = True) -> None:
        """Record trade execution."""
        if success:
            self.trades_executed += 1
        else:
            self.trades_failed += 1
    
    def record_cycle(self) -> None:
        """Record completed cycle."""
        self.cycles_completed += 1
    
    def record_error(self) -> None:
        """Record error encountered."""
        self.errors_encountered += 1
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert metrics to dictionary for logging/export."""
        return {
            "strategies_executed": self.strategies_executed,
            "strategies_successful": self.strategies_successful,
            "strategies_failed": self.strategies_failed,
            "strategies_timeout": self.strategies_timeout,
            "strategies_disabled": self.strategies_disabled,
            "avg_execution_time_ms": round(self.avg_execution_time_ms, 2),
            "max_execution_time_ms": round(self.max_execution_time_ms, 2),
            "candle_fetches": self.candle_fetches,
            "candle_cache_hit_rate": round(self.candle_cache_hits / max(1, self.candle_fetches), 3),
            "trades_executed": self.trades_executed,
            "trades_failed": self.trades_failed,
            "cycles_completed": self.cycles_completed,
            "errors_encountered": self.errors_encountered,
        }


# ============================================================================
# CONFIGURATION
# ============================================================================

@dataclass
class MonitorConfig:
    """Configuration for code sniper monitor."""
    candle_buffer_sec: int = CSM_CANDLE_BUFFER_SEC
    error_retry_sec: int = CSM_ERROR_RETRY_SEC
    pause_check_sec: int = CSM_PAUSE_CHECK_SEC
    candles_required: int = CSM_CANDLES_REQUIRED
    historical_count: int = CSM_HISTORICAL_COUNT
    trade_expiry_hours: int = CSM_TRADE_EXPIRY_HOURS
    max_concurrent_strategies: int = CSM_MAX_CONCURRENT_STRATEGIES
    strategy_timeout_sec: int = CSM_STRATEGY_TIMEOUT_SEC
    circuit_breaker_threshold: int = CSM_CIRCUIT_BREAKER_THRESHOLD
    candle_staleness_threshold_min: int = CSM_CANDLE_STALENESS_THRESHOLD_MIN
    health_check_interval_sec: int = CSM_HEALTH_CHECK_INTERVAL_SEC


# ============================================================================
# EVENTS
# ============================================================================

@dataclass
class StrategyEvent:
    """Base class for strategy events."""
    strategy_id: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

@dataclass
class StrategyExpired(StrategyEvent):
    """Event when a strategy expires."""
    strategy_data: Dict[str, Any] = field(default_factory=dict)

@dataclass
class TradeExecuted(StrategyEvent):
    """Event when a trade is executed."""
    decision: Decision = field(default_factory=lambda: None)
    order: Any = None
    trade_id: Optional[str] = None

@dataclass
class StrategyFailed(StrategyEvent):
    """Event when a strategy fails."""
    error: str = ""
    error_type: str = ""
    failure_count: int = 0


@dataclass
class Strategy:
    """Clean strategy container - all data in one place."""
    
    id: str
    signal: CodeSniperSignal
    asset: str
    symbol: str
    context: Dict[str, Any]
    expiry: datetime
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_run: Optional[datetime] = None
    run_count: int = 0
    failure_count: int = 0
    is_disabled: bool = False
    
    @property
    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) > self.expiry
    
    @property
    def minutes_left(self) -> float:
        return (self.expiry - datetime.now(timezone.utc)).total_seconds() / 60
    
    @property
    def should_execute(self) -> bool:
        """Check if strategy should execute (not expired, not disabled)."""
        return not self.is_expired and not self.is_disabled


class CodeSniperMonitor:
    """Fast 5min candle monitor - minimal overhead, maximum clarity."""
    
    def __init__(
        self,
        candle_service: Any,
        broker: Any,
        risk_service: Any,
        trade_monitor: Optional[Any] = None,
        check_active_trade_callback: Optional[Callable[[], bool]] = None,
        on_trade_executed: Optional[Callable] = None,
        on_strategy_expired: Optional[Callable] = None,
        config: Optional[MonitorConfig] = None,
    ):
        self.candles = candle_service
        self.broker = broker
        self.risk = risk_service
        self.trade_mon = trade_monitor
        self.check_trade = check_active_trade_callback
        self.on_trade = on_trade_executed
        self.on_expire = on_strategy_expired
        self.config = config or MonitorConfig()
        
        self._strategies: Dict[str, Strategy] = {}
        self._task: Optional[asyncio.Task] = None
        self._stop = False
        self._semaphore = asyncio.Semaphore(self.config.max_concurrent_strategies)
        self._event_queue: asyncio.Queue = asyncio.Queue()
        self._last_health_check = datetime.now(timezone.utc)
        
        # Candle cache for current cycle
        self._candle_cache: Dict[str, tuple[Dict[str, Any], List[Dict[str, Any]]]] = {}
        
        # Metrics
        self.metrics = MonitorMetrics()
        
        # Keep logger for compatibility
        self.logger = logger
    
    # ========== Public API ==========
    
    def start_monitoring(self) -> None:
        """Start monitoring loop."""
        if self._task and not self._task.done():
            logger.warning("Monitor already running")
            return
        
        self._task = asyncio.create_task(self._loop())
        logger.info("Monitor started", extra={
            "task_active": not self._task.done()
        })
    
    async def stop_monitoring(self) -> None:
        """Stop monitoring loop with graceful shutdown."""
        self._stop = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await asyncio.wait_for(asyncio.gather(self._task, return_exceptions=True), timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning("Timeout waiting for monitor task to complete")
        logger.info("Monitor stopped")
    
    def add_strategy(
        self,
        strategy_id: str,
        code_signal: CodeSniperSignal,
        asset: str,
        symbol: str,
        context: Dict[str, Any],
    ) -> None:
        """Add strategy to monitor."""
        expiry = datetime.now(timezone.utc) + timedelta(minutes=code_signal.ttl_minutes)
        
        strat = Strategy(
            id=strategy_id,
            signal=code_signal,
            asset=asset,
            symbol=symbol,
            context=context,
            expiry=expiry,
        )
        
        self._strategies[strategy_id] = strat
        
        logger.info("Strategy added", extra={
            "id": strategy_id,
            "asset": asset,
            "symbol": symbol,
            "ttl_min": code_signal.ttl_minutes,
            "confidence": code_signal.confidence,
        })
    
    def remove_strategy(self, strategy_id: str) -> None:
        """Remove strategy from monitor."""
        if strat := self._strategies.pop(strategy_id, None):
            logger.info("Strategy removed", extra={
                "id": strategy_id,
                "runs": strat.run_count,
            })
    
    def get_active_strategies(self) -> Dict[str, Dict[str, Any]]:
        """Get all active strategies - returns dict format for compatibility."""
        result = {}
        for sid, strat in self._strategies.items():
            result[sid] = {
                "strategy_id": strat.id,
                "code_signal": strat.signal,
                "asset": strat.asset,
                "symbol": strat.symbol,
                "context": strat.context,
                "expiry": strat.expiry,
                "effective_ttl": strat.signal.ttl_minutes,
                "created_at": strat.created_at,
                "last_execution": strat.last_run,
                "execution_count": strat.run_count,
            }
        return result
    
    @property
    def active_count(self) -> int:
        return len(self._strategies)

    def active_strategy_minutes_left(self, asset: str) -> Optional[float]:
        """Minutes left on the longest-lived active strategy for *asset*.

        Returns None when no unexpired, enabled strategy is registered for the
        asset. Used by the runner to skip a new GPT cycle while the current
        judge program is still monitoring (2026-07-02 audit F4 — previously
        every hourly cycle replaced the strategy, so ttl_minutes > 60 never
        ran to term: 55 of 74 strategies were replaced, only 12 expired).
        """
        left = [
            s.minutes_left
            for s in self._strategies.values()
            if s.asset == asset and s.should_execute
        ]
        return max(left) if left else None
    
    def get_metrics(self) -> Dict[str, Any]:
        """Get current metrics as dictionary."""
        return self.metrics.to_dict()
    
    # ========== Core Loop ==========
    
    async def _loop(self) -> None:
        """Main 5min monitoring loop - clean & fast with health checks."""
        try:
            while not self._stop:
                try:
                    # Health check
                    await self._health_check()
                    
                    # Pause if trade active
                    if self.check_trade and self.check_trade():
                        logger.info("Trade active - pausing monitor", extra={
                            "strategies": self.active_count
                        })
                        await asyncio.sleep(self.config.pause_check_sec)
                        continue
                    
                    # If ALL active symbols are in a market-close window,
                    # sleep until the earliest reopen instead of burning cycles.
                    active_symbols = {s.symbol for s in self._strategies.values() if s.should_execute}
                    if active_symbols:
                        close_waits = []
                        for sym in active_symbols:
                            closed, secs = _is_market_closed(sym)
                            if closed:
                                close_waits.append((sym, secs))
                        if len(close_waits) == len(active_symbols):
                            # Every symbol is in its close window
                            earliest = min(w[1] for w in close_waits)
                            # Add a small buffer so the first bar has time to form
                            sleep_sec = earliest + 30
                            logger.info(
                                "All active symbols in market-close window — "
                                "sleeping until reopen",
                                extra={
                                    "symbols": [w[0] for w in close_waits],
                                    "sleep_sec": sleep_sec,
                                },
                            )
                            await asyncio.sleep(sleep_sec)
                            continue
                    
                    # Wait for next 5min close
                    next_bar = await await_next_bar_close("5m", "UTC")
                    wait_sec = (next_bar - datetime.now(timezone.utc)).total_seconds()
                    
                    if wait_sec > 0:
                        await asyncio.sleep(wait_sec)
                    
                    # Buffer for broker finalization
                    await asyncio.sleep(self.config.candle_buffer_sec)
                    
                    # Clear candle cache for new cycle
                    self._candle_cache.clear()
                    
                    logger.info("Candle closed - executing", extra={
                        "time": next_bar.isoformat(),
                        "strategies": self.active_count,
                    })
                    
                    # Run all strategies
                    await self._run_all()
                    
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    self.metrics.record_error()
                    logger.error("Loop error", extra={
                        "error": str(e),
                        "type": type(e).__name__,
                    })
                    await asyncio.sleep(self.config.error_retry_sec)
        
        except asyncio.CancelledError:
            logger.info("Loop cancelled")
        finally:
            await self._cleanup()
            logger.info("Loop ended")
    
    async def _health_check(self) -> None:
        """Perform health check and recovery actions."""
        now = datetime.now(timezone.utc)
        
        # Check if health check is due
        if (now - self._last_health_check).total_seconds() < self.config.health_check_interval_sec:
            return
        
        self._last_health_check = now
        
        # Check for stuck strategies (no progress in 10+ minutes)
        stuck_strategies = []
        for sid, strat in self._strategies.items():
            if strat.last_run and (now - strat.last_run).total_seconds() > 600:  # 10 minutes
                stuck_strategies.append(sid)
        
        if stuck_strategies:
            logger.warning("Stuck strategies detected", extra={
                "stuck_count": len(stuck_strategies),
                "strategy_ids": stuck_strategies,
            })
        
        # Update metrics
        self.metrics.strategies_disabled = sum(1 for s in self._strategies.values() if s.is_disabled)
        self.metrics.last_health_check = now
        
        # Log health metrics
        logger.info("Health check", extra={
            "active_strategies": self.active_count,
            "disabled_strategies": self.metrics.strategies_disabled,
            "failed_strategies": sum(1 for s in self._strategies.values() if s.failure_count > 0),
            "cached_symbols": len(self._candle_cache),
            "metrics": self.metrics.to_dict(),
        })
    
    async def _cleanup(self) -> None:
        """Clean up resources on shutdown."""
        # Wait for in-flight strategies to complete (with timeout)
        if self._semaphore.locked():
            try:
                await asyncio.wait_for(
                    asyncio.gather(*[self._semaphore.acquire() for _ in range(self.config.max_concurrent_strategies)],
                                   return_exceptions=True),
                    timeout=5.0
                )
            except asyncio.TimeoutError:
                logger.warning("Timeout waiting for strategies to complete during shutdown")
        
        # Clear caches
        self._candle_cache.clear()
        
        logger.info("Cleanup completed")
    
    async def _run_all(self) -> None:
        """Execute all active strategies on candle close with parallel processing."""
        if not self._strategies:
            return
        
        start = datetime.now(timezone.utc)
        
        # Group strategies by symbol for batch candle fetching
        strategies_by_symbol = defaultdict(list)
        expired_strategies = []
        
        for sid, strat in list(self._strategies.items()):
            if not strat.should_execute:
                if strat.is_expired:
                    expired_strategies.append(sid)
                continue
            strategies_by_symbol[strat.symbol].append(strat)
        
        # Batch fetch candles for all unique symbols.
        # Skip symbols whose market is closed BEFORE creating the fetch
        # coroutine, so we never touch the broker — or leave an un-awaited
        # coroutine dangling — during the daily-maintenance / weekend window.
        candle_tasks = []
        for symbol in strategies_by_symbol.keys():
            closed, wait_sec = _is_market_closed(symbol)
            if closed:
                logger.info(
                    "Market closed — skipping symbol until reopen",
                    extra={"symbol": symbol, "reopen_in_sec": wait_sec},
                )
                continue
            candle_tasks.append((symbol, self._get_candles_cached(symbol)))

        # Execute all candle fetches in parallel
        candle_results = {}
        for symbol, task in candle_tasks:
            try:
                candle_results[symbol] = await task
            except Exception as e:
                logger.error("Candle fetch failed for symbol", extra={
                    "symbol": symbol,
                    "error": str(e),
                    "error_type": type(e).__name__
                })
        
        # Create execution tasks for all strategies
        execution_tasks = []
        for symbol, strategies in strategies_by_symbol.items():
            if symbol not in candle_results:
                continue
                
            curr, hist = candle_results[symbol]
            for strat in strategies:
                task = self._run_one_with_semaphore(strat, curr, hist)
                execution_tasks.append(task)
        
        # Execute all strategies in parallel with concurrency control
        if execution_tasks:
            results = await asyncio.gather(*execution_tasks, return_exceptions=True)
            
            # Process results and handle failures
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.error("Strategy execution failed", extra={
                        "error": str(result),
                        "error_type": type(result).__name__
                    })
        
        # Cleanup expired strategies
        for sid in expired_strategies:
            self._expire(sid)
        
        # Process events
        await self._process_events()
        
        # Update metrics
        self.metrics.record_cycle()
        
        # Cycle summary
        duration = (datetime.now(timezone.utc) - start).total_seconds()
        logger.info("Cycle complete", extra={
            "processed": len(self._strategies),
            "expired": len(expired_strategies),
            "remaining": self.active_count,
            "duration_sec": duration,
            "symbols_fetched": len(candle_results),
            "metrics": self.metrics.to_dict(),
        })
    
    async def _run_one_with_semaphore(
        self,
        strat: Strategy,
        current: Dict[str, Any],
        history: List[Dict[str, Any]],
    ) -> None:
        """Execute single strategy with semaphore control."""
        async with self._semaphore:
            await self._run_one(strat, current, history)
    
    async def _run_one(
        self,
        strat: Strategy,
        current: Dict[str, Any],
        history: List[Dict[str, Any]],
    ) -> None:
        """Execute single strategy with timeout and circuit breaker."""
        start_time = time.time()
        
        try:
            logger.debug("Running strategy", extra={
                "id": strat.id,
                "run": strat.run_count + 1,
                "minutes_left": strat.minutes_left,
            })
            
            # Execute code with timeout
            # Lazy import avoids the circular-import chain at module load time.
            from ..code_sniper_executor import execute_code_sniper_strategy  # noqa: PLC0415
            decision = await asyncio.wait_for(
                asyncio.to_thread(
                    execute_code_sniper_strategy,
                    code_signal=strat.signal,
                    current_candle=current,
                    historical_candles=history,
                    context=strat.context,
                    asset=strat.asset,
                    symbol=strat.symbol,
                    risk_service=self.risk,
                ),
                timeout=self.config.strategy_timeout_sec
            )
            
            # Update tracking and metrics
            execution_time_ms = (time.time() - start_time) * 1000
            strat.last_run = datetime.now(timezone.utc)
            strat.run_count += 1
            strat.failure_count = 0  # Reset failure count on success
            
            self.metrics.update_execution_time(execution_time_ms)
            self.metrics.record_success()
            
            # Log output
            logger.info("Strategy output", extra={
                "id": strat.id,
                "side": decision.side.value if decision.side else None,
                "entry": decision.entry,
                "qty": decision.quantity,
                "execution_time_ms": round(execution_time_ms, 2),
            })
            
            # Handle trade
            if strat.context.get("typed_status") in ("invalidated", "expired", "missed_trigger"):
                self.remove_strategy(strat.id)
                return
            if decision.quantity > 0:
                await self._execute_trade(strat, decision)
            else:
                logger.debug("No setup - monitoring continues", extra={
                    "id": strat.id,
                    "minutes_left": strat.minutes_left,
                })
        
        except CandleContractError as e:
            # Unfit candles: skip this bar and try the next one. Deliberately NOT
            # a strategy failure — three bad bars from the feed must not retire a
            # working judge program via the circuit breaker.
            logger.warning("Skipping bar — candle contract violated", extra={
                "id": strat.id,
                "symbol": strat.symbol,
                "error": str(e),
            })
            return

        except asyncio.TimeoutError:
            execution_time_ms = (time.time() - start_time) * 1000
            strat.failure_count += 1
            error_msg = f"Strategy execution timeout after {self.config.strategy_timeout_sec}s"
            
            self.metrics.update_execution_time(execution_time_ms)
            self.metrics.record_failure("TimeoutError")
            
            logger.error("Strategy timeout", extra={
                "id": strat.id,
                "timeout_sec": self.config.strategy_timeout_sec,
                "failure_count": strat.failure_count,
                "execution_time_ms": round(execution_time_ms, 2),
            })
            await self._handle_strategy_failure(strat, error_msg, "TimeoutError")
            
        except Exception as e:
            execution_time_ms = (time.time() - start_time) * 1000
            strat.failure_count += 1
            
            self.metrics.update_execution_time(execution_time_ms)
            self.metrics.record_failure(type(e).__name__)
            
            logger.error("Strategy execution error", extra={
                "id": strat.id,
                "error": str(e),
                "failure_count": strat.failure_count,
                "execution_time_ms": round(execution_time_ms, 2),
            })
            await self._handle_strategy_failure(strat, str(e), type(e).__name__)
    
    async def _execute_trade(self, strat: Strategy, decision: Decision) -> None:
        """Execute trade and setup monitoring."""
        try:
            # Authoritative single-position guard. The in-memory has_active_trade()
            # flag lives on the AgentRunner, but a fresh AgentRunner (and a fresh
            # CodeSniperMonitor) is created every cycle (see api.py), so a flag set
            # by the runner that opened a position is invisible to the next cycle's
            # monitor — that gap let BTC open a buy and an opposite sell at once.
            # The broker is the only source of truth that survives the re-creation,
            # so check it directly before placing. Mirrors the scheduled-path guard
            # in runner.py (suffix-safe via startswith, e.g. BTCUSDm → BTCUSD).
            #
            # broker.positions() is a synchronous MT5 IPC call and must run in a
            # worker thread to avoid blocking the uvicorn event loop.
            #
            # 2026-07-22: the same fetch also feeds the CORRELATED-asset guard
            # below, so both checks cost ONE broker round-trip, not two.
            # positions_raw() is preferred because it preserves MT5's `type`
            # field (0=BUY/1=SELL); Position exposes only an unsigned volume, so
            # the side — which the correlation check needs — is unrecoverable
            # from positions(). Falls back to positions() for brokers/test
            # doubles that lack the raw accessor (the same-asset guard is
            # side-agnostic and still works; the correlation check degrades to a
            # no-op rather than guessing).
            if hasattr(self.broker, "positions_raw") or hasattr(self.broker, "positions"):
                try:
                    fetch = getattr(self.broker, "positions_raw", None) or self.broker.positions
                    open_positions = await asyncio.to_thread(fetch)
                    asset_upper = (decision.asset or strat.asset or "").upper()
                    existing = [
                        p for p in open_positions
                        if _position_symbol(p).upper().startswith(asset_upper)
                    ]
                    if existing:
                        logger.warning(
                            "Skipping code-sniper entry — position already open at broker",
                            extra={
                                "id": strat.id,
                                "symbol": decision.symbol,
                                "requested_side": decision.side.value if decision.side else None,
                                "open_positions": len(existing),
                            },
                        )
                        # Leave the strategy registered so it can still act once the
                        # existing position closes (within its TTL); just skip this
                        # entry to avoid a second / opposite simultaneous trade.
                        return

                    # Correlated-instrument coherence: never hold gold and silver
                    # on opposite sides. Validated on 3y of live data — XAU/XAG
                    # agree on 77-80% of bars and 97-100% of the largest moves,
                    # so an opposing pair is betting against itself. See
                    # config.CORRELATION_GROUPS for the evidence and the
                    # deliberate decision NOT to block same-side pairs.
                    conflicts = _opposing_correlated_positions(
                        asset_upper, decision.side, open_positions
                    )
                    if conflicts:
                        conflicting_symbols = [_position_symbol(p) for p in conflicts]
                        # The file log renders %(message)s ONLY — anything left in
                        # extra={} is invisible in results/logs and therefore
                        # invisible to the audit. Keep the facts in the message.
                        logger.warning(
                            "Skipping code-sniper entry — opposing position in correlated "
                            f"asset: {decision.symbol} {decision.side.value if decision.side else '?'} "
                            f"blocked by open {', '.join(conflicting_symbols)}",
                            extra={
                                "id": strat.id,
                                "symbol": decision.symbol,
                                "requested_side": decision.side.value if decision.side else None,
                                "conflicting_symbols": conflicting_symbols,
                                "conflicting_count": len(conflicts),
                            },
                        )
                        # Same as above: stay registered and retry within TTL once
                        # the correlated position closes.
                        return
                except Exception as e:
                    # Unknown exposure is not an empty account. Retain the
                    # setup for a later qualified read; manage open trades as usual.
                    logger.error(
                        "Pre-trade broker position check failed — skipping entry",
                        extra={"id": strat.id, "error": str(e), "type": type(e).__name__},
                    )
                    return

            # Pre-trade margin check – catch insufficient margin before
            # hitting the broker so we get a clear log and avoid the error
            # propagating as a generic strategy failure.
            #
            # ALL broker calls below (balances, ensure_margin, place) are
            # synchronous MT5 IPC functions.  They MUST run in a thread pool
            # thread via asyncio.to_thread; calling them directly from this
            # async method would block the entire uvicorn event loop.
            if self.risk is not None:
                try:
                    broker = self.broker
                    if broker is not None and hasattr(broker, "balances"):
                        balances = await asyncio.to_thread(broker.balances)
                        self.risk.update_balances(balances)
                    if not await asyncio.to_thread(self.risk.ensure_margin, decision):
                        avail = self.risk.available_funds()
                        logger.warning("Insufficient margin - skipping trade", extra={
                            "id": strat.id,
                            "symbol": decision.symbol,
                            "quantity": decision.quantity,
                            "entry": decision.entry,
                            "available_funds": avail,
                        })
                        return
                except AttributeError:
                    pass  # broker missing order_margin – let place() handle it

            # Scheduled-release blackout (LP-04). Placed with the other entry
            # brakes and BEFORE the order is built: the point is not to be in
            # the market across a release print, and the 07-14 CPI exhibit
            # gapped $22.66 straight through its stop, so no stop geometry
            # substitutes for this. Blocks new entries only — open positions
            # stay with the trail engine.
            from ..entry_policy import evaluate_entry_policy
            from ..exit_policy import family_exit_rules
            setup = strat.signal.metadata.get("typed_setup", {})
            policy = evaluate_entry_policy(decision.asset or strat.asset, datetime.now(timezone.utc),
                horizon_minutes=setup.get("expected_horizon_minutes", strat.signal.metadata.get("expected_horizon_minutes", 0)),
                static_policy=lambda asset, now: news_blackout_active(asset))
            blackout, blackout_reason = policy["blocked"], policy["reason"]
            if blackout:
                logger.warning(
                    f"Scheduled-release blackout — skipping entry: {blackout_reason}",
                    extra={
                        "id": strat.id,
                        "symbol": decision.symbol,
                        "asset": decision.asset or strat.asset,
                        "reason": "news_blackout",
                    },
                )
                return

            # Account-level daily drawdown kill switch
            halted, halt_reason = await account_trading_halted(self.broker)
            if halted:
                logger.warning(f"Account daily DD guard triggered — skipping trade: {halt_reason}")
                return

            # Over-trading brake: per-asset daily trade cap + consecutive-SL
            # cooldown (restart-safe, read-only, fail-open). The legacy in-memory
            # DailyGuards never runs on this live path, so this restores both
            # brakes here. See packages/core/risk/trade_throttle.py.
            throttle_asset = decision.asset or strat.asset
            if callable(getattr(self.broker, "operational_snapshot", None)):
                throttled, throttle_reason = await trading_throttled(throttle_asset, broker=self.broker)
            else:
                throttled, throttle_reason = await trading_throttled(throttle_asset)
            if throttled:
                logger.warning(
                    f"Trade throttle — skipping entry: {throttle_reason}",
                    extra={"id": strat.id, "symbol": decision.symbol, "asset": throttle_asset},
                )
                return

            # Place order — broker.place() calls mt5.symbol_select,
            # mt5.symbol_info_tick, mt5.order_send and has time.sleep retries;
            # all of those are synchronous and must run in a worker thread.
            #
            # TP2 runner: the native bracket carries the SL only. TP2 is
            # managed by the TradeMonitor (partial close + trailed runner)
            # instead of a full native close — see config.TP2_RUNNER_ENABLED.
            # Only strip the TP when a monitor is actually attached, so a
            # missing TradeMonitor can never leave a position TP-less.
            from ..config import TP2_RUNNER_ENABLED
            place_decision = decision
            if setup.get("family") == "range_reclaim":
                from dataclasses import replace as _dc_replace
                place_decision = _dc_replace(decision, tp=decision.tp1)
            elif TP2_RUNNER_ENABLED and self.trade_mon is not None and decision.tp is not None:
                from dataclasses import replace as _dc_replace
                place_decision = _dc_replace(decision, tp=None)
                logger.info("TP2 runner active — placing native bracket with SL only", extra={
                    "id": strat.id, "symbol": decision.symbol,
                    "managed_tp2": decision.tp, "tp1": decision.tp1,
                })
            order, fills = await asyncio.to_thread(self.broker.place, place_decision)
            trade_id = None
            if callable(getattr(self.broker, "operational_snapshot", None)):
                def sync_entry():
                    from ..operational_ledger import sync_broker
                    account, ledger = sync_broker(self.broker)
                    try:
                        ledger.observe(account, order.id, "entry_receipt", {
                            "entry": order.average_price, "quantity": order.filled_quantity,
                            "sl": decision.sl, "tp1": decision.tp1, "tp2": decision.tp,
                            "setup": setup,
                        })
                        from ..operational_ledger import position_cost
                        strat.context["broker_cost"] = position_cost(
                            ledger, account, order.id,
                            getattr(self.broker, "_operational_account_currency", None),
                        )
                    finally:
                        ledger.close()
                try:
                    await asyncio.to_thread(sync_entry)
                except Exception as exc:
                    logger.error(f"Entry ledger sync needs reconciliation: {exc}")

            logger.info("Trade executed", extra={
                "id": strat.id,
                "symbol": decision.symbol,
                "side": decision.side.value,
                "qty": decision.quantity,
                "entry": decision.entry,
            })
            
            # Queue for monitoring
            if self.trade_mon:
                try:
                    try:
                        executed_entry, executed_quantity = _confirmed_execution(order, fills)
                    except ValueError:
                        # An accepted order can have an incomplete response.
                        # Recover the confirmed position by its exact broker
                        # identity before falling back to the orphan-recovery
                        # alarm; never substitute the requested size/price.
                        from types import SimpleNamespace
                        fetch_positions = getattr(self.broker, "positions_raw", None)
                        if fetch_positions is None:
                            raise
                        live_positions = await asyncio.to_thread(fetch_positions)
                        ticket = str(getattr(order, "id", "") or "")
                        matched = [
                            pos for pos in live_positions
                            if isinstance(pos, dict) and ticket
                            and ticket in {str(pos.get("ticket") or ""), str(pos.get("identifier") or "")}
                        ]
                        if len(matched) != 1:
                            raise ValueError("Cannot reconcile accepted order to one broker position")
                        live = matched[0]
                        executed_entry, executed_quantity = _confirmed_execution(
                            SimpleNamespace(average_price=live.get("price_open"), filled_quantity=live.get("volume")),
                            [],
                        )
                    # Downstream persistence receives the same confirmed fill.
                    order.average_price = executed_entry
                    order.filled_quantity = executed_quantity
                    from ..config import (
                        TP2_RUNNER_ENABLED as _RUNNER_ON,
                        RUNNER_TRAIL_ATR_MULT as _TRAIL_MULT,
                        RUNNER_TRAIL_FALLBACK_RISK_MULT as _TRAIL_FALLBACK,
                    )
                    # Chandelier distance frozen at entry (no per-candle data
                    # fetch): ATR(1h) captured by the runner cycle when
                    # available, else a multiple of the initial risk.
                    atr_1h = strat.context.get("atr_1h")
                    risk_dist = abs(executed_entry - float(decision.sl)) if decision.sl else 0.0
                    trail_dist = (
                        float(_TRAIL_MULT) * float(atr_1h)
                        if atr_1h else float(_TRAIL_FALLBACK) * risk_dist
                    ) or None
                    self.trade_mon.queue_trade(
                        symbol=decision.symbol,
                        trade_state={
                            "direction": "long" if decision.side == Side.BUY else "short",
                            "entry": executed_entry,
                            "sl": decision.sl,
                            "initial_stop": decision.sl,
                            "sl_moved": False,
                            "tp1_hit": False,
                            "tp2_hit": False,
                            "quantity": executed_quantity,
                            "asset": decision.asset,
                        },
                        rules=family_exit_rules(setup.get("family"), {
                            "tp1": decision.tp1,
                            "tp": decision.tp,
                            "move_sl_to_be": True,
                            "runner_enabled": bool(_RUNNER_ON),
                            "runner_trail_dist": trail_dist,
                        }, max_age_minutes=setup.get("max_position_age_minutes")),
                        expiry=datetime.now(timezone.utc) + timedelta(minutes=setup.get("max_position_age_minutes", self.config.trade_expiry_hours*60)),
                        prompt_hash=strat.context.get("prompt_hash", ""),
                        gpt_mode="multi_expert_code_sniper",
                        latency_ms=0.0,
                        trade_id=trade_id or "",
                        cycle_id=strat.context.get("cycle_id") or "",
                        gpt_response_id=(order.id if order else "") or "",
                        position_ticket=(order.id if order else "") or "",
                        agent_id=strat.context.get("agent_id") or "",
                    )
                    logger.info("Trade queued for monitoring", extra={
                        "symbol": decision.symbol,
                        "direction": "long" if decision.side == Side.BUY else "short",
                        "entry": executed_entry,
                        "filled_quantity": executed_quantity,
                        "sl": decision.sl,
                        "tp1": decision.tp1,
                        "tp2": decision.tp,
                        "expiry_hours": self.config.trade_expiry_hours,
                    })
                except Exception as e:
                    logger.error(
                        "CRITICAL: filled position could not be queued for management; "
                        f"reconciliation required for {decision.symbol}: {e}",
                        extra={"position_ticket": getattr(order, "id", None)},
                    )
            else:
                logger.error(
                    "CRITICAL: trade_mon is None — post-trade monitoring "
                    "(SL trail, TP1 partial, TP2 close) will NOT run",
                    extra={"id": strat.id, "symbol": decision.symbol},
                )
            
            # Record successful trade
            self.metrics.record_trade(success=True)
            
            # Remove from monitor
            self.remove_strategy(strat.id)
            
            # Emit event
            event = TradeExecuted(
                strategy_id=strat.id,
                decision=decision,
                order=order,
                trade_id=trade_id
            )
            await self._event_queue.put(event)
            
            # Callback (include strategy context for cycle_id linkage)
            if self.on_trade:
                self.on_trade(strat.id, decision, order, trade_id, strat.context)
        
        except Exception as e:
            # Record failed trade
            from packages.adapters.mt5_adapter import OrderReconciliationRequired
            if isinstance(e, OrderReconciliationRequired):
                # A timeout is not proof of non-execution. Disarm this setup
                # instead of letting the next bar submit its uncertain order again.
                self.remove_strategy(strat.id)
            self.metrics.record_trade(success=False)
            
            logger.error("Trade execution failed", extra={
                "id": strat.id,
                "error": str(e),
            })
            raise
    
    async def _get_candles_cached(
        self, 
        symbol: str
    ) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """Fetch candles with caching for current cycle."""
        # Check cache first
        if symbol in self._candle_cache:
            self.metrics.record_candle_fetch(0.0, cache_hit=True)
            return self._candle_cache[symbol]
        
        # Fetch fresh candles
        start_time = time.time()
        result = await self._get_candles(symbol)
        fetch_time_ms = (time.time() - start_time) * 1000
        
        # Record metrics
        self.metrics.record_candle_fetch(fetch_time_ms, cache_hit=False)
        
        # Cache for this cycle
        self._candle_cache[symbol] = result
        
        return result
    
    async def _get_candles(
        self, 
        symbol: str
    ) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """Fetch candles from service - fast & validated."""
        raw = await self.candles.get_candles(symbol, "5m", limit=self.config.candles_required)
        
        if not raw or len(raw) < self.config.candles_required:
            raise ValueError(
                f"Need {self.config.candles_required} candles, got {len(raw) if raw else 0}"
            )
        
        # Convert to dicts
        current = self._to_dict(raw[-1])
        history = [self._to_dict(c) for c in raw[-self.config.candles_required:-1]]
        
        if len(history) != self.config.historical_count:
            raise ValueError(f"Expected {self.config.historical_count} historical, got {len(history)}")
        
        # Check candle staleness
        candle_time = pd.Timestamp(current.get("time")).to_pydatetime()
        candle_age_minutes = (datetime.now(timezone.utc) - candle_time).total_seconds() / 60
        
        if candle_age_minutes > self.config.candle_staleness_threshold_min:
            # Reject, do not merely note it. This used to log a warning and hand
            # the stale bar to the strategy anyway, so a market order could be
            # placed against a quote that no longer existed — and the
            # fill-fidelity gate cannot catch it, because that compares the
            # trigger close to the JUDGED entry and has no way to know the
            # trigger close itself is old (audit 2026-08-27, H1). The monitor
            # wakes again in five minutes; the setup is not going anywhere.
            raise CandleContractError(
                f"Stale candle for {symbol}: newest bar is {candle_age_minutes:.1f}min "
                f"old (limit {self.config.candle_staleness_threshold_min}min)"
            )
        
        logger.debug("Candles fetched", extra={
            "symbol": symbol,
            "historical": len(history),
            "candle_age_minutes": candle_age_minutes,
        })
        
        return current, history
    
    @staticmethod
    def _to_dict(candle) -> Dict[str, Any]:
        """Convert candle to dict."""
        return {
            "time": candle.time.isoformat() if hasattr(candle.time, 'isoformat') else str(candle.time),
            "open": float(candle.open),
            "high": float(candle.high),
            "low": float(candle.low),
            "close": float(candle.close),
            "volume": float(candle.volume) if candle.volume else 0.0,
        }
    
    async def _handle_strategy_failure(self, strat: Strategy, error: str, error_type: str) -> None:
        """Handle strategy failure with circuit breaker logic."""
        if strat.failure_count >= self.config.circuit_breaker_threshold:
            strat.is_disabled = True
            logger.warning("Strategy disabled due to repeated failures", extra={
                "id": strat.id,
                "failure_count": strat.failure_count,
                "threshold": self.config.circuit_breaker_threshold,
            })
        
        # Emit failure event
        event = StrategyFailed(
            strategy_id=strat.id,
            error=error,
            error_type=error_type,
            failure_count=strat.failure_count
        )
        await self._event_queue.put(event)
    
    async def _process_events(self) -> None:
        """Process pending events from the event queue."""
        while not self._event_queue.empty():
            try:
                event = self._event_queue.get_nowait()
                if isinstance(event, TradeExecuted):
                    logger.info("Trade executed event processed", extra={
                        "strategy_id": event.strategy_id,
                        "trade_id": event.trade_id,
                    })
                elif isinstance(event, StrategyFailed):
                    logger.warning("Strategy failed event processed", extra={
                        "strategy_id": event.strategy_id,
                        "failure_count": event.failure_count,
                    })
            except asyncio.QueueEmpty:
                break
    
    def _expire(self, strategy_id: str) -> None:
        """Handle strategy expiry."""
        if strat := self._strategies.pop(strategy_id, None):
            logger.info("Strategy expired", extra={
                "id": strategy_id,
                "runs": strat.run_count,
            })
            
            # Emit expiry event
            strategy_dict = {
                "asset": strat.asset,
                "symbol": strat.symbol,
                "execution_count": strat.run_count,
                "signal": strat.signal,
                "context": strat.context,
                "expiry": strat.expiry,
                "created_at": strat.created_at,
                "last_execution": strat.last_run,
            }
            
            # Add to event queue (async context not available here)
            try:
                event = StrategyExpired(
                    strategy_id=strategy_id,
                    strategy_data=strategy_dict
                )
                asyncio.create_task(self._event_queue.put(event))
            except Exception as e:
                logger.error("Failed to queue expiry event", extra={"error": str(e)})
            
            if self.on_expire:
                self.on_expire(strategy_id, strategy_dict)


# ============================================================================
# SERVICE REGISTRY
# ============================================================================

class MonitorRegistry:
    """Thread-safe registry for code sniper monitors."""
    
    def __init__(self):
        self._monitors: Dict[str, CodeSniperMonitor] = {}
        self._lock = asyncio.Lock()
    
    async def get_monitor(self, agent_id: str) -> Optional[CodeSniperMonitor]:
        """Get monitor for agent."""
        async with self._lock:
            return self._monitors.get(agent_id)
    
    async def set_monitor(self, agent_id: str, monitor: CodeSniperMonitor) -> None:
        """Register monitor for agent."""
        async with self._lock:
            # Stop existing monitor if any
            if existing := self._monitors.get(agent_id):
                await existing.stop_monitoring()
            self._monitors[agent_id] = monitor
    
    async def remove_monitor(self, agent_id: str) -> None:
        """Remove and stop monitor for agent."""
        async with self._lock:
            if monitor := self._monitors.pop(agent_id, None):
                await monitor.stop_monitoring()
    
    async def get_all_monitors(self) -> Dict[str, CodeSniperMonitor]:
        """Get all registered monitors."""
        async with self._lock:
            return self._monitors.copy()
    
    async def shutdown_all(self) -> None:
        """Shutdown all monitors."""
        async with self._lock:
            for monitor in self._monitors.values():
                await monitor.stop_monitoring()
            self._monitors.clear()


# Global registry instance
_registry = MonitorRegistry()


# The sync wrappers `get_code_sniper_monitor` / `set_code_sniper_monitor` were
# removed on 2026-08-27. They returned None whenever an event loop was running —
# which is every call site in this system — so `if monitor is not None:` silently
# skipped the body. That made all four teardown paths no-ops, including the
# operator's Stop button (audit 2026-08-27, C2). A getter that quietly answers
# "nothing here" in the only context it is ever called from is worse than one that
# does not exist, so it no longer exists. Use the async accessors below.


async def get_code_sniper_monitor_async(agent_id: str) -> Optional[CodeSniperMonitor]:
    """The monitor registered for ``agent_id``, or None."""
    return await _registry.get_monitor(agent_id)


async def set_code_sniper_monitor_async(agent_id: str, monitor: CodeSniperMonitor) -> None:
    """Register ``monitor`` for ``agent_id``, stopping any monitor it replaces."""
    await _registry.set_monitor(agent_id, monitor)


async def remove_code_sniper_monitor(agent_id: str) -> None:
    """Stop the monitor for ``agent_id`` and drop it from the registry.

    ``MonitorRegistry.remove_monitor`` already awaits ``stop_monitoring()`` before
    dropping the entry, so this is the whole teardown — callers do not need to
    cancel the task themselves, and must not, because doing it outside the
    registry lock races with a concurrent ``set_monitor``.
    """
    await _registry.remove_monitor(agent_id)


async def shutdown_code_sniper_monitor(agent_id: str) -> bool:
    """THE way to stop an agent's strategy monitor. Returns True if one was running.

    Every stop path calls this and nothing else. Consolidating them is the fix for
    the 2026-08-27 audit's C2: four hand-rolled teardowns had drifted into four
    different (and equally broken) shapes.

    NOT called from ``AgentRunner.cleanup_resources``. That runs in a ``finally``
    after every cycle (api.py), and the monitor is process-global with a lifetime
    measured in judge TTLs — a per-cycle teardown would destroy the strategy the
    cycle just armed.
    """
    monitor = await _registry.get_monitor(agent_id)
    if monitor is None:
        logger.debug("No strategy monitor to stop", extra={"agent_id": agent_id})
        return False
    active = monitor.active_count
    await remove_code_sniper_monitor(agent_id)
    logger.info("Strategy monitor stopped and removed", extra={
        "agent_id": agent_id,
        "strategies_dropped": active,
    })
    return True


async def shutdown_all_monitors() -> None:
    """Shutdown all monitors."""
    await _registry.shutdown_all()


__all__ = [
    "CodeSniperMonitor",
    "MonitorConfig",
    "MonitorMetrics",
    "MonitorRegistry",
    "StrategyEvent",
    "StrategyExpired",
    "TradeExecuted",
    "StrategyFailed",
    "get_code_sniper_monitor_async",
    "set_code_sniper_monitor_async",
    "remove_code_sniper_monitor",
    "shutdown_code_sniper_monitor",
    "shutdown_all_monitors",
]
