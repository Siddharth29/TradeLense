from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Awaitable, Callable, Dict, Mapping

from packages.core import scheduler, session
from packages.core.metrics import record_drift
from packages.core.config import (
    SUPERVISOR_MAX_RESTART_ATTEMPTS,
    SUPERVISOR_RESTART_COOLDOWN_MINUTES,
    SUPERVISOR_HEALTH_CHECK_INTERVAL_MINUTES,
    SUPERVISOR_MAX_CONSECUTIVE_FAILURES,
)

logger = logging.getLogger(__name__)


class Supervisor:
    """Coordinate agent runners per asset symbol with automatic restart capabilities."""

    def __init__(
        self,
        runner: Callable[[str, int, Mapping[str, Any]], Awaitable[Any]],
    ) -> None:
        self._runner = runner
        self._tasks: Dict[str, asyncio.Task] = {}
        self._active_cycles: Dict[str, asyncio.Task] = {}
        self._stopping: set[str] = set()
        self._cfg: Dict[str, Mapping[str, Any]] = {}
        self._errors: Dict[str, BaseException] = {}
        self._restart_counts: Dict[str, int] = {}
        self._last_restart: Dict[str, datetime] = {}
        self._health_monitor_task: asyncio.Task | None = None
        self._max_restart_attempts = SUPERVISOR_MAX_RESTART_ATTEMPTS
        self._restart_cooldown_minutes = SUPERVISOR_RESTART_COOLDOWN_MINUTES
        self._health_check_interval_minutes = SUPERVISOR_HEALTH_CHECK_INTERVAL_MINUTES

    @staticmethod
    def _normalize_interval(value: str | None) -> str:
        """Return scheduler-compatible interval from ``value``."""

        if not value:
            logger.info("No interval specified, defaulting to 1h")
            return "1h"
        iv = value.strip().lower()
        if iv == "hourly":
            logger.info(f"Normalized interval '{value}' to '1h'")
            return "1h"
        if iv == "30min" or iv == "30m":
            logger.info(f"Normalized interval '{value}' to '30m'")
            return "30m"
        if iv.endswith("m") or iv.endswith("h"):
            logger.info(f"Using interval '{value}' as-is")
            return iv
        logger.error(f"Unsupported run interval: {value}")
        raise ValueError(f"unsupported run interval: {value}")

    # public API -----------------------------------------------------------
    def start(self, asset: str, cfg: Mapping[str, Any]) -> None:
        """Launch a background task for ``asset``.

        The supplied ``cfg`` is stored for later runs.
        """

        if asset in self._tasks or asset in self._stopping:
            raise RuntimeError(f"{asset} already started")

        cfg = dict(cfg)
        original_interval = cfg.get("run_interval")
        cfg["run_interval"] = self._normalize_interval(original_interval)
        self._cfg[asset] = cfg
        is_mt5 = bool(cfg.get("broker") == "mt5" or cfg.get("mt5_service"))
        interval = cfg["run_interval"]
        
        logger.info(
            f"Starting agent for asset={asset} with interval={interval} "
            f"(original_interval={original_interval}, normalized_interval={interval}, "
            f"broker_type={'MT5' if is_mt5 else 'NSE'})"
        )

        async def _maybe_run(planned: datetime) -> None:
            # The monotonic scheduler can finish just before the wall-clock
            # boundary. Keep this slot pending instead of letting should_run_now
            # reject it and silently advancing to the next hourly slot.
            while (remaining := (planned - datetime.now(timezone.utc)).total_seconds()) > 0:
                await asyncio.sleep(remaining)
            now_utc = datetime.now(timezone.utc)
            now_local = now_utc.astimezone(session.IST)
            
            # Extract the actual symbol from config for session validation
            # The 'asset' parameter is the agent_id, but we need the trading symbol
            symbol = cfg.get("symbol")
            if not symbol:
                # Fallback: try to get symbol from instrument config
                instrument = cfg.get("instrument", {})
                if isinstance(instrument, dict):
                    symbol = instrument.get("symbol")
                if not symbol:
                    symbol = asset  # Last resort fallback
            
            # Use the actual symbol for session validation (e.g., "XAUUSD", "BTCUSD")
            if not session.is_session_open(now_local, symbol):
                logger.info(f"Session closed for {symbol} (agent: {asset}) - skipping run")
                return
            
            drift = (now_utc - planned.astimezone(timezone.utc)).total_seconds() * 1000.0
            record_drift(asset, drift)
            
            # Set execution mode to 'scheduled' for supervisor runs
            cfg_copy = dict(cfg)
            cfg_copy["execution_mode"] = "scheduled"
            cfg_copy["scheduled_time"] = planned
            
            await self._run_cycle(asset, cfg_copy)

        async def _wrapped() -> None:
            """Main agent loop with robust error handling and automatic restart."""
            consecutive_failures = 0
            max_consecutive_failures = SUPERVISOR_MAX_CONSECUTIVE_FAILURES

            logger.info(
                f"Agent {asset} background task started "
                f"(interval={interval}, broker={'MT5' if is_mt5 else 'NSE'}) — "
                f"waiting for next {interval} bar close before first run"
            )

            while True:
                try:
                    logger.info(
                        f"Agent {asset} waiting for next {interval} bar close "
                        f"(consecutive_failures={consecutive_failures})"
                    )
                    planned = await scheduler.await_next_bar_close(
                        interval, "Asia/Kolkata"
                    )
                    logger.info(f"Agent {asset} woke for bar close at {planned.isoformat()}")
                    await _maybe_run(planned)
                    # Reset failure counter on successful run
                    consecutive_failures = 0
                    
                except asyncio.CancelledError:
                    logger.info(f"Agent {asset} cancelled")
                    raise
                except BaseException as exc:
                    # Catch BaseException to detect unexpected exits
                    if isinstance(exc, Exception):
                        consecutive_failures += 1
                        logger.error(
                            f"Agent {asset} failed (attempt {consecutive_failures}/{max_consecutive_failures}): {exc}",
                            exc_info=True
                        )
                        self._errors[asset] = exc
                        if consecutive_failures >= max_consecutive_failures:
                            logger.error(f"Agent {asset} exceeded max consecutive failures, stopping")
                            break
                        await asyncio.sleep(min(consecutive_failures * 30, 300))
                    else:
                        logger.error(f"Agent {asset} hit unexpected BaseException: {type(exc).__name__}: {exc}")
                        raise

        self._tasks[asset] = asyncio.create_task(_wrapped(), name=f"agent:{asset}")
        
        # Start health monitoring if not already running
        if self._health_monitor_task is None or self._health_monitor_task.done():
            self._health_monitor_task = asyncio.create_task(self._health_monitor(), name="health_monitor")

    async def stop(self, asset: str) -> bool:
        """Cancel and discard the task for ``asset``.

        Returns:
            bool: True if agent was running and stopped, False if already stopped
        """
        if asset in self._stopping:
            return False
        self._stopping.add(asset)
        try:
            scheduled = self._tasks.pop(asset, None)
            immediate = self._active_cycles.get(asset)
            tasks = {task for task in (scheduled, immediate) if task is not None}
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

            from packages.core.services.code_sniper_monitor import shutdown_code_sniper_monitor
            agent_id = self._cfg.get(asset, {}).get("agent_id") or asset
            monitor_stopped = await shutdown_code_sniper_monitor(agent_id)

            self._restart_counts.pop(asset, None)
            self._last_restart.pop(asset, None)
            self._errors.pop(asset, None)
            return bool(tasks or monitor_stopped)
        finally:
            self._stopping.discard(asset)

    async def _run_cycle(self, asset: str, cfg: Mapping[str, Any]) -> Any:
        if asset in self._active_cycles or asset in self._stopping:
            logger.info(f"Skipping overlapping or stopped cycle for {asset}")
            return None
        task = asyncio.current_task()
        assert task is not None
        self._active_cycles[asset] = task
        try:
            return await self._runner(asset, cfg["N"], cfg)
        finally:
            self._active_cycles.pop(asset, None)
    
    async def stop_all(self) -> Dict[str, bool]:
        """Stop all running agents.
        
        Returns:
            Dict[str, bool]: Status of stop operation for each agent
        """
        results = {}
        for asset in list(self._tasks.keys()):
            results[asset] = await self.stop(asset)
        
        logger.info(f"STOPPED ALL: {len(results)} agents stopped by user request")
        return results
    
    def force_stop(self, asset: str) -> bool:
        """Force stop an agent even if it's unresponsive.
        
        Returns:
            bool: True if force stop was attempted, False if agent not found
        """
        task = self._tasks.pop(asset, None)
        if task is not None:
            if not task.done():
                task.cancel()
                logger.warning(f"FORCE STOPPED: Agent {asset} force stopped due to unresponsive behavior")
            
            # Forcefully clean up all tracking
            self._restart_counts.pop(asset, None)
            self._last_restart.pop(asset, None)
            self._errors.pop(asset, None)
            
            return True
        else:
            logger.warning(f"Agent {asset} not found for force stop")
            return False

    async def run_once(
        self, asset: str, cfg: Mapping[str, Any] | None = None
    ) -> Any:
        """Run the agent once for ``asset``."""

        if cfg is not None:
            cfg = dict(cfg)
            cfg["run_interval"] = self._normalize_interval(cfg.get("run_interval"))
            self._cfg[asset] = cfg
        else:
            cfg = self._cfg[asset]

        is_mt5 = bool(cfg.get("broker") == "mt5" or cfg.get("mt5_service"))
        try:
            now_utc = datetime.now(timezone.utc)
            now_local = now_utc.astimezone(session.IST)
            
            # Extract the actual symbol from config for session validation
            # The 'asset' parameter is the agent_id, but we need the trading symbol
            symbol = cfg.get("symbol")
            if not symbol:
                # Fallback: try to get symbol from instrument config
                instrument = cfg.get("instrument", {})
                if isinstance(instrument, dict):
                    symbol = instrument.get("symbol")
                if not symbol:
                    symbol = asset  # Last resort fallback
            
            # Check execution mode and timing
            execution_mode = cfg.get("execution_mode", "run_now")
            scheduled_time = cfg.get("scheduled_time")
            
            if execution_mode == "scheduled" and scheduled_time:
                # For scheduled mode, check if it's time to run
                if now_utc < scheduled_time:
                    logger.info(f"Scheduled agent {asset} waiting for scheduled time: {scheduled_time}")
                    return None
                else:
                    logger.info(f"Scheduled agent {asset} executing at scheduled time: {scheduled_time}")
            
            # Use the actual symbol for session validation (e.g., "XAUUSD", "BTCUSD")
            if not session.is_session_open(now_local, symbol):
                logger.info(f"Session closed for {symbol} (agent: {asset}) - skipping run_once")
                return None
            
            return await self._run_cycle(asset, cfg)
        except Exception as exc:  # pragma: no cover - defensive
            self._errors[asset] = exc
            raise

    def status(self, asset: str) -> Mapping[str, Any]:
        """Return comprehensive execution status for ``asset``."""

        task = self._tasks.get(asset)
        status_info = {
            "running": False,
            "restart_count": self._restart_counts.get(asset, 0),
            "last_restart": self._last_restart.get(asset),
            "last_error": self._errors.get(asset)
        }
        
        if task is None:
            status_info["status"] = "not_started"
            err = self._errors.get(asset)
            if err is not None:
                status_info["status"] = "failed"
                status_info["error"] = str(err)
            return status_info

        if task.cancelled():
            status_info["status"] = "stopped"
            status_info["cancelled"] = True
            return status_info

        if task.done():
            status_info["status"] = "completed"
            exc = task.exception() or self._errors.get(asset)
            if exc is not None:
                status_info["status"] = "failed"
                status_info["error"] = str(exc)
            return status_info

        # Task is running
        status_info["running"] = True
        status_info["status"] = "running"
        return status_info
    
    def get_all_status(self) -> Dict[str, Mapping[str, Any]]:
        """Get status for all agents."""
        all_status = {}
        
        # Get status for all known agents (running and stopped)
        all_assets = set(self._tasks.keys()) | set(self._cfg.keys()) | set(self._errors.keys())
        
        for asset in all_assets:
            all_status[asset] = self.status(asset)
        
        return all_status

    async def _health_monitor(self) -> None:
        """Monitor agent health and automatically restart failed agents."""
        logger.info("Health monitor started")
        
        while True:
            try:
                await asyncio.sleep(self._health_check_interval_minutes * 60)
                
                current_time = datetime.now(timezone.utc)
                failed_assets = []
                
                for asset, task in list(self._tasks.items()):
                    if task.done():
                        exc = task.exception()
                        if exc is not None:
                            logger.warning(f"Agent {asset} task completed with exception: {exc}")
                            failed_assets.append(asset)
                        else:
                            logger.warning(f"Agent {asset} task completed without exception")
                            failed_assets.append(asset)
                
                # Restart failed agents
                for asset in failed_assets:
                    await self._attempt_restart(asset, current_time)
                    
            except asyncio.CancelledError:
                logger.info("Health monitor cancelled")
                break
            except Exception as exc:
                logger.error(f"Health monitor error: {exc}", exc_info=True)
                await asyncio.sleep(60)  # Wait 1 minute before retrying

    async def _attempt_restart(self, asset: str, current_time: datetime) -> None:
        """Attempt to restart a failed agent with rate limiting."""
        if asset in self._stopping or asset not in self._tasks:
            return
        restart_count = self._restart_counts.get(asset, 0)
        last_restart = self._last_restart.get(asset)
        
        # Check if we've exceeded max restart attempts
        if restart_count >= self._max_restart_attempts:
            logger.error(f"Agent {asset} exceeded max restart attempts ({self._max_restart_attempts})")
            return
        
        # Check cooldown period
        if last_restart is not None:
            time_since_restart = current_time - last_restart
            if time_since_restart < timedelta(minutes=self._restart_cooldown_minutes):
                logger.info(f"Agent {asset} restart cooldown active, waiting...")
                return
        
        # Clean up old task
        old_task = self._tasks.pop(asset, None)
        if old_task is not None:
            old_task.cancel()
        
        # Get configuration
        cfg = self._cfg.get(asset)
        if cfg is None:
            logger.error(f"No configuration found for agent {asset}")
            return
        
        try:
            logger.info(f"Restarting agent {asset} (attempt {restart_count + 1}/{self._max_restart_attempts})")
            
            # Restart the agent
            self.start(asset, cfg)
            
            # Update restart tracking
            self._restart_counts[asset] = restart_count + 1
            self._last_restart[asset] = current_time
            self._errors.pop(asset, None)  # Clear previous error
            
            logger.info(f"Agent {asset} restarted successfully")
            
        except Exception as exc:
            logger.error(f"Failed to restart agent {asset}: {exc}", exc_info=True)
            self._errors[asset] = exc

    def get_health_status(self) -> Dict[str, Any]:
        """Get comprehensive health status of all agents."""
        status = {
            "total_agents": len(self._tasks),
            "running_agents": 0,
            "failed_agents": 0,
            "agents": {}
        }
        
        for asset, task in self._tasks.items():
            agent_status = {
                "running": not task.done(),
                "restart_count": self._restart_counts.get(asset, 0),
                "last_restart": self._last_restart.get(asset),
                "error": str(self._errors.get(asset)) if self._errors.get(asset) else None
            }
            
            if task.done():
                agent_status["running"] = False
                status["failed_agents"] += 1
            else:
                status["running_agents"] += 1
            
            status["agents"][asset] = agent_status
        
        return status


__all__ = ["Supervisor"]
