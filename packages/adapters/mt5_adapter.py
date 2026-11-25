"""MetaTrader 5 adapter with lazy imports.

The real ``MetaTrader5`` package is imported only when a login attempt is made
to keep environments without the dependency functional.  The adapter provides
session management, market data helpers and basic trading operations via
``MetaTrader5.order_send``.
"""

from __future__ import annotations

from typing import Any, Dict, List, TypedDict, TYPE_CHECKING
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import asyncio
import math
import time
import pandas as pd
from prometheus_client import Counter, Histogram

from .broker_base import IBroker
from packages.core.offline_replay import offline_mode
from packages.core.candles import Candle, MarketClosedError
from packages.core.models import Decision, Fill, Order, Position
from packages.core.volume import floor_volume
from packages.core.trade_geometry import first_target_rr
from packages.db.models import WalletSnapshotDoc
from packages.core.logger import get_logger
from packages.core.config import (
    MT5_DEFAULT_MAX_RETRIES, MT5_RETRY_BASE_DELAY, MT5_CANDLE_FINALIZATION_BUFFER_SEC,
    MT5_MAGIC_NUMBER,
    MT5_STALE_MARKET_CLOSED_INTERVALS, MT5_STALE_MARKET_CLOSED_BACKOFF_SEC,
    CS_MAX_ENTRY_DEVIATION_RISK_MULT, CS_MIN_RR_RATIO_TP1,
)

if TYPE_CHECKING:
    from packages.core.services.risk import RiskService


logger = get_logger(__name__)

_mt5_bars_fetch_count = Counter(
    "mt5_bars_fetch_count", "Number of MT5 bars fetch operations"
)
_mt5_fetch_latency_ms = Histogram(
    "mt5_fetch_latency_ms", "Latency of MT5 recent bar fetch in milliseconds"
)


class Bar(TypedDict):
    """Typed representation of a market bar."""

    time: datetime  # Changed from 'ts' to 'time' for consistency
    open: float
    high: float
    low: float
    close: float
    tick_volume: int


class OrderReconciliationRequired(RuntimeError):
    """A send may have executed; do not submit the same strategy again."""


class MT5Adapter(IBroker):
    """Minimal MetaTrader5 adapter interface."""

    def __init__(
        self,
        *_,
        risk_service: "RiskService | None" = None,
        symbol_suffix: str = "",
        **__,
    ) -> None:
        """Initialize adapter without importing MT5 dependencies.

        Parameters
        ----------
        risk_service:
            Optional :class:`RiskService` used for margin checks. When omitted a
            transient service is created during order submission.
        symbol_suffix:
            Optional suffix to append to symbols for broker-specific requirements.
            Examples: ".raw", "#", ".m"
        """
        self._mt5 = None  # Lazy import holder
        self._risk = risk_service
        self._symbol_suffix = symbol_suffix
        self._credentials = {}  # Store for auto-reconnect
        self._report_account = None
        self._health_ts: float = 0.0    # monotonic ts of last health check
        self._health_ok: bool = False   # cached is_connected result

    async def connect(self, *, server: str, login: str, password: str, **_: Any) -> None:
        """Asynchronously establish a session using provided credentials."""
        # Store credentials for auto-reconnect
        self._credentials = {"server": server, "login": login, "password": password}
        logger.info(
            "MT5 connecting",
            extra={"server": server, "login": login},
        )
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self.login, server=server, login=login, password=password),
                timeout=30.0,  # mt5.initialize() / login() can block; cap at 30 s
            )
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"MT5 login timed out after 30 s (server={server}, login={login})"
            )
        logger.info("MT5 connected", extra={"server": server, "login": login})

    # ------------------------------------------------------------------
    # Symbol normalization
    # ------------------------------------------------------------------
    def _normalize_symbol(self, symbol: str) -> str:
        """
        Normalize symbol for MT5 broker requirements.
        
        Args:
            symbol: Trading symbol (e.g., "BTCUSD", "EURUSD", "XAUUSD")
        
        Returns:
            MT5-formatted symbol with broker-specific suffix if configured
        
        Examples:
            Without suffix: "btcusd" -> "BTCUSD"
            With suffix ".raw": "BTCUSD" -> "BTCUSD.raw"
            With suffix "#": "EURUSD" -> "EURUSD#"
        """
        # Normalize to uppercase and strip whitespace
        normalized = symbol.upper().strip()
        
        # Add broker-specific suffix if configured
        if self._symbol_suffix:
            normalized = f"{normalized}{self._symbol_suffix}"
        
        logger.debug(
            "Symbol normalized for MT5",
            extra={
                "original_symbol": symbol,
                "normalized_symbol": normalized,
                "suffix": self._symbol_suffix or "none"
            }
        )
        
        return normalized
    
    # ------------------------------------------------------------------
    # Authentication helpers
    # ------------------------------------------------------------------
    def login(self, *, server: str, login: str, password: str) -> None:
        """Login to a MetaTrader5 server.

        Parameters are validated for type safety and the underlying
        ``MetaTrader5`` module is imported lazily.  A ``RuntimeError`` is raised
        if either initialization or login fails.
        """

        if offline_mode():
            raise RuntimeError("MT5 login requires connected mode")
        if not all(isinstance(x, str) for x in (server, login, password)):
            raise TypeError("server, login and password must be str")

        if self._mt5 is None:  # pragma: no cover - import happens at runtime
            import MetaTrader5 as mt5  # type: ignore[import-not-found]

            self._mt5 = mt5

        if not self._mt5.initialize():
            raise RuntimeError("MetaTrader5.initialize failed")

        # ``login`` expects an integer account id
        try:
            login_id = int(login)
        except ValueError as exc:  # pragma: no cover - defensive programming
            raise TypeError("login must be numeric") from exc

        if not self._mt5.login(login=login_id, password=password, server=server):
            # Get detailed error information
            error_code = self._mt5.last_error()
            error_msg = f"MetaTrader5.login failed with error code {error_code}"
            logger.error(f"MT5 login failed", extra={
                "server": server,
                "login": login_id,
                "error_code": error_code,
                "error_message": error_msg
            })
            raise RuntimeError(error_msg)
        
        # Verify connection after login
        account_info = self._mt5.account_info()
        if account_info is None:
            error_code = self._mt5.last_error()
            error_msg = f"Failed to get account info after login, error code {error_code}"
            logger.error(f"MT5 account info failed", extra={
                "server": server,
                "login": login_id,
                "error_code": error_code
            })
            raise RuntimeError(error_msg)
        
        from packages.core.services.mt5_credentials import selected_mt5_mode

        mode = selected_mt5_mode()
        if mode and not self._matches_account(account_info, server, login, mode):
            self._health_ok = False
            self._health_ts = time.monotonic()
            raise RuntimeError("MT5 terminal account did not match the selected startup account")

        # Auto-detect trial/demo server and set symbol suffix
        server_lower = server.lower()
        if ("trial" in server_lower or "demo" in server_lower) and not self._symbol_suffix:
            self._symbol_suffix = "m"
            logger.info("Auto-detected trial/demo server, setting symbol suffix to 'm'",
                        extra={"server": server, "suffix": "m"})

        # Warm (not invalidate) the health cache: we already verified account_info above,
        # so the connection is confirmed good. Setting _health_ts to now means the first
        # is_connected() call from the event loop thread will return the cached True without
        # making any MT5 IPC calls (which would block the event loop on Windows).
        self._report_account = (server.casefold(), int(login), getattr(account_info, "trade_mode", None))
        self._health_ts = time.monotonic()
        self._health_ok = True

        logger.info(f"MT5 login successful", extra={
            "server": server,
            "login": login_id,
            "account_balance": account_info.balance,
            "account_currency": account_info.currency,
            "symbol_suffix": self._symbol_suffix or "none"
        })

    def _matches_account(self, account, server: str, login: str, mode: str) -> bool:
        sdk_mode = {"demo": "ACCOUNT_TRADE_MODE_DEMO", "live": "ACCOUNT_TRADE_MODE_REAL"}.get(mode)
        expected = getattr(self._mt5, sdk_mode, None) if sdk_mode else None
        return (
            account is not None
            and expected is not None
            and getattr(account, "trade_mode", None) == expected
            and str(getattr(account, "login", "")) == login
            and str(getattr(account, "server", "")).casefold() == server.casefold()
        )

    def is_connected_to(self, *, server: str, login: str, mode: str) -> bool:
        """Check the terminal identity even when generic health is cached."""
        try:
            return (
                self._mt5 is not None
                and self._matches_account(self._mt5.account_info(), server, login, mode)
                and self.is_connected()
            )
        except Exception:
            return False

    def is_connected(self) -> bool:
        """Check if MT5 adapter is connected and functional.

        Results are cached for 5 seconds to avoid repeated IPC overhead.
        Called 3+ times per trading cycle; caching saves ~100-300ms.
        """
        if self._mt5 is None:
            return False

        # Fast path: return cached result within TTL
        now = time.monotonic()
        if now - self._health_ts < 5.0:
            return self._health_ok

        try:
            account_info = self._mt5.account_info()
            if account_info is None:
                logger.warning("MT5 account_info returned None - connection may be lost")
                self._health_ts = now
                self._health_ok = False
                return False

            terminal_info = self._mt5.terminal_info()
            if terminal_info is None:
                logger.warning("MT5 terminal_info returned None - terminal may not be connected")
                self._health_ts = now
                self._health_ok = False
                return False

            connected = getattr(terminal_info, 'connected', False)
            if not connected:
                logger.warning("MT5 terminal not connected to trade server")
                self._health_ts = now
                self._health_ok = False
                return False

            self._health_ts = now
            self._health_ok = True
            return True

        except Exception as e:
            logger.error(f"CRITICAL: Error checking MT5 connection status: {e}")
            self._health_ts = now
            self._health_ok = False
            return False
    
    async def ensure_connection(self, server: str = None, login: str = None, password: str = None) -> bool:
        """Ensure MT5 connection is active, attempt reconnection if needed.
        
        Returns:
            bool: True if connection is active, False if reconnection failed
        """
        try:
            if self.is_connected():
                return True
            
            logger.warning("MT5 connection lost - attempting reconnection")
            
            if not all([server, login, password]):
                logger.error("CRITICAL: Cannot reconnect MT5 - missing credentials")
                return False
            
            # Attempt reconnection
            try:
                await self.connect(server=server, login=login, password=password)
                if self.is_connected():
                    logger.info("MT5 reconnection successful")
                    return True
                else:
                    logger.error("MT5 reconnection failed - still not connected after login attempt")
                    return False
            except Exception as e:
                logger.error(f"CRITICAL: MT5 reconnection failed: {e}")
                return False
                
        except Exception as e:
            logger.error(f"CRITICAL: Error in MT5 ensure_connection: {e}")
            return False

    def _reconnect_if_needed(self) -> None:
        """Best-effort synchronous reconnect before a write operation.

        Order writes (place/modify_sl/partial_close/close) run on worker
        threads and, unlike :meth:`candles`, previously had no auto-reconnect —
        so a dropped terminal made an SL-trail or exit silently fail until the
        next candle. This re-establishes the session in-thread using the stored
        credentials. Never raises: on failure the caller proceeds and surfaces
        the real broker error as before.
        """
        try:
            if self._mt5 is not None and self.is_connected():
                return
            creds = self._credentials or {}
            if not (creds.get("server") and creds.get("login") and creds.get("password")):
                return
            logger.warning("MT5 not connected before write op — attempting reconnect")
            self.login(
                server=creds["server"],
                login=creds["login"],
                password=creds["password"],
            )
        except Exception as e:
            logger.error(f"MT5 reconnect-before-write failed: {e}")

    def disconnect(self) -> None:
        """Properly disconnect from MT5 and clean up resources."""
        try:
            if self._mt5 is not None:
                # Shutdown MT5 connection
                self._mt5.shutdown()
                logger.info("MT5 connection shutdown successfully")
                self._mt5 = None
                self._health_ts = 0.0
                self._health_ok = False
            else:
                logger.debug("MT5 already disconnected")
        except Exception as e:
            logger.error(f"Error during MT5 disconnect: {e}")
    
    def __del__(self):
        """Ensure MT5 is properly shut down when object is destroyed."""
        try:
            if hasattr(self, '_mt5') and self._mt5 is not None:
                self._mt5.shutdown()
        except Exception:
            pass  # Ignore errors during cleanup

    # ------------------------------------------------------------------
    # Account information
    # ------------------------------------------------------------------
    def account_info(self) -> WalletSnapshotDoc:
        """Return normalised wallet information for the connected account."""

        if self._mt5 is None:
            raise RuntimeError("not logged in")

        info = self._mt5.account_info()
        if info is None:  # pragma: no cover - underlying lib returns None on error
            raise RuntimeError("MetaTrader5.account_info failed")

        data = info._asdict() if hasattr(info, "_asdict") else dict(info)
        return WalletSnapshotDoc(
            broker="mt5",
            as_of=datetime.now(timezone.utc),
            equity={
                "available_margin": float(data.get("margin_free") or 0.0),
                "used_margin": float(data.get("margin") or 0.0),
                "payin_amount": float(data.get("balance") or 0.0),
            },
        )

    def wallet_info(self) -> Dict[str, Any]:
        """Return raw account information for the connected account."""

        if self._mt5 is None:
            raise RuntimeError("not logged in")

        info = self._mt5.account_info()
        if info is None:  # pragma: no cover - underlying lib returns None on error
            err = getattr(self._mt5, "last_error", lambda: ("unknown",))[0]
            raise RuntimeError(f"MetaTrader5.account_info() failed: {err}")

        return {
            "login": info.login,
            "name": getattr(info, "name", None),
            "server": getattr(info, "server", None),
            "currency": getattr(info, "currency", None),
            "balance": float(info.balance),
            "equity": float(info.equity),
            "margin": float(info.margin),
            "free_margin": float(info.margin_free),
            "leverage": int(info.leverage),
        }

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------
    @staticmethod
    def _compute_bounds(now: datetime, minutes: int, count: int) -> tuple[datetime, datetime]:
        """Return the nominal lookback and the open time of the forming bar."""
        import pandas as pd

        end_ts = pd.Timestamp(now)
        current_interval_start = end_ts.floor(f'{minutes}min')

        # The fetch uses a cutoff strictly before this boundary.
        end = current_interval_start.to_pydatetime()

        if end.tzinfo is None and now.tzinfo is not None:
            end = end.replace(tzinfo=now.tzinfo)

        start = end - timedelta(minutes=minutes * count)

        logger.debug(
            "MT5 _compute_bounds - fetching closed candles only",
            extra={
                "original_now": now.isoformat(),
                "current_interval_start": current_interval_start.isoformat(),
                "end_bound": end.isoformat(),
                "start_time": start.isoformat(),
                "interval_minutes": minutes,
                "count": count,
            }
        )

        return start, end

    async def candles(self, symbol: str, interval: str, limit: int, max_retries: int = MT5_DEFAULT_MAX_RETRIES) -> List[Candle]:
        """Return a list of :class:`packages.core.candles.Candle`.

        ``interval`` supports only ``"5m"`` and ``"15m"`` which map to the
        corresponding MetaTrader5 timeframe constants.  ``limit`` controls how
        many candles are returned counting backwards from now.
        
        Enhanced with retry mechanism for production reliability.
        With smart wait implementation, alignment failures should be rare.
        """

        if self._mt5 is None:
            raise RuntimeError("CRITICAL: MT5 not initialized - cannot fetch candles")
        if interval not in {"5m", "15m"}:
            raise ValueError(f"CRITICAL: Invalid interval '{interval}' - must be '5m' or '15m'")
        if limit <= 0:
            return []

        # ── Market-hours guard ──────────────────────────────────────────
        # During the broker's daily maintenance (~20:55-22:05 UTC) and the
        # weekend close, MT5 returns stale-or-empty candles.  Treating that as
        # a retryable fetch error burns the entire retry budget (with 5-9 s
        # sleeps) and emits CRITICAL noise — yet there is nothing to recover:
        # the broker is intentionally idle.  Detect it up-front and raise a
        # typed MarketClosedError so callers skip the cycle and sleep until
        # reopen instead of hammering a closed market.
        try:
            from packages.core.services.code_sniper_monitor import _is_market_closed
            closed, wait_sec = _is_market_closed(symbol)
        except Exception:
            # Never let the guard itself break a fetch — fail open (fetch).
            closed, wait_sec = False, 0
        if closed:
            logger.info(
                "MT5Adapter.candles() - market closed, skipping fetch",
                extra={
                    "symbol": symbol,
                    "interval": interval,
                    "seconds_until_open": wait_sec,
                    "method": "MT5Adapter.candles",
                },
            )
            raise MarketClosedError(symbol, wait_sec)

        # OPTIMIZATION: Reduced from 4 to 2 retries - alignment issues now prevented upstream
        base_delay = MT5_RETRY_BASE_DELAY
        for attempt in range(max_retries + 1):
            try:
                logger.info(
                    "MT5Adapter.candles() - attempt started",
                    extra={
                        "symbol": symbol,
                        "interval": interval,
                        "limit": limit,
                        "attempt": attempt + 1,
                        "max_retries": max_retries + 1,
                        "method": "MT5Adapter.candles"
                    }
                )
                
                # Check connection before attempting fetch.
                # MUST use asyncio.to_thread: is_connected() calls mt5.account_info() /
                # mt5.terminal_info() synchronously.  Calling those from the event loop
                # thread on Windows can deadlock the IPC pipe (MT5 COM/named-pipe is
                # not safe to call from a thread different from the one that called
                # mt5.initialize()).
                if not await asyncio.to_thread(self.is_connected):
                    if attempt < max_retries:
                        logger.warning(
                            "MT5Adapter.candles() - not connected, attempting reconnection",
                            extra={
                                "symbol": symbol,
                                "interval": interval,
                                "attempt": attempt + 1,
                                "max_retries": max_retries + 1,
                                "method": "MT5Adapter.candles"
                            }
                        )
                        # Try to reconnect using stored credentials
                        try:
                            await self.ensure_connection(**self._credentials)
                            if not await asyncio.to_thread(self.is_connected):
                                raise RuntimeError("Failed to reconnect")
                        except Exception as reconnect_error:
                            logger.error(
                                "MT5Adapter.candles() - reconnection failed",
                                extra={
                                    "symbol": symbol,
                                    "interval": interval,
                                    "attempt": attempt + 1,
                                    "error": str(reconnect_error),
                                    "method": "MT5Adapter.candles"
                                }
                            )
                            if attempt == max_retries:
                                raise RuntimeError(f"CRITICAL: MT5 connection failed after all retry attempts: {reconnect_error}")
                            
                        # Wait before retry
                        delay = base_delay + (attempt * 2)  # Progressive delay: 5s, 7s, 9s
                        logger.info(
                            "MT5Adapter.candles() - waiting before retry",
                            extra={
                                "symbol": symbol,
                                "interval": interval,
                                "delay_seconds": delay,
                                "attempt": attempt + 1,
                                "method": "MT5Adapter.candles"
                            }
                        )
                        await asyncio.sleep(delay)
                        continue
                    else:
                        raise RuntimeError("CRITICAL: MT5 connection failed after all retry attempts")
                
                result = await self._fetch_candles_internal(symbol, interval, limit)
                
                logger.info(
                    "MT5Adapter.candles() - success",
                    extra={
                        "symbol": symbol,
                        "interval": interval,
                        "limit": limit,
                        "result_count": len(result),
                        "attempt": attempt + 1,
                        "method": "MT5Adapter.candles"
                    }
                )
                return result

            except MarketClosedError:
                # Feed frozen behind end time ⇒ market halted (holiday / early
                # close / outage).  Not retryable — bubble straight up so the
                # caller skips the cycle and backs off instead of re-burning
                # the retry+reconnect budget against a closed market.
                raise
            except asyncio.TimeoutError as e:
                if attempt < max_retries:
                    delay = base_delay + (attempt * 2)  # Progressive delay: 5s, 7s, 9s
                    logger.warning(
                        "MT5Adapter.candles() - timeout, retrying",
                        extra={
                            "symbol": symbol,
                            "interval": interval,
                            "attempt": attempt + 1,
                            "max_retries": max_retries + 1,
                            "delay_seconds": delay,
                            "error": str(e),
                            "method": "MT5Adapter.candles"
                        }
                    )
                    await asyncio.sleep(delay)
                    continue
                else:
                    logger.error(
                        "MT5Adapter.candles() - timeout after all attempts",
                        extra={
                            "symbol": symbol,
                            "interval": interval,
                            "max_retries": max_retries + 1,
                            "error": str(e),
                            "method": "MT5Adapter.candles"
                        }
                    )
                    raise RuntimeError(f"CRITICAL: Failed to fetch candles for {symbol} - timeout after {max_retries + 1} attempts")
            except Exception as e:
                if attempt < max_retries:
                    delay = base_delay + (attempt * 2)  # Progressive delay: 5s, 7s, 9s
                    logger.warning(
                        "MT5Adapter.candles() - fetch failed, retrying",
                        extra={
                            "symbol": symbol,
                            "interval": interval,
                            "attempt": attempt + 1,
                            "max_retries": max_retries + 1,
                            "delay_seconds": delay,
                            "error": str(e),
                            "method": "MT5Adapter.candles"
                        }
                    )
                    await asyncio.sleep(delay)
                    continue
                else:
                    logger.error(
                        "MT5Adapter.candles() - failed after all attempts",
                        extra={
                            "symbol": symbol,
                            "interval": interval,
                            "max_retries": max_retries + 1,
                            "error": str(e),
                            "method": "MT5Adapter.candles"
                        }
                    )
                    raise RuntimeError(f"CRITICAL: Failed to fetch candles for {symbol} after {max_retries + 1} attempts: {e}")

    async def htf_candles(self, symbol: str, interval: str, limit: int) -> List[Candle]:
        """Fetch higher-timeframe candles (1h/4h/1d, also 5m/15m) for CONTEXT only.

        Lightweight, additive sibling to :meth:`candles`, used by the optional
        higher-timeframe context path. It deliberately skips the 5m/15m
        alignment-retry machinery (HTF bars don't need candle-close alignment)
        and NEVER raises into the trading cycle — on any error it returns ``[]``
        so callers degrade gracefully. The forming (in-progress) last bar is
        stripped so only closed candles are returned.
        """
        mt5 = self._mt5
        if mt5 is None or limit <= 0:
            return []
        tf_suffix = {"5m": "M5", "15m": "M15", "1h": "H1", "4h": "H4", "1d": "D1"}.get(interval.lower())
        tf_const = getattr(mt5, f"TIMEFRAME_{tf_suffix}", None) if tf_suffix else None
        if tf_const is None:
            logger.warning("htf_candles: unsupported interval %s", interval)
            return []
        sym = self._normalize_symbol(symbol)
        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(mt5.copy_rates_from_pos, sym, tf_const, 0, int(limit) + 1),
                timeout=15.0,
            )
        except Exception as e:  # never break the cycle on HTF context
            logger.warning("htf_candles fetch failed for %s %s: %s", sym, interval, e)
            return []
        if raw is None or len(raw) == 0:
            return []
        out: List[Candle] = []
        for row in raw:
            ts = pd.to_datetime(int(row["time"]), unit="s", utc=True)
            names = row.dtype.names if (hasattr(row, "dtype") and getattr(row.dtype, "names", None)) else ()
            if "tick_volume" in names:
                vol = int(row["tick_volume"])
            elif "real_volume" in names:
                vol = int(row["real_volume"])
            elif "volume" in names:
                vol = int(row["volume"])
            else:
                vol = None
            out.append(
                Candle(
                    time=ts,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(vol) if vol is not None else None,
                )
            )
        # Strip the forming (in-progress) last bar so only closed candles remain.
        if len(out) > 1:
            out = out[:-1]
        return out[-int(limit):]

    async def _fetch_candles_internal(self, symbol: str, interval: str, limit: int) -> List[Candle]:
        """Internal candle fetching logic."""

        mt5 = self._mt5
        # Normalize symbol for broker (e.g. BTCUSD -> BTCUSDm on trial servers)
        symbol = self._normalize_symbol(symbol)
        timeframe = {
            "5m": getattr(mt5, "TIMEFRAME_M5"),
            "15m": getattr(mt5, "TIMEFRAME_M15"),
        }[interval]

        minutes = 5 if interval == "5m" else 15
        now = datetime.now(timezone.utc)
        start, end = self._compute_bounds(now, minutes, limit)
        remaining = (end - now).total_seconds() + MT5_CANDLE_FINALIZATION_BUFFER_SEC
        if remaining > 0:
            await asyncio.sleep(remaining)
            now = datetime.now(timezone.utc)
            start, end = self._compute_bounds(now, minutes, limit)
        # MT5 includes bars with open time equal to the cutoff. Request a
        # count of closed bars so weekend and rollover gaps never shorten it.
        cutoff = end - timedelta(seconds=1)
        # Record metrics for monitoring
        _mt5_bars_fetch_count.inc()
        fetch_start_time = time.time()
        
        logger.info(
            "ENHANCED: Fetching candles with connection validation",
            extra={
                "symbol": symbol,
                "interval": interval,
                "now": now.isoformat(),
                "start": start.isoformat(),
                "end": end.isoformat(),
                "limit": limit,
                "time_range_minutes": (end - start).total_seconds() / 60,
                "connection_status": self.is_connected(),
                "operation": "mt5_candle_fetch"
            },
        )
        
        # Enhanced fetch with connection validation
        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(mt5.copy_rates_from, symbol, timeframe, cutoff, limit),
                timeout=10.0  # MT5 IPC should respond in <5s; 10s is generous
            )
        except asyncio.TimeoutError:
            logger.error(f"CRITICAL: MT5 copy_rates_from timeout for {symbol} {interval}")
            raise RuntimeError(f"CRITICAL: MT5 data fetch timeout for {symbol} {interval}")
        except Exception as e:
            logger.error(f"CRITICAL: MT5 copy_rates_from failed for {symbol} {interval}: {e}")
            raise RuntimeError(f"CRITICAL: MT5 data fetch failed for {symbol} {interval}: {e}")
        
        # Validate raw data integrity
        if raw is None:
            error_code = mt5.last_error() if hasattr(mt5, 'last_error') else 'unknown'
            raise RuntimeError(f"CRITICAL: MT5 copy_rates_from returned None for {symbol} {interval} (error: {error_code})")
        
        if hasattr(raw, '__len__') and len(raw) == 0:
            logger.warning(f"MT5 returned empty data for {symbol} {interval} - may be outside market hours")
            # Don't raise error for empty data during off-hours, but log it
            return []

        # Count-based terminal history can retain an early partial snapshot of
        # an older bar. Refresh the requested wall-clock range as well, and let
        # that response replace matching timestamps. Keep count-based bars across
        # weekend/session gaps so refreshing never shortens the requested history.
        if callable(getattr(mt5, "copy_rates_range", None)):
            revised = await asyncio.wait_for(
                asyncio.to_thread(mt5.copy_rates_range, symbol, timeframe, start, cutoff),
                timeout=10.0,
            )
            if revised is None:
                raise RuntimeError(f"MT5 range history refresh failed for {symbol} {interval}: {mt5.last_error()}")
            by_time = {int(row["time"]): row for row in raw}
            by_time.update({int(row["time"]): row for row in revised})
            raw = [by_time[stamp] for stamp in sorted(by_time) if stamp < end.timestamp()][-limit:]

        # A live quote plus an old history response indicates terminal history
        # synchronization, not evidence that the market is shut. Try one bounded
        # alternate read before the existing stale/frozen-feed checks. Quiet or
        # closed markets keep their no-retry path; never reconnect/switch accounts.
        closed_times = [int(row["time"]) for row in raw if int(row["time"]) < end.timestamp()]
        if closed_times and end.timestamp() - max(closed_times) > minutes * 60 * 2:
            if callable(getattr(mt5, "symbol_info_tick", None)) and callable(getattr(mt5, "copy_rates_from_pos", None)):
                try:
                    tick = await asyncio.wait_for(
                        asyncio.to_thread(mt5.symbol_info_tick, symbol), timeout=2.0
                    )
                    tick_age = datetime.now(timezone.utc).timestamp() - float(tick.time) if tick else float("inf")
                    if 0 <= tick_age <= 60:
                        refreshed = await asyncio.wait_for(
                            asyncio.to_thread(mt5.copy_rates_from_pos, symbol, timeframe, 0, limit + 1),
                            timeout=10.0,
                        )
                        refreshed = [row for row in refreshed if int(row["time"]) < end.timestamp()] if refreshed is not None else []
                        if refreshed and int(refreshed[-1]["time"]) > max(closed_times):
                            raw = refreshed[-limit:]
                            logger.info(
                                "MT5 history refreshed from current bars after stale response",
                                extra={"symbol": symbol, "interval": interval},
                            )
                except Exception as exc:
                    logger.warning(
                        "MT5 bounded history refresh failed",
                        extra={"symbol": symbol, "interval": interval, "error": str(exc)},
                    )
        
        # Record fetch latency metrics
        fetch_latency_ms = (time.time() - fetch_start_time) * 1000
        _mt5_fetch_latency_ms.observe(fetch_latency_ms)
        
        # Log what MT5 actually returned
        logger.info(
            "MT5 raw data received",
            extra={
                "symbol": symbol,
                "interval": interval,
                "requested_limit": limit,
                "raw_count": len(raw),
                "raw_first_time": pd.to_datetime(int(raw[0]["time"]), unit="s", utc=True).isoformat() if len(raw) > 0 else None,
                "raw_last_time": pd.to_datetime(int(raw[-1]["time"]), unit="s", utc=True).isoformat() if len(raw) > 0 else None,
                "fetch_latency_ms": fetch_latency_ms,
                "operation": "mt5_candle_fetch_success"
            },
        )

        out: List[Candle] = []
        # Always take the most recent candles, regardless of how many we requested
        for row in raw:
            ts = pd.to_datetime(int(row["time"]), unit="s", utc=True)
            if ts >= end:
                continue
            
            # Handle volume data - try different field names that MT5 might use
            vol = None
            if hasattr(row, 'dtype') and hasattr(row.dtype, 'names') and row.dtype.names is not None:
                # NumPy structured array - check available field names
                field_names = row.dtype.names
                if 'tick_volume' in field_names:
                    vol = int(row["tick_volume"])
                elif 'tick_volum' in field_names:  # Note: MT5 sometimes uses 'tick_volum'
                    vol = int(row["tick_volum"])
                elif 'volume' in field_names:
                    vol = int(row["volume"])
                elif 'real_volum' in field_names:  # Note: MT5 sometimes uses 'real_volum'
                    vol = int(row["real_volum"])
            else:
                # Fallback for dict-like access
                vol = row.get("tick_volume") or row.get("tick_volum") or row.get("volume") or row.get("real_volum")
                if vol is not None:
                    vol = int(vol)
            
            out.append(
                Candle(
                    time=ts,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(vol) if vol is not None else None,
                )
            )
        
        # Validate last candle matches what we requested from broker.
        # Broker data delivery can lag by up to 1 interval — only retry
        # when the gap exceeds 2 full intervals (truly stale data).
        if out:
            last_candle_time = out[-1].time
            time_diff = abs((end - last_candle_time).total_seconds())
            interval_seconds = minutes * 60

            # Frozen feed far behind the requested end ⇒ the market is halted
            # (holiday / early close / broker outage) even though the scheduled-
            # close guard reported "open".  Retrying cannot conjure bars the
            # broker isn't producing, so raise the typed MarketClosedError
            # (bubbles straight up — candle_service/runner already skip the
            # cycle on it) instead of a retryable ValueError that burns the
            # whole multi-layer retry+reconnect budget against a closed market.
            if time_diff > interval_seconds * MT5_STALE_MARKET_CLOSED_INTERVALS:
                logger.info(
                    "MT5._fetch_candles_internal() - feed frozen far behind end, treating as market closed",
                    extra={
                        "symbol": symbol,
                        "interval": interval,
                        "requested_end": end.isoformat(),
                        "actual_last_candle": last_candle_time.isoformat(),
                        "time_diff_seconds": time_diff,
                        "intervals_behind": time_diff / interval_seconds,
                        "method": "MT5Adapter._fetch_candles_internal",
                    },
                )
                raise MarketClosedError(symbol, MT5_STALE_MARKET_CLOSED_BACKOFF_SEC)

            if time_diff > interval_seconds * 2:
                logger.warning(
                    "MT5._fetch_candles_internal() - Last candle too far from requested end time",
                    extra={
                        "symbol": symbol,
                        "interval": interval,
                        "requested_end": end.isoformat(),
                        "actual_last_candle": last_candle_time.isoformat(),
                        "time_diff_seconds": time_diff,
                        "intervals_behind": time_diff / interval_seconds,
                        "method": "MT5Adapter._fetch_candles_internal"
                    }
                )
                raise ValueError(f"Last candle {last_candle_time} doesn't match requested end time {end} for {interval}")
            elif time_diff > interval_seconds:
                logger.debug(
                    "MT5._fetch_candles_internal() - Last candle slightly behind (normal broker latency)",
                    extra={
                        "symbol": symbol,
                        "interval": interval,
                        "time_diff_seconds": time_diff,
                    }
                )
        
        # Log the actual candle times returned for debugging
        if out:
            logger.info(
                "MT5._fetch_candles_internal() - candles fetched successfully",
                extra={
                    "symbol": symbol,
                    "interval": interval,
                    "count": len(out),
                    "first_candle_time": out[0].time.isoformat(),
                    "last_candle_time": out[-1].time.isoformat(),
                    "current_time": now.isoformat(),
                    "last_candle_age_minutes": (now - out[-1].time).total_seconds() / 60,
                    "data_source": "mt5_broker_raw",
                },
            )
        
        if len(out) < limit:
            raise RuntimeError(
                f"Insufficient closed candles for {symbol} {interval}: "
                f"expected {limit}, got {len(out)}"
            )
        return out[-limit:]

    async def ticker(self, symbol: str, max_retries: int = 2) -> Dict[str, Any]:
        """Return comprehensive ticker data for ``symbol`` with enhanced reliability."""

        if self._mt5 is None:
            raise RuntimeError("CRITICAL: MT5 not initialized - cannot get ticker")
        
        # Connection reliability check with retry mechanism
        for attempt in range(max_retries + 1):
            try:
                # Check connection before attempting fetch
                if not self.is_connected():
                    if attempt < max_retries:
                        logger.warning(f"MT5 not connected for ticker {symbol} on attempt {attempt + 1}")
                        await asyncio.sleep(0.5)
                        continue
                    else:
                        raise RuntimeError("CRITICAL: MT5 connection failed for ticker fetch")
                
                tick = await asyncio.wait_for(
                    asyncio.to_thread(self._mt5.symbol_info_tick, symbol),
                    timeout=5.0  # Tick data is a fast IPC call
                )
                
                if tick is None:
                    error_code = self._mt5.last_error() if hasattr(self._mt5, 'last_error') else 'unknown'
                    if attempt < max_retries:
                        logger.warning(f"MT5 symbol_info_tick returned None for {symbol} (error: {error_code}) - retrying")
                        await asyncio.sleep(0.5)
                        continue
                    else:
                        raise RuntimeError(f"CRITICAL: MT5 symbol_info_tick failed for {symbol} (error: {error_code})")
                
                data = tick._asdict() if hasattr(tick, "_asdict") else dict(tick)
                
                # Validate ticker data integrity
                last_price = float(data.get("last") or data.get("bid") or data.get("ask") or 0.0)
                if last_price <= 0:
                    if attempt < max_retries:
                        logger.warning(f"Invalid ticker price for {symbol}: {last_price} - retrying")
                        await asyncio.sleep(0.5)
                        continue
                    else:
                        raise RuntimeError(f"CRITICAL: Invalid ticker price for {symbol}: {last_price}")
                
                # Return comprehensive ticker data
                return {
                    "symbol": symbol,
                    "last": last_price,
                    "bid": float(data.get("bid", last_price)),
                    "ask": float(data.get("ask", last_price)),
                    "high": float(data.get("high", last_price)),
                    "low": float(data.get("low", last_price)),
                    "time": datetime.now(timezone.utc),
                    "mt5_time": data.get("time")
                }
                
            except Exception as e:
                if attempt < max_retries:
                    logger.warning(f"MT5 ticker fetch failed for {symbol} on attempt {attempt + 1}: {e} - retrying")
                    await asyncio.sleep(0.5)
                    continue
                else:
                    logger.error(f"CRITICAL: MT5 ticker fetch failed for {symbol} after {max_retries + 1} attempts: {e}")
                    raise RuntimeError(f"CRITICAL: Failed to get ticker for {symbol} after {max_retries + 1} attempts: {e}")

    async def get_recent_bars(
        self, symbol: str, timeframe: str = None, count: int = None, *, n5: int = 0, n15: int = 0
    ) -> List[Bar] | tuple[List[Bar], List[Bar]]:
        """Return recent bars for ``symbol`` and ``timeframe``.

        This method supports two calling patterns:
        1. get_recent_bars(symbol, timeframe, count) - returns List[Bar]
        2. get_recent_bars(symbol, n5=n5, n15=n15) - returns tuple[List[Bar], List[Bar]]

        Parameters
        ----------
        symbol: str
            Market symbol to fetch bars for
        timeframe: str, optional
            Timeframe string (e.g., "M5", "M15") for single timeframe mode
        count: int, optional
            Number of bars to fetch for single timeframe mode
        n5: int, optional
            Number of 5-minute bars to fetch for multi-timeframe mode
        n15: int, optional
            Number of 15-minute bars to fetch for multi-timeframe mode

        Returns
        -------
        List[Bar] or tuple[List[Bar], List[Bar]]
            Single timeframe returns List[Bar], multi-timeframe returns tuple
        """
        
        logger.info(
            "get_recent_bars called",
            extra={
                "symbol": symbol,
                "timeframe": timeframe,
                "count": count,
                "n5": n5,
                "n15": n15,
                "mode": "multi-timeframe" if (n5 > 0 or n15 > 0) else "single-timeframe"
            }
        )
        
        # Multi-timeframe mode (for agent runner compatibility)
        if n5 > 0 or n15 > 0:
            logger.info("Fetching multi-timeframe bars", extra={"n5": n5, "n15": n15})
            bars5 = await self._get_recent_bars_single(symbol, "M5", n5) if n5 > 0 else []
            bars15 = await self._get_recent_bars_single(symbol, "M15", n15) if n15 > 0 else []
            logger.info("Multi-timeframe fetch complete", extra={"bars5": len(bars5), "bars15": len(bars15)})
            return bars5, bars15
        
        # Single timeframe mode (original behavior)
        if timeframe is None or count is None:
            raise ValueError("timeframe and count are required for single timeframe mode")
        logger.info("Fetching single timeframe bars", extra={"timeframe": timeframe, "count": count})
        return await self._get_recent_bars_single(symbol, timeframe, count)

    async def _get_recent_bars_single(self, symbol: str, timeframe: str, count: int) -> List[Bar]:
        """Internal method to fetch bars for a single timeframe."""

        logger.info("_get_recent_bars_single called", extra={"symbol": symbol, "timeframe": timeframe, "count": count})
        
        if self._mt5 is None:
            logger.error("MT5 adapter not logged in - self._mt5 is None")
            raise RuntimeError("not logged in")
        
        # Check MT5 terminal status — run in a thread to avoid blocking the event loop
        # (mt5.terminal_info() is a synchronous IPC call that must NOT run on the main thread)
        try:
            terminal_info = await asyncio.to_thread(self._mt5.terminal_info)
            if terminal_info is None:
                logger.error("MT5 terminal_info() returned None - terminal may not be running")
                raise RuntimeError("MT5 terminal not accessible")
            
            # Check if MT5 is connected
            connected = getattr(terminal_info, 'connected', False)
            logger.info("MT5 terminal connected status", extra={"connected": connected})
            
            if not connected:
                logger.error("MT5 terminal is not connected to trade server")
                raise RuntimeError("MT5 terminal not connected to trade server")
                
        except Exception as e:
            logger.error("Error checking MT5 terminal status", extra={"error": str(e)})
            # Don't raise here, let the actual data fetch attempt handle connection issues
        if count <= 0:
            logger.warning("Invalid count, returning empty list", extra={"count": count})
            return []

        mt5 = self._mt5
        tf_const = getattr(mt5, f"TIMEFRAME_{timeframe.upper()}", None)
        if tf_const is None:
            logger.error("Invalid timeframe", extra={"timeframe": timeframe})
            raise ValueError("invalid timeframe")

        logger.info("Using MT5 timeframe constant", extra={"tf_const": tf_const})
        start = time.perf_counter()
        try:
            logger.info("Selecting symbol", extra={"symbol": symbol})
            # Add timeout to prevent hanging on symbol selection
            selected = await asyncio.wait_for(
                asyncio.to_thread(mt5.symbol_select, symbol, True),
                timeout=3.0  # Symbol selection is a fast IPC call
            )
            # Safely check boolean result - MT5 symbol_select returns bool
            if selected is False or selected is None:
                logger.error("Failed to select symbol", extra={"symbol": symbol})
                raise RuntimeError("MetaTrader5.symbol_select failed")
            
            logger.info("Fetching rates from position 0", extra={"count": count})
            # Add timeout to prevent hanging on data fetch
            raw = await asyncio.wait_for(
                asyncio.to_thread(mt5.copy_rates_from_pos, symbol, tf_const, 0, count),
                timeout=8.0  # Data fetch via IPC should be fast
            )
            # Safely check if raw is None or empty - avoid array boolean ambiguity
            if raw is None or (hasattr(raw, '__len__') and len(raw) == 0):
                logger.error("copy_rates_from_pos returned None or empty")
                raise RuntimeError("MetaTrader5.copy_rates_from_pos failed")
            
            logger.info("Raw data received", extra={"bars_count": len(raw)})
        except Exception as e:
            latency_ms = (time.perf_counter() - start) * 1000.0
            _mt5_bars_fetch_count.inc()
            _mt5_fetch_latency_ms.observe(latency_ms)
            logger.exception(
                "mt5 get_recent_bars failed",
                extra={
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "count": count,
                    "latency_ms": latency_ms,
                    "error": str(e)
                },
            )
            raise
        latency_ms = (time.perf_counter() - start) * 1000.0
        _mt5_bars_fetch_count.inc()
        _mt5_fetch_latency_ms.observe(latency_ms)
        logger.info(
            "mt5 get_recent_bars success",
            extra={
                "symbol": symbol,
                "timeframe": timeframe,
                "count": count,
                "latency_ms": latency_ms,
                "bars_count": len(raw) if raw is not None and hasattr(raw, '__len__') else 0
            },
        )

        bars: List[Bar] = []
        # Safely iterate over the NumPy array
        try:
            for row in raw:
                ts = datetime.fromtimestamp(int(row["time"]), tz=timezone.utc)
                # For NumPy structured arrays, use proper field access
                tick_vol = 0
                if hasattr(row, 'dtype') and hasattr(row.dtype, 'names') and row.dtype.names is not None:
                    tick_vol = int(row["tick_volume"]) if "tick_volume" in row.dtype.names else 0
                else:
                    # Fallback for dict-like access
                    tick_vol = int(row.get("tick_volume", 0)) if hasattr(row, 'get') else 0
                
                bars.append(
                    {
                        "time": ts,  # Changed from 'ts' to 'time' for consistency
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "tick_volume": tick_vol,
                    }
                )
        except Exception as e:
            logger.error("Error processing raw bar data", extra={"error": str(e), "raw_type": type(raw).__name__})
            raise RuntimeError(f"Failed to process MT5 bar data: {e}")
        return bars


    # ------------------------------------------------------------------
    # Trading operations
    # ------------------------------------------------------------------
    def place(self, decision: Decision) -> "tuple[Order, list[Fill]]":
        """Submit *decision* using :func:`MetaTrader5.order_send`.

        The ``Decision`` is mapped to the appropriate ``TRADE_ACTION_DEAL`` or
        ``TRADE_ACTION_PENDING`` request and the resulting
        :class:`packages.core.models.Order` is returned.
        """

        self._validate_decision(decision)
        self._reconnect_if_needed()
        if self._mt5 is None:
            raise RuntimeError("not logged in")

        from packages.core.services.risk import RiskService

        if (getattr(self, "_credentials", {}) or {}).get("login"):
            from packages.core.entry_policy import evaluate_entry_policy
            from packages.core.operational_ledger import sync_broker
            from packages.core.config import BACKTEST_MAX_TRADES_PER_DAY, BACKTEST_COOLDOWN_SL_THRESHOLD, TRADE_COOLDOWN_MINUTES, TRADE_THROTTLE_ENABLED
            now = datetime.now(timezone.utc)
            policy = evaluate_entry_policy(decision.asset or decision.symbol, now)
            if policy["blocked"]:
                raise ValueError(f"Entry policy rejected broker order: {policy['reason']}")
            account, ledger = sync_broker(self, now)
            try:
                blocked, reason = ledger.throttle(account, decision.asset or decision.symbol, now,
                    cap=BACKTEST_MAX_TRADES_PER_DAY, loss_threshold=BACKTEST_COOLDOWN_SL_THRESHOLD,
                    cooldown_minutes=TRADE_COOLDOWN_MINUTES)
                if blocked and TRADE_THROTTLE_ENABLED:
                    raise ValueError(f"Operational entry control rejected order: {reason}")
            finally:
                ledger.close()

        risk = self._risk or RiskService(self)
        if self._risk is None:
            risk.update_balances(self.balances())
        risk.ensure_margin(decision, raise_error=True)

        mt5 = self._mt5
        # Determine action and order type based on side/price type
        if decision.price_type.name == "MARKET":
            action = getattr(mt5, "TRADE_ACTION_DEAL", 1)
            type_map = {
                "buy": getattr(mt5, "ORDER_TYPE_BUY", 0),
                "sell": getattr(mt5, "ORDER_TYPE_SELL", 1),
            }
        else:
            action = getattr(mt5, "TRADE_ACTION_PENDING", 5)
            type_map = {
                "buy": getattr(mt5, "ORDER_TYPE_BUY_LIMIT", 2),
                "sell": getattr(mt5, "ORDER_TYPE_SELL_LIMIT", 3),
            }

        # Normalize symbol for MT5 broker
        normalized_symbol = self._normalize_symbol(decision.symbol)

        # Ensure symbol is selected in Market Watch before placing order
        try:
            selected = mt5.symbol_select(normalized_symbol, True)
            if selected is False or selected is None:
                error_code = mt5.last_error() if hasattr(mt5, 'last_error') else 'unknown'
                logger.error("Failed to select symbol for order", extra={
                    "symbol": normalized_symbol,
                    "error_code": error_code,
                })
                raise RuntimeError(f"Cannot select symbol {normalized_symbol} in Market Watch: {error_code}")
        except RuntimeError:
            raise
        except Exception as e:
            logger.warning(f"symbol_select call failed: {e}", extra={"symbol": normalized_symbol})

        # Production safety: validate quantity and price
        if not math.isfinite(float(decision.quantity)) or float(decision.quantity) <= 0:
            raise ValueError(f"Invalid quantity {decision.quantity} for {normalized_symbol}")
        if float(decision.entry) <= 0 and decision.price_type.name == "LIMIT":
            raise ValueError(f"Invalid entry price {decision.entry} for limit order on {normalized_symbol}")

        def checked_market_price() -> float:
            """Revalidate the approved bracket at the quote we can execute."""
            tick = mt5.symbol_info_tick(normalized_symbol)
            try:
                quote = float(getattr(tick, "ask" if decision.side.value == "buy" else "bid"))
            except (AttributeError, TypeError, ValueError) as exc:
                raise ValueError(f"No executable market quote for {normalized_symbol}") from exc
            if not math.isfinite(quote) or quote <= 0:
                raise ValueError(f"Invalid executable market quote for {normalized_symbol}: {quote}")

            sign = 1.0 if decision.side.value == "buy" else -1.0
            levels = {"SL": decision.sl, "TP1": decision.tp1, "TP2": decision.tp}
            for name, level in levels.items():
                if level is not None and (not math.isfinite(float(level)) or float(level) <= 0):
                    raise ValueError(f"Invalid {name} for market order on {normalized_symbol}")
            if decision.sl is not None and sign * (quote - float(decision.sl)) <= 0:
                raise ValueError(f"Market quote has crossed SL for {normalized_symbol}")
            for name in ("TP1", "TP2"):
                if levels[name] is not None and sign * (float(levels[name]) - quote) <= 0:
                    raise ValueError(f"Market quote has crossed {name} for {normalized_symbol}")
            if decision.tp1 is not None and decision.tp is not None:
                if sign * (float(decision.tp) - float(decision.tp1)) < 0:
                    raise ValueError(f"Reversed targets for {normalized_symbol}")

            # A runner deliberately omits native TP2 but retains TP1, so this
            # gate must not depend on decision.tp being present.
            if decision.sl is not None and decision.tp1 is not None:
                planned_entry = float(decision.entry)
                planned_risk = sign * (planned_entry - float(decision.sl))
                if (not math.isfinite(planned_entry) or planned_entry <= 0
                        or planned_risk <= 0
                        or sign * (float(decision.tp1) - planned_entry) <= 0):
                    raise ValueError(f"Invalid planned market bracket for {normalized_symbol}")
                deviation = abs(quote - planned_entry)
                if deviation > CS_MAX_ENTRY_DEVIATION_RISK_MULT * planned_risk + 1e-12:
                    raise ValueError(
                        f"Executable quote deviates {deviation / planned_risk:.3f}R from approved entry "
                        f"for {normalized_symbol} (maximum {CS_MAX_ENTRY_DEVIATION_RISK_MULT}R)"
                    )
                rr_tp1 = first_target_rr(quote, decision.sl, decision.tp1)
                if rr_tp1 + 1e-9 < CS_MIN_RR_RATIO_TP1:
                    raise ValueError(
                        f"Executable quote RR to TP1 {rr_tp1:.3f} below {CS_MIN_RR_RATIO_TP1} "
                        f"for {normalized_symbol}"
                    )
            return quote

        request = {
            "action": action,
            "symbol": normalized_symbol,
            "volume": float(decision.quantity),
            "type": type_map[decision.side.value],
            "price": float(decision.entry),  # Market orders are qualified immediately before send.
            "sl": float(decision.sl) if decision.sl is not None else 0.0,
            "tp": float(decision.tp) if decision.tp is not None else 0.0,
            "deviation": 20,
            "magic": MT5_MAGIC_NUMBER,
            "type_time": getattr(mt5, "ORDER_TIME_GTC", 0),
            "type_filling": getattr(mt5, "ORDER_FILLING_IOC", 0),
        }

        # Broker lot rules are required. A failed metadata read must not
        # bypass normalization or invent a default step/minimum.
        try:
            sym_info = mt5.symbol_info(normalized_symbol)
            if sym_info is None:
                raise ValueError("Broker lot metadata unavailable")
            sym_data = sym_info._asdict() if hasattr(sym_info, "_asdict") else dict(sym_info)
            request["volume"] = floor_volume(
                decision.quantity, sym_data.get("volume_step"), sym_data.get("volume_min"),
                sym_data.get("volume_max"),
            )
        except Exception as exc:
            raise ValueError(f"Cannot qualify order volume for {normalized_symbol}: {exc}") from exc

        # Idempotency guard: snapshot the open position tickets for this symbol
        # before sending. If order_send returns None or raises, the order may
        # still have executed and only the *response* was lost — blindly
        # retrying would open a DUPLICATE position. Before any retry we re-read
        # positions; if a new ticket appeared, we adopt it instead of resending.
        #
        # CRITICAL: only positions stamped with this system's magic number are
        # considered. A manual trade or another bot opening a position on the
        # same symbol inside the ambiguous-send window must NEVER be adopted as
        # our fill (this mirrors the recovery path's magic filter).
        def _is_ours(pd: dict) -> bool:
            try:
                return int(pd.get("magic", -1)) == int(MT5_MAGIC_NUMBER)
            except Exception:
                return False

        def _symbol_positions() -> dict:
            try:
                raw = mt5.positions_get(symbol=normalized_symbol)
                if raw is None:
                    raise RuntimeError("MetaTrader5.positions_get failed")
                out = {}
                for p in raw:
                    pd = p._asdict() if hasattr(p, "_asdict") else dict(p)
                    if not _is_ours(pd):
                        continue
                    ticket = int(pd.get("ticket", 0))
                    if ticket <= 0:
                        raise ValueError("Position has no valid ticket")
                    out[ticket] = pd
                return out
            except Exception as exc:
                raise RuntimeError("Cannot establish broker position snapshot") from exc

        def _detect_new_position():
            try:
                positions = _symbol_positions()
            except RuntimeError as exc:
                raise OrderReconciliationRequired(
                    f"Order outcome unknown for {normalized_symbol}: position read failed; no resend"
                ) from exc
            new = set(positions) - tickets_before
            if not new:
                return None
            if len(new) != 1:
                raise OrderReconciliationRequired(
                    f"Order outcome ambiguous for {normalized_symbol}: multiple new positions; no resend"
                )
            return positions[new.pop()]

        tickets_before = set(_symbol_positions())

        max_retries = 2
        result = None
        recovered_pos = None
        for attempt in range(max_retries + 1):
            if decision.price_type.name == "MARKET":
                # Qualify after the account/position reads, immediately before
                # EVERY send. A missing tick never falls back to the plan.
                request["price"] = checked_market_price()
                logger.info("Using validated live tick price for market order", extra={
                    "symbol": normalized_symbol, "requested_price": float(decision.entry),
                    "live_price": request["price"], "side": decision.side.value, "attempt": attempt + 1,
                })
            try:
                result = mt5.order_send(request)
            except Exception as send_exc:
                logger.warning(f"order_send raised exception (attempt {attempt+1})", extra={
                    "error": str(send_exc), "symbol": normalized_symbol,
                })
                import time as _time
                _time.sleep(0.3)
                recovered_pos = _detect_new_position()
                if recovered_pos is not None:
                    logger.warning(
                        "order_send raised but a new position appeared — adopting it "
                        "instead of retrying (idempotency)", extra={
                            "symbol": normalized_symbol,
                            "ticket": recovered_pos.get("ticket"),
                        })
                    break
                if attempt < max_retries:
                    continue
                raise OrderReconciliationRequired(
                    f"MetaTrader5.order_send outcome unknown after {max_retries+1} attempts: {send_exc}"
                ) from send_exc

            if result is not None:
                break

            # result is None — the order may still have filled. Check before retry.
            import time as _time
            _time.sleep(0.3)
            recovered_pos = _detect_new_position()
            if recovered_pos is not None:
                logger.warning(
                    "order_send returned None but a new position appeared — adopting "
                    "it instead of retrying (idempotency)", extra={
                        "symbol": normalized_symbol,
                        "ticket": recovered_pos.get("ticket"),
                    })
                break

            # result is None — get diagnostics
            error_code = mt5.last_error() if hasattr(mt5, 'last_error') else 'unknown'
            logger.warning(f"order_send returned None (attempt {attempt+1})", extra={
                "symbol": normalized_symbol,
                "last_error": str(error_code),
                "request": {k: v for k, v in request.items() if k != "comment"},
            })
            if attempt < max_retries:
                _time.sleep(0.5)

        # An ambiguous send that actually opened a position: build the Order from
        # the live position rather than resending or raising.
        if recovered_pos is not None:
            ticket = str(recovered_pos.get("ticket") or "")
            # An observed ticket confirms ownership, not missing fill fields.
            # Leave unknown values unconfirmed so the monitor reconciles them.
            avg = float(recovered_pos.get("price_open") or 0.0)
            filled = float(recovered_pos.get("volume") or 0.0)
            order = Order(
                id=ticket,
                decision=decision,
                status="filled",
                filled_quantity=filled,
                average_price=avg,
                client_order_id=decision.client_order_id,
            )
            fill = Fill(
                order_id=ticket,
                quantity=filled,
                price=avg,
                timestamp=datetime.now(timezone.utc),
            )
            return order, [fill]

        try:
            if result is None:
                error_code = mt5.last_error() if hasattr(mt5, 'last_error') else 'unknown'
                raise OrderReconciliationRequired(
                    f"MetaTrader5.order_send failed after {max_retries+1} attempts - "
                    f"no result returned. Last error: {error_code}"
                )
            
            data = result._asdict() if hasattr(result, "_asdict") else dict(result)
            retcode = data.get("retcode", 0)
            
            # Check for MT5 error codes
            if retcode not in (10009, 10010):  # DONE / DONE_PARTIAL
                error_msg = data.get("comment", f"MT5 error code: {retcode}")
                logger.error(f"MT5 order failed: {error_msg}", extra={
                    "symbol": decision.symbol,
                    "side": decision.side.value,
                    "quantity": decision.quantity,
                    "price": decision.entry,
                    "retcode": retcode,
                    "comment": error_msg
                })
                raise RuntimeError(f"MT5 order failed: {error_msg}")
            
            order_id = str(data.get("order") or data.get("ticket") or data.get("deal") or "")
            status = "partially_filled" if retcode == 10010 else str(data.get("comment") or "filled")
            filled = float(data.get("volume") or data.get("volume_filled") or data.get("volume_done") or 0.0)
            avg = float(data.get("price") or data.get("price_done") or 0.0)
            
            logger.info(f"MT5 order executed successfully", extra={
                "symbol": decision.symbol,
                "side": decision.side.value,
                "quantity": decision.quantity,
                "filled_quantity": filled,
                "price": decision.entry,
                "average_price": avg,
                "order_id": order_id,
                "status": status
            })
            
        except Exception as e:
            logger.error(f"MT5 order execution failed: {e}", extra={
                "symbol": decision.symbol,
                "side": decision.side.value,
                "quantity": decision.quantity,
                "price": decision.entry,
                "error": str(e),
                "error_type": type(e).__name__
            })
            raise
        order = Order(
            id=order_id,
            decision=decision,
            status=status,
            filled_quantity=filled,
            average_price=avg,
            client_order_id=decision.client_order_id,
        )
        fill = Fill(
            order_id=order_id,
            quantity=filled,
            price=avg,
            timestamp=datetime.now(timezone.utc),
        )
        return order, [fill]

    def modify(self, order: Order, decision: Decision) -> Order:
        """Amend an existing *order* via ``MetaTrader5.order_send``.

        A ``TRADE_ACTION_MODIFY`` request is issued based on ``decision`` and
        the updated :class:`packages.core.models.Order` is returned.
        """

        if not isinstance(order, Order):
            raise TypeError("order must be Order")
        self._validate_decision(decision)
        self._reconnect_if_needed()
        if self._mt5 is None:
            raise RuntimeError("not logged in")

        from packages.core.services.risk import RiskService

        risk = self._risk or RiskService(self)
        if self._risk is None:
            risk.update_balances(self.balances())
        risk.ensure_margin(decision, raise_error=True)

        mt5 = self._mt5
        normalized = self._normalize_symbol(decision.symbol)
        request = {
            "action": getattr(mt5, "TRADE_ACTION_MODIFY", 2),
            "order": int(order.id),
            "symbol": normalized,
            "price": float(decision.entry),
            "sl": float(decision.sl) if decision.sl is not None else 0.0,
            "tp": float(decision.tp) if decision.tp is not None else 0.0,
            "type_time": getattr(mt5, "ORDER_TIME_GTC", 0),
            "expiration": 0,
        }
        result = mt5.order_send(request)
        if result is None:
            raise RuntimeError("MetaTrader5.order_send failed")
        data = result._asdict() if hasattr(result, "_asdict") else dict(result)
        retcode = data.get("retcode", 0)
        if retcode != 10009:  # TRADE_RETCODE_DONE
            error_msg = data.get("comment", f"MT5 error code: {retcode}")
            logger.error(f"MT5 modify failed: {error_msg}", extra={
                "order_id": order.id, "retcode": retcode, "comment": error_msg,
            })
            raise RuntimeError(f"MT5 modify failed: {error_msg}")
        order_id = str(data.get("order") or order.id)
        status = str(data.get("comment") or data.get("retcode") or order.status)
        filled = float(data.get("volume") or data.get("volume_filled") or 0.0)
        avg = float(data.get("price") or data.get("price_done") or decision.entry)
        return Order(
            id=order_id,
            decision=decision,
            status=status,
            filled_quantity=filled,
            average_price=avg,
            client_order_id=order.client_order_id,
        )

    def cancel(self, order: Order) -> None:
        """Cancel *order* through :func:`MetaTrader5.order_send`.

        Sends a ``TRADE_ACTION_REMOVE`` request for the order ticket.
        """

        if not isinstance(order, Order):
            raise TypeError("order must be Order")
        if self._mt5 is None:
            raise RuntimeError("not logged in")

        mt5 = self._mt5
        request = {
            "action": getattr(mt5, "TRADE_ACTION_REMOVE", 3),
            "order": int(order.id),
        }
        result = mt5.order_send(request)
        if result is None:
            raise RuntimeError("MetaTrader5.order_send failed")

    def order_margin(self, decision: Decision) -> float:
        """Return required margin for *decision*.

        Uses MT5's ``order_check()`` when available for accurate margin.
        Falls back to ``abs(quantity) * entry`` estimate.
        """

        self._validate_decision(decision)
        qty = abs(float(decision.quantity))
        price = float(decision.entry)

        # Try MT5 order_check for accurate margin
        if self._mt5 is not None:
            try:
                mt5 = self._mt5
                action = getattr(mt5, "TRADE_ACTION_DEAL", 1)
                type_val = (
                    getattr(mt5, "ORDER_TYPE_BUY", 0)
                    if decision.side.value == "buy"
                    else getattr(mt5, "ORDER_TYPE_SELL", 1)
                )
                normalized = self._normalize_symbol(decision.symbol)
                request = {
                    "action": action,
                    "symbol": normalized,
                    "volume": qty,
                    "type": type_val,
                    "price": price,
                }
                check = mt5.order_check(request)
                if check is not None:
                    data = check._asdict() if hasattr(check, "_asdict") else dict(check)
                    margin = float(data.get("margin", 0.0))
                    if margin > 0:
                        return margin
            except Exception:
                pass  # fall through to estimate

        return qty * price

    def positions(self) -> List[Position]:
        """Return open positions as :class:`Position` objects."""

        if self._mt5 is None:
            raise RuntimeError("not logged in")
        raw = self._mt5.positions_get()
        if raw is None:
            raise RuntimeError("MetaTrader5.positions_get failed")
        
        # Handle empty positions array (valid case)
        if hasattr(raw, '__len__') and len(raw) == 0:
            return []
            
        out: List[Position] = []
        for item in raw:
            data = item._asdict() if hasattr(item, "_asdict") else dict(item)
            symbol = str(data.get("symbol") or "")
            qty = float(data.get("volume") or data.get("lots") or 0.0)
            avg = float(data.get("price_open") or data.get("price") or 0.0)
            out.append(Position(symbol=symbol, quantity=qty, average_price=avg))
        return out

    def positions_raw(self) -> List[Dict[str, Any]]:
        """Return open positions as raw broker dicts (ticket, symbol, magic, …).

        Unlike :meth:`positions`, this preserves the broker's ``magic`` and
        ``ticket`` fields, so callers (e.g. the ghost-position guardian) can tell
        which positions THIS system opened. Read-only — never mutates state.
        """
        if self._mt5 is None:
            raise RuntimeError("not logged in")
        raw = self._mt5.positions_get()
        if raw is None:
            raise RuntimeError("MetaTrader5.positions_get failed")
        if hasattr(raw, "__len__") and len(raw) == 0:
            return []
        out: List[Dict[str, Any]] = []
        for item in raw:
            data = item._asdict() if hasattr(item, "_asdict") else dict(item)
            out.append(data)
        return out

    def balances(self) -> Dict[str, float]:
        """Return current account balances."""

        if self._mt5 is None:
            raise RuntimeError("not logged in")
        info = self._mt5.account_info()
        if info is None:
            raise RuntimeError("MetaTrader5.account_info failed")
        data = info._asdict() if hasattr(info, "_asdict") else dict(info)
        return {
            "cash": float(data.get("balance") or 0.0),
            "equity": float(data.get("equity") or 0.0),
            "used_margin": float(data.get("margin") or 0.0),
            "available_margin": float(data.get("margin_free") or 0.0),
        }

    def symbol_info(self, symbol: str) -> Dict[str, str]:
        """Return metadata for *symbol* via :func:`MetaTrader5.symbol_info`."""

        if not isinstance(symbol, str):
            raise TypeError("symbol must be str")
        if self._mt5 is None:
            raise RuntimeError("not logged in")
        info = self._mt5.symbol_info(symbol)
        if info is None:
            raise RuntimeError("MetaTrader5.symbol_info failed")
        data = info._asdict() if hasattr(info, "_asdict") else dict(info)
        return {str(k): str(v) for k, v in data.items()}

    # ------------------------------------------------------------------
    # Position management (live trailing SL, close)
    # ------------------------------------------------------------------
    def modify_sl(self, symbol: str, new_sl: float, *, position_ticket=None) -> float:
        """Modify the stop-loss of an open position for *symbol*.

        Issues a ``TRADE_ACTION_SLTP`` request to move the SL while preserving
        the existing TP.  This is used by :class:`TradeMonitor` for trailing
        stop adjustments.

        The requested SL is clamped to the broker's minimum stop distance
        (``trade_stops_level``) from the current price — sending a closer SL
        is rejected by MT5 with retcode 10016 ("Invalid stops").

        Parameters
        ----------
        symbol:
            The market symbol whose position SL should be modified.
        new_sl:
            The new stop-loss price.

        Returns
        -------
        float
            The SL the broker actually holds after the call (the clamped or
            rounded value on success, the unchanged current SL on the skip /
            backward-refusal no-op paths).
        """
        self._reconnect_if_needed()
        if self._mt5 is None:
            raise RuntimeError("not logged in")

        mt5 = self._mt5
        normalized = self._normalize_symbol(symbol)

        # Find the open position for this symbol
        positions = mt5.positions_get(ticket=int(position_ticket)) if position_ticket else mt5.positions_get(symbol=normalized)
        if positions is None or (hasattr(positions, '__len__') and len(positions) == 0):
            raise RuntimeError(f"No open position found for {normalized}")

        pos = positions[0]
        pos_data = pos._asdict() if hasattr(pos, "_asdict") else dict(pos)
        ticket = int(pos_data.get("ticket", 0))
        current_tp = float(pos_data.get("tp", 0.0))
        current_sl = float(pos_data.get("sl", 0.0))
        volume = float(pos_data.get("volume", 0.0))
        pos_type = int(pos_data.get("type", 0))  # 0=BUY, 1=SELL

        # Determine the broker's price precision. The trailing logic can
        # recompute an SL (e.g. ``entry - 0.5R`` right after the BE move) that
        # the broker rounds to the exact value it already holds; sending that
        # fires MT5 retcode 10025 ("No changes"). Rounding ``new_sl`` to the
        # symbol's digits and comparing at tick resolution lets us skip the
        # redundant round-trip instead of issuing a request the broker rejects.
        digits = None
        sym_data = {}
        try:
            sym_info = mt5.symbol_info(normalized)
            if sym_info is not None:
                sym_data = sym_info._asdict() if hasattr(sym_info, "_asdict") else dict(sym_info)
                digits = int(sym_data.get("digits"))
        except Exception:
            digits = None

        if isinstance(digits, int) and 0 <= digits <= 10:
            new_sl = round(float(new_sl), digits)
            tol = 0.5 * (10 ** -digits)  # half a price tick
        else:
            tol = 1e-8

        # Clamp to the broker's minimum stop distance. An SL closer to the
        # current price than trade_stops_level × point is rejected with
        # retcode 10016 ("Invalid stops") — clamping to the closest legal
        # level keeps the trailing ratchet moving instead of failing.
        # Strict isinstance checks: only act on genuine numbers, never on
        # mock/sentinel objects that merely support float().
        def _real_num(value) -> float:
            return float(value) if isinstance(value, (int, float)) else 0.0

        try:
            stops_points = _real_num(sym_data.get("trade_stops_level"))
            point = _real_num(sym_data.get("point"))
        except Exception:
            stops_points = point = 0.0
        if stops_points > 0 and point > 0:
            min_dist = stops_points * point
            bid = ask = 0.0
            try:
                tick = mt5.symbol_info_tick(normalized)
                bid = _real_num(getattr(tick, "bid", None))
                ask = _real_num(getattr(tick, "ask", None))
            except Exception:
                pass
            limit = None
            if pos_type == 0 and bid > 0 and new_sl > bid - min_dist:
                limit = bid - min_dist   # BUY: SL must sit below bid − min_dist
            elif pos_type == 1 and ask > 0 and new_sl < ask + min_dist:
                limit = ask + min_dist   # SELL: SL must sit above ask + min_dist
            if limit is not None:
                if isinstance(digits, int) and 0 <= digits <= 10:
                    limit = round(limit, digits)
                logger.warning(
                    f"modify_sl: clamping SL to broker stops level for {normalized}: "
                    f"{new_sl} -> {limit} (min_dist={min_dist})",
                    extra={
                        "symbol": normalized,
                        "requested_sl": new_sl,
                        "clamped_sl": limit,
                        "stops_level_points": stops_points,
                    },
                )
                new_sl = limit

        # Skip if SL is already at the requested level (avoids MT5 retcode 10025)
        if current_sl > 0 and abs(new_sl - current_sl) < tol:
            logger.debug(
                f"modify_sl: SL already at {current_sl} for {normalized}, skipping"
            )
            return current_sl

        # Safety: Never move SL backward (away from profit)
        if pos_type == 0:  # BUY position — SL must only move UP
            if new_sl < current_sl and current_sl > 0:
                logger.warning(
                    f"modify_sl: Refusing to move SL backward for BUY {normalized}. "
                    f"Current SL={current_sl}, Requested={new_sl}"
                )
                return current_sl
        else:  # SELL position — SL must only move DOWN
            if new_sl > current_sl and current_sl > 0:
                logger.warning(
                    f"modify_sl: Refusing to move SL backward for SELL {normalized}. "
                    f"Current SL={current_sl}, Requested={new_sl}"
                )
                return current_sl

        request = {
            "action": getattr(mt5, "TRADE_ACTION_SLTP", 6),
            "position": ticket,
            "symbol": normalized,
            "sl": float(new_sl),
            "tp": current_tp,
        }

        result = mt5.order_send(request)
        if result is None:
            raise RuntimeError(f"modify_sl: order_send returned None for {normalized}")

        data = result._asdict() if hasattr(result, "_asdict") else dict(result)
        retcode = data.get("retcode", 0)

        # TRADE_RETCODE_NO_CHANGES (10025): the broker already holds this exact
        # SL (a sub-tick recompute that rounded to the current value slipped past
        # the local skip-guard above). The position is protected at the requested
        # level, so treat it as a benign no-op rather than raising an error.
        if retcode == 10025:
            logger.debug(
                f"modify_sl: broker reports no change for {normalized} "
                f"(SL already at requested {new_sl})"
            )
            return new_sl

        if retcode != 10009:  # TRADE_RETCODE_DONE
            error_msg = data.get("comment", f"MT5 error code: {retcode}")
            logger.error(
                f"modify_sl failed for {normalized}",
                extra={
                    "symbol": normalized,
                    "ticket": ticket,
                    "new_sl": new_sl,
                    "retcode": retcode,
                    "comment": error_msg,
                },
            )
            raise RuntimeError(f"modify_sl failed: {error_msg}")

        logger.info(
            f"SL modified for {normalized}",
            extra={
                "symbol": normalized,
                "ticket": ticket,
                "old_sl": current_sl,
                "new_sl": new_sl,
                "tp": current_tp,
            },
        )
        return new_sl

    def move_sl_to_be(self, symbol: str) -> None:
        """Move stop-loss to break-even+0.5R for *symbol*.

        Reads the position's open price, computes BE+0.5R from the current SL,
        and uses it as the new SL.
        """
        if self._mt5 is None:
            raise RuntimeError("not logged in")

        mt5 = self._mt5
        normalized = self._normalize_symbol(symbol)

        positions = mt5.positions_get(symbol=normalized)
        if positions is None or (hasattr(positions, '__len__') and len(positions) == 0):
            raise RuntimeError(f"No open position found for {normalized}")

        pos = positions[0]
        pos_data = pos._asdict() if hasattr(pos, "_asdict") else dict(pos)
        entry_price = float(pos_data.get("price_open", 0.0))
        current_sl = float(pos_data.get("sl", 0.0))

        if entry_price <= 0:
            raise RuntimeError(f"Invalid entry price {entry_price} for {normalized}")

        risk = abs(entry_price - current_sl) if current_sl > 0 else 0
        # Determine direction from position type (0=buy, 1=sell)
        pos_type = int(pos_data.get("type", 0))
        if pos_type == 0:  # buy/long
            be_price = entry_price + 0.5 * risk
        else:  # sell/short
            be_price = entry_price - 0.5 * risk

        logger.info(
            f"Moving SL to BE+0.5R for {normalized} at {be_price} (entry={entry_price}, risk={risk})"
        )
        self.modify_sl(symbol, be_price)

    def partial_close(self, symbol: str, fraction: float = 0.5, *, position_ticket=None) -> "float | bool":
        """Close a fraction of an open position for *symbol* at market price.

        Sends a counter-directional market order for ``fraction`` of the position
        volume (default 50%), rounded DOWN to the symbol's volume step and clamped
        up to its minimum lot.

        Returns
        -------
        float
            The volume actually closed. This is almost never ``volume * fraction``:
            a 0.07-lot position asked to close 50% sends 0.03, not 0.035. Callers
            MUST book the P&L leg and decrement their remaining size with THIS
            number — assuming the request is what corrupted the live trade record
            before 2026-08-27 (audit C3).
        False
            The broker refused: no position, or the volume cannot be split
            (at min-lot, or the split would close the whole position).

        The ``float | False`` return keeps the old truthiness contract — every
        caller tests ``closed is not False`` — so adding the real number cannot
        change any control flow.
        """
        self._reconnect_if_needed()
        if self._mt5 is None:
            raise RuntimeError("not logged in")
        if not 0.0 < fraction < 1.0:
            raise ValueError(f"fraction must be between 0 and 1, got {fraction}")

        mt5 = self._mt5
        normalized = self._normalize_symbol(symbol)

        positions = mt5.positions_get(ticket=int(position_ticket)) if position_ticket else mt5.positions_get(symbol=normalized)
        if positions is None:
            raise RuntimeError("partial_close: position snapshot unavailable")
        if hasattr(positions, '__len__') and len(positions) == 0:
            logger.warning(f"partial_close: No open position for {normalized}")
            return False

        pos = positions[0]
        pos_data = pos._asdict() if hasattr(pos, "_asdict") else dict(pos)
        ticket = int(pos_data.get("ticket", 0))
        volume = float(pos_data.get("volume", 0.0))
        pos_type = int(pos_data.get("type", 0))  # 0=BUY, 1=SELL

        # Calculate partial volume respecting symbol's volume step
        close_volume = volume * fraction
        try:
            sym_info = mt5.symbol_info(normalized)
            if sym_info is not None:
                sym_data = sym_info._asdict() if hasattr(sym_info, "_asdict") else dict(sym_info)
                vol_step = float(sym_data.get("volume_step", 0.01))
                vol_min = float(sym_data.get("volume_min", 0.01))
                if vol_step > 0:
                    import math
                    close_volume = math.floor(close_volume / vol_step) * vol_step
                close_volume = max(close_volume, vol_min)
                # Match backtest_account._splittable_volume: 0.02*0.5 must not
                # land at 0.010000000000000002 and read as >= the position.
                close_volume = round(close_volume, 8)
        except Exception:
            close_volume = round(close_volume, 2)

        if close_volume <= 0 or close_volume >= volume:
            logger.info(
                f"partial_close: position volume {volume} at minimum lot — "
                f"skipping partial close for {normalized}"
            )
            return False

        # Opposite order type for closing
        close_type = (
            getattr(mt5, "ORDER_TYPE_SELL", 1)
            if pos_type == 0
            else getattr(mt5, "ORDER_TYPE_BUY", 0)
        )

        # Get current price
        tick = mt5.symbol_info_tick(normalized)
        if tick is None:
            raise RuntimeError(f"Cannot get tick for {normalized} to partial close")
        tick_data = tick._asdict() if hasattr(tick, "_asdict") else dict(tick)
        close_price = (
            float(tick_data.get("bid", 0.0))
            if pos_type == 0
            else float(tick_data.get("ask", 0.0))
        )

        request = {
            "action": getattr(mt5, "TRADE_ACTION_DEAL", 1),
            "position": ticket,
            "symbol": normalized,
            "volume": close_volume,
            "type": close_type,
            "price": close_price,
            "deviation": 20,
            "magic": MT5_MAGIC_NUMBER,
            "type_time": getattr(mt5, "ORDER_TIME_GTC", 0),
            "type_filling": getattr(mt5, "ORDER_FILLING_IOC", 0),
        }

        try:
            result = mt5.order_send(request)
        except Exception as exc:
            raise OrderReconciliationRequired(f"partial_close: uncertain send for {normalized}") from exc
        if result is None:
            raise OrderReconciliationRequired(f"partial_close: order_send returned None for {normalized}")

        data = result._asdict() if hasattr(result, "_asdict") else dict(result)
        retcode = data.get("retcode", 0)

        if retcode not in (10009, 10010):  # DONE / DONE_PARTIAL
            error_msg = data.get("comment", f"MT5 error code: {retcode}")
            logger.error(
                f"partial_close failed for {normalized}",
                extra={
                    "symbol": normalized,
                    "ticket": ticket,
                    "close_volume": close_volume,
                    "total_volume": volume,
                    "retcode": retcode,
                    "comment": error_msg,
                },
            )
            raise RuntimeError(f"partial_close failed: {error_msg}")

        logger.info(
            f"Partial close {fraction*100:.0f}% for {normalized}",
            extra={
                "symbol": normalized,
                "ticket": ticket,
                "closed_volume": close_volume,
                "remaining_volume": volume - close_volume,
                "close_price": close_price,
                "pos_type": "BUY" if pos_type == 0 else "SELL",
                "requested_volume": volume * fraction,
            },
        )
        # The FILLED volume, not the requested one — see the docstring.
        filled_volume = float(data.get("volume") or 0.)
        if not math.isfinite(filled_volume) or not 0 < filled_volume <= close_volume + 1e-8:
            raise OrderReconciliationRequired("partial_close: broker receipt has no confirmed filled volume")
        receipts = getattr(self, "_partial_receipts", {})
        receipts[str(ticket)] = {"deal_id": data.get("deal"), "order_id": data.get("order"),
                                 "price": data.get("price"), "volume": filled_volume,
                                 "received_at": datetime.now(timezone.utc).isoformat()}
        self._partial_receipts = receipts
        return filled_volume

    def close(self, symbol: str) -> None:
        """Close an open position for *symbol* at market price.

        Sends a counter-directional market order for the full position volume.
        """
        self._reconnect_if_needed()
        if self._mt5 is None:
            raise RuntimeError("not logged in")

        mt5 = self._mt5
        normalized = self._normalize_symbol(symbol)

        positions = mt5.positions_get(symbol=normalized)
        if positions is None or (hasattr(positions, '__len__') and len(positions) == 0):
            logger.warning(f"close: No open position for {normalized} — nothing to close")
            return

        pos = positions[0]
        pos_data = pos._asdict() if hasattr(pos, "_asdict") else dict(pos)
        ticket = int(pos_data.get("ticket", 0))
        volume = float(pos_data.get("volume", 0.0))
        pos_type = int(pos_data.get("type", 0))  # 0=BUY, 1=SELL

        # Close by sending opposite order
        close_type = getattr(mt5, "ORDER_TYPE_SELL", 1) if pos_type == 0 else getattr(mt5, "ORDER_TYPE_BUY", 0)

        # Get current price for the close
        tick = mt5.symbol_info_tick(normalized)
        if tick is None:
            raise RuntimeError(f"Cannot get tick for {normalized} to close position")
        tick_data = tick._asdict() if hasattr(tick, "_asdict") else dict(tick)
        # For closing BUY we sell at bid; for closing SELL we buy at ask
        close_price = float(tick_data.get("bid", 0.0)) if pos_type == 0 else float(tick_data.get("ask", 0.0))

        request = {
            "action": getattr(mt5, "TRADE_ACTION_DEAL", 1),
            "position": ticket,
            "symbol": normalized,
            "volume": volume,
            "type": close_type,
            "price": close_price,
            "deviation": 20,
            "magic": MT5_MAGIC_NUMBER,
            "type_time": getattr(mt5, "ORDER_TIME_GTC", 0),
            "type_filling": getattr(mt5, "ORDER_FILLING_IOC", 0),
        }

        result = mt5.order_send(request)
        if result is None:
            raise RuntimeError(f"close: order_send returned None for {normalized}")

        data = result._asdict() if hasattr(result, "_asdict") else dict(result)
        retcode = data.get("retcode", 0)

        if retcode != 10009:  # TRADE_RETCODE_DONE
            error_msg = data.get("comment", f"MT5 error code: {retcode}")
            logger.error(
                f"close failed for {normalized}",
                extra={
                    "symbol": normalized,
                    "ticket": ticket,
                    "volume": volume,
                    "retcode": retcode,
                    "comment": error_msg,
                },
            )
            raise RuntimeError(f"close failed: {error_msg}")

        logger.info(
            f"Position closed for {normalized}",
            extra={
                "symbol": normalized,
                "ticket": ticket,
                "volume": volume,
                "close_price": close_price,
                "pos_type": "BUY" if pos_type == 0 else "SELL",
            },
        )

    def close_position_by_ticket(self, ticket: int) -> bool:
        """Force-close the position identified by *ticket* at market price.

        Targets the EXACT position (not "first position for the symbol"), so it
        is safe when an app position and a foreign/ghost position share a symbol.
        Used by the ghost-position guardian. Returns True when the close is
        confirmed (or the position is already gone).
        """
        self._reconnect_if_needed()
        if self._mt5 is None:
            raise RuntimeError("not logged in")
        mt5 = self._mt5

        found = mt5.positions_get(ticket=int(ticket))
        if found is None:
            raise RuntimeError("close_by_ticket: position snapshot unavailable")
        if hasattr(found, "__len__") and len(found) == 0:
            logger.warning(f"close_by_ticket: position {ticket} not found — already closed")
            return True  # goal (no such open position) already achieved

        pos_data = found[0]._asdict() if hasattr(found[0], "_asdict") else dict(found[0])
        symbol = str(pos_data.get("symbol") or "")
        volume = float(pos_data.get("volume", 0.0))
        pos_type = int(pos_data.get("type", 0))  # 0=BUY, 1=SELL

        # Counter-directional market order closes the position.
        close_type = (
            getattr(mt5, "ORDER_TYPE_SELL", 1) if pos_type == 0
            else getattr(mt5, "ORDER_TYPE_BUY", 0)
        )

        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise RuntimeError(f"Cannot get tick for {symbol} to close ticket {ticket}")
        tick_data = tick._asdict() if hasattr(tick, "_asdict") else dict(tick)
        # Closing a BUY sells at bid; closing a SELL buys at ask.
        close_price = float(tick_data.get("bid", 0.0)) if pos_type == 0 else float(tick_data.get("ask", 0.0))

        request = {
            "action": getattr(mt5, "TRADE_ACTION_DEAL", 1),
            "position": int(ticket),
            "symbol": symbol,
            "volume": volume,
            "type": close_type,
            "price": close_price,
            # Wider deviation than a normal close: killing a ghost prioritises
            # certainty of exit over price improvement.
            "deviation": 50,
            "magic": MT5_MAGIC_NUMBER,
            "type_time": getattr(mt5, "ORDER_TIME_GTC", 0),
            "type_filling": getattr(mt5, "ORDER_FILLING_IOC", 0),
        }

        result = mt5.order_send(request)
        if result is None:
            raise RuntimeError(f"close_by_ticket: order_send returned None for ticket {ticket}")

        data = result._asdict() if hasattr(result, "_asdict") else dict(result)
        retcode = data.get("retcode", 0)
        if retcode != 10009:  # TRADE_RETCODE_DONE
            error_msg = data.get("comment", f"MT5 error code: {retcode}")
            logger.error(
                f"close_by_ticket failed for ticket {ticket}",
                extra={"ticket": ticket, "symbol": symbol, "retcode": retcode, "comment": error_msg},
            )
            raise RuntimeError(f"close_by_ticket failed: {error_msg}")

        logger.info(
            f"Position {ticket} closed by ticket",
            extra={
                "ticket": ticket,
                "symbol": symbol,
                "volume": volume,
                "close_price": close_price,
                "pos_type": "BUY" if pos_type == 0 else "SELL",
            },
        )
        return True

    # ------------------------------------------------------------------
    # Deal history — used to recover actual exit price/reason when
    # MT5's native SL/TP closes a position before our monitor detects it.
    # ------------------------------------------------------------------
    # MQL5 ENUM_DEAL_REASON (verified against live Exness deals — the exits
    # previously logged as "MT5_REASON_4"/"MT5_REASON_5" were SL/TP hits):
    #   0 CLIENT, 1 MOBILE, 2 WEB, 3 EXPERT, 4 SL, 5 TP, 6 SO (stop-out)
    _DEAL_REASON_LABELS = {4: "SL", 5: "TP", 6: "SO", 3: "EXPERT"}

    def account_pnl_report(self, day_start: datetime) -> dict:
        """Booked MT5 trading P&L by posting time, plus current account floating P&L."""
        mt5 = self._mt5
        account = mt5.account_info() if mt5 is not None else None
        if (account is None or (account.server.casefold(), account.login, account.trade_mode)
                != self._report_account):
            raise RuntimeError("Selected MT5 account unavailable")
        now = datetime.now(timezone.utc)
        deals = mt5.history_deals_get(datetime(1970, 1, 1, tzinfo=timezone.utc), now)
        after = mt5.account_info()
        if deals is None or after is None or (after.login, after.server) != (account.login, account.server):
            raise RuntimeError("MT5 account history unavailable")

        # Standalone broker charges carry their cash amount in profit. Balance
        # transfers, credit and bonuses are funding, outside trading P&L.
        trading = {mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL, mt5.DEAL_DIVIDEND, mt5.DEAL_DIVIDEND_FRANKED}
        charges = {getattr(mt5, name) for name in (
            "DEAL_TYPE_CHARGE", "DEAL_TYPE_COMMISSION", "DEAL_TYPE_COMMISSION_DAILY",
            "DEAL_TYPE_COMMISSION_MONTHLY", "DEAL_TYPE_COMMISSION_AGENT_DAILY",
            "DEAL_TYPE_COMMISSION_AGENT_MONTHLY", "DEAL_TYPE_INTEREST", "DEAL_TAX",
        ) if hasattr(mt5, name)}
        gross = costs = today_gross = today_costs = Decimal(0)
        curve = []
        seen = set()
        included = trading | charges
        for deal in sorted(deals, key=lambda d: (d.time_msc, d.ticket)):
            if deal.ticket in seen or deal.type not in included:
                continue
            seen.add(deal.ticket)
            profit = Decimal(str(deal.profit))
            cost = sum(Decimal(str(v)) for v in (deal.commission, deal.swap, deal.fee))
            if deal.type in charges:
                cost += profit
                profit = Decimal(0)
            gross += profit
            costs += cost
            stamp = datetime.fromtimestamp(deal.time_msc / 1000, timezone.utc)
            if stamp >= day_start:
                today_gross += profit
                today_costs += cost
                curve.append({"time": stamp, "pnl": float(today_gross + today_costs)})
        return {
            "currency": after.currency, "floating": float(after.profit),
            "all_time": {"gross": gross, "charges": costs, "net": gross + costs},
            "today": {"gross": today_gross, "charges": today_costs, "net": today_gross + today_costs},
            "daily_curve": curve,
        }

    def operational_snapshot(self, now=None):
        """Complete broker deal identities for operational controls, read-only."""
        if self._mt5 is None:
            raise RuntimeError("Broker disconnected")
        now = now or datetime.now(timezone.utc)
        account = self._mt5.account_info()
        if account is None:
            raise RuntimeError("Account identity unavailable")
        credentials = getattr(self, "_credentials", {}) or {}
        if credentials.get("login") and int(credentials["login"]) != int(account.login):
            raise RuntimeError("Terminal account differs from configured account")
        key = f"mt5:{account.server}:{account.login}"
        raw = self._mt5.history_deals_get(now - timedelta(days=2), now)
        positions = self._mt5.positions_get()
        if raw is None or positions is None:
            raise RuntimeError("Operational broker history/positions unavailable")
        rows = [d._asdict() if hasattr(d, "_asdict") else dict(d) for d in raw]
        pids = {int(d.get("position_id") or 0) for d in rows}
        pids.update(int(getattr(p, "identifier", 0) or getattr(p, "ticket", 0)) for p in positions)
        for pid in pids - {0}:
            # Open-position history is mutable. After a long connection outage,
            # the two-day window can omit intermediate partials; a cached opening
            # snapshot would then report the wrong remaining quantity and P&L.
            full = self._mt5.history_deals_get(position=pid)
            if not full:
                raise RuntimeError("Complete position history unavailable")
            rows.extend(d._asdict() if hasattr(d, "_asdict") else dict(d) for d in full)
        after = self._mt5.account_info()
        if after is None or (after.server, after.login) != (account.server, account.login):
            raise RuntimeError("Account changed during operational synchronization")
        self._operational_position_aliases = {
            int(getattr(p, "ticket", 0)): int(getattr(p, "identifier", 0) or getattr(p, "ticket", 0))
            for p in positions if getattr(p, "ticket", 0)
        }
        self._operational_account_currency = getattr(account, "currency", None)
        return key, rows

    def executable_quote(self, symbol, side):
        tick = self._mt5.symbol_info_tick(self._normalize_symbol(symbol)) if self._mt5 else None
        if tick is None:
            raise RuntimeError("Quote unavailable")
        stamp = float(getattr(tick, "time_msc", 0) or tick.time * 1000) / 1000
        price = float(tick.bid if side in ("long", "buy") else tick.ask)
        age = datetime.now(timezone.utc).timestamp() - stamp
        if not math.isfinite(price) or price <= 0 or not -2 <= age <= 10:
            raise RuntimeError("Quote stale or invalid")
        return {"price": price, "time": datetime.fromtimestamp(stamp, timezone.utc)}

    def get_last_exit_deal(
        self, symbol: str, position_ticket: str | int | None = None,
        lookback_hours: float = 24.0,
        magic: int | None = None,
        not_before: datetime | None = None,
    ) -> Dict[str, Any] | None:
        """Return the most recent closing deal for *symbol*, or None.

        Queries the last ``lookback_hours`` (default 24) of deal history for an
        ``OUT`` deal (position close) on this symbol.  When *position_ticket*
        is given, only deals belonging to that exact position are considered —
        this is what lets exit records match the right trade doc.  The monitor
        crash-recovery passes a longer lookback (a position orphaned before a
        weekend closes >24h before the Monday restart).  Returns a dict
        with ``price``, ``volume``, ``reason``, ``time``, ``profit``,
        ``magic`` and ``position_id`` keys.

        A *position_ticket* that is not a valid integer is treated as **absent**
        rather than raising (2026-07-27: a legacy ``monitor_state`` row carried
        the composite key ``"XAUUSD:4517.5"``, and the ``int()`` blew up inside
        the caller's try/except — silently reverting LP-13 to its pre-fix
        behaviour). *magic* and *not_before* narrow a ticket-less lookup so it
        cannot attribute somebody else's close (a manual mobile trade, or an
        older app position on the same symbol) to the row being recovered.
        """
        if self._mt5 is None:
            return None

        mt5 = self._mt5
        normalized = self._normalize_symbol(symbol)

        now = datetime.now(timezone.utc)
        from_time = now - timedelta(hours=float(lookback_hours))

        if position_ticket and str(position_ticket).isdigit() and (getattr(self, "_credentials", {}) or {}).get("login"):
            from packages.core.operational_ledger import sync_broker, utc
            account, ledger = sync_broker(self, now)
            try:
                pid = ledger.resolve_position_id(account, position_ticket)
                full = mt5.history_deals_get(position=int(pid))
                if full is None:
                    raise RuntimeError("Whole-position exit history unavailable")
                ledger.ingest(account, [d._asdict() if hasattr(d, "_asdict") else dict(d) for d in full], magic=MT5_MAGIC_NUMBER)
                p = ledger.position(account, pid)
                if not p or not p["closed"] or p["symbol"] != normalized:
                    return None
                if magic is not None and int(magic) != MT5_MAGIC_NUMBER:
                    return None
                if not_before is not None and utc(p["closed"]) < utc(not_before):
                    return None
                reason = int(p["exit_reason"])
                return {"price": p["exit_price"], "volume": p["last_exit_volume"],
                        "reason": self._DEAL_REASON_LABELS.get(reason, f"REASON_{reason}"),
                        "profit": p["pnl"], "whole_position_net": p["pnl"],
                        "time": utc(p["closed"]), "position_id": int(pid), "magic": MT5_MAGIC_NUMBER}
            finally:
                ledger.close()

        try:
            if position_ticket and str(position_ticket).isdigit():
                deals = mt5.history_deals_get(position=int(position_ticket))
            else:
                deals = mt5.history_deals_get(from_time, now, group=normalized)
        except Exception as e:
            logger.warning(f"history_deals_get failed for {normalized}: {e}")
            return None

        if deals is None or (hasattr(deals, '__len__') and len(deals) == 0):
            return None

        DEAL_ENTRY_OUT = 1
        want_position: int | None = None
        if position_ticket:
            try:
                want_position = int(str(position_ticket).strip())
            except (TypeError, ValueError):
                # Legacy/composite key (e.g. "XAUUSD:4517.5"): fall back to the
                # symbol+window lookup instead of raising into the caller.
                logger.warning(
                    f"get_last_exit_deal: non-numeric position_ticket "
                    f"{position_ticket!r} for {normalized} — matching by "
                    "symbol + time window instead"
                )
                want_position = None

        not_before_ts = None
        if not_before is not None:
            nb = not_before if not_before.tzinfo else not_before.replace(tzinfo=timezone.utc)
            not_before_ts = nb.timestamp()

        for deal in reversed(deals):
            d = deal._asdict() if hasattr(deal, "_asdict") else dict(deal)
            if int(d.get("entry", -1)) not in (DEAL_ENTRY_OUT, 3):
                continue
            if want_position is not None and int(d.get("position_id", 0) or 0) != want_position:
                continue
            if magic is not None:
                try:
                    if int(d.get("magic", 0) or 0) != int(magic):
                        continue
                except (TypeError, ValueError):
                    continue
            if not_before_ts is not None:
                dt_raw = d.get("time")
                dt_ts = (
                    float(dt_raw) if isinstance(dt_raw, (int, float))
                    else dt_raw.timestamp() if isinstance(dt_raw, datetime) else None
                )
                if dt_ts is not None and dt_ts < not_before_ts:
                    continue

            reason_code = int(d.get("reason", 0))
            reason_str = self._DEAL_REASON_LABELS.get(reason_code, f"REASON_{reason_code}")

            deal_time = d.get("time")
            if isinstance(deal_time, (int, float)):
                deal_time = datetime.fromtimestamp(int(deal_time), tz=timezone.utc)

            if (getattr(self, "_credentials", {}) or {}).get("login"):
                pid = d.get("position_id")
                return self.get_last_exit_deal(symbol, pid, lookback_hours, magic, not_before) if pid else None
            return {
                "price": float(d.get("price", 0.0)),
                "volume": float(d.get("volume", 0.0)),
                "reason": reason_str,
                "profit": float(d.get("profit", 0.0)),
                "whole_position_net": None,
                "time": deal_time,
                "position_id": int(d.get("position_id", 0) or 0) or None,
                "magic": int(d.get("magic", 0) or 0),
            }

        return None

    # ------------------------------------------------------------------
    # Async wrappers — prevent blocking the asyncio event loop
    # ------------------------------------------------------------------
    # Every sync MT5 method uses IPC to the terminal (50-200ms per call).
    # Calling them from async code blocks the event loop.
    # These wrappers dispatch to the thread pool so other coroutines
    # (trade monitoring, WebSocket pushes, health checks) keep running.

    async def aplace(self, decision: Decision):
        """Non-blocking :meth:`place`. Returns ``(Order, [Fill])``."""
        return await asyncio.to_thread(self.place, decision)

    async def apositions(self) -> List[Position]:
        """Non-blocking :meth:`positions`."""
        return await asyncio.to_thread(self.positions)

    async def abalances(self) -> Dict[str, float]:
        """Non-blocking :meth:`balances`."""
        return await asyncio.to_thread(self.balances)

    async def aclose(self, symbol: str) -> None:
        """Non-blocking :meth:`close`."""
        await asyncio.to_thread(self.close, symbol)

    async def amodify_sl(self, symbol: str, new_sl: float) -> None:
        """Non-blocking :meth:`modify_sl`."""
        await asyncio.to_thread(self.modify_sl, symbol, new_sl)

    async def amove_sl_to_be(self, symbol: str) -> None:
        """Non-blocking :meth:`move_sl_to_be`."""
        await asyncio.to_thread(self.move_sl_to_be, symbol)

    async def apartial_close(self, symbol: str, fraction: float = 0.5) -> "float | bool":
        """Non-blocking :meth:`partial_close`. Returns the FILLED volume, or False."""
        return await asyncio.to_thread(self.partial_close, symbol, fraction)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_decision(decision: Decision) -> None:
        if not isinstance(decision, Decision):
            raise TypeError("decision must be Decision")


async def wait_for_bar(adapter: MT5Adapter, symbol: str, interval: str) -> Bar:
    """Block until a new closed bar is available and return it.

    Uses adaptive polling: polls every 200ms near expected bar close,
    500ms when close to it, 1s otherwise.  This reduces average detection
    latency from ~500ms to ~100ms while keeping CPU usage low between bars.

    Raises :class:`RuntimeError` if the market is closed for this symbol
    so the caller (TradeMonitor) can sleep through the close window
    instead of burning retries.

    Parameters
    ----------
    adapter:
        Connected :class:`MT5Adapter` instance.
    symbol:
        Market symbol to poll.
    interval:
        Timeframe string understood by :meth:`get_recent_bars` (e.g. ``"M5"``).
    """
    from packages.core.services.code_sniper_monitor import _is_market_closed

    last_ts: datetime | None = None
    _interval_secs = {"M5": 300, "M15": 900, "M1": 60}.get(interval, 300)
    _stale_polls = 0  # consecutive polls with no new bar
    while True:
        # ---- market-close guard ----
        closed, wait_sec = _is_market_closed(symbol)
        if closed:
            raise RuntimeError(
                f"{symbol} market closed — reopen in ~{wait_sec}s"
            )

        bars = await adapter._get_recent_bars_single(symbol, interval, 2)
        if len(bars) < 2:
            await asyncio.sleep(0.5)
            continue
        bars = sorted(bars, key=lambda b: b["time"])
        closed = bars[-2]
        if last_ts is None:
            last_ts = closed["time"]
            # Don't sleep here — immediately check if a NEW bar is already
            # available on the first poll (avoids missing the first candle)
            continue
        elif closed["time"] > last_ts:
            return closed
        # Adaptive polling: faster near expected bar close
        elapsed = (datetime.now(timezone.utc) - closed["time"]).total_seconds()
        remaining = max(0, _interval_secs - elapsed)
        if remaining < 10:
            await asyncio.sleep(0.2)   # 200ms — bar about to close
        elif remaining < 30:
            await asyncio.sleep(0.5)   # 500ms — close is near
        else:
            await asyncio.sleep(1.0)   # 1s — far from close


__all__ = ["MT5Adapter", "wait_for_bar"]
