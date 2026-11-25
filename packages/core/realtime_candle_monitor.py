"""
Real-time candle monitoring system using MT5 polling for 5-minute candle closes.

This module provides a robust monitoring system that:
1. Polls MT5 for new 5-minute candle closes
2. Validates patterns with historical context
3. Triggers trades when conditions are met
4. Handles multiple assets and agents simultaneously
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Callable, Set
import pandas as pd
import logging

from .logger import get_logger
from .trigger_check import enhanced_entry_trigger_met, validate_pattern_with_history, calculate_dynamic_buffer
from .slot_candle_matcher import SlotCandleMatcher
from .candle_cache import get_cache, get_cache_manager
from .config import REALTIME_POLL_INTERVAL, REALTIME_CANDLE_TIMEOUT
from .utils import calculate_atr
from packages.adapters.mt5_adapter import MT5Adapter

logger = get_logger(__name__)


class RealtimeCandleMonitor:
    """
    Real-time candle monitor that polls MT5 for 5-minute candle closes
    and validates patterns with historical context.
    """
    
    def __init__(self, mt5_adapter: MT5Adapter, candle_service: Any):
        self.mt5_adapter = mt5_adapter
        self.candle_service = candle_service
        self.slot_matcher = SlotCandleMatcher()
        
        # Active monitoring state
        self._active_levels: Dict[str, Dict[str, Any]] = {}  # level_id -> level_data
        self._asset_subscribers: Dict[str, List[Callable]] = {}  # asset -> callbacks
        self._monitoring_tasks: Dict[str, asyncio.Task] = {}  # asset -> task
        self._last_candle_times: Dict[str, datetime] = {}  # asset -> last_candle_time
        
        # Configuration
        self._poll_interval = REALTIME_POLL_INTERVAL
        self._candle_timeout = REALTIME_CANDLE_TIMEOUT
        
        # Statistics
        self._stats = {
            "candles_processed": 0,
            "triggers_found": 0,
            "trades_executed": 0,
            "errors": 0
        }
        
        logger.info("RealtimeCandleMonitor initialized")
    
    async def start_monitoring(self, assets: List[str]) -> None:
        """Start monitoring 5-minute candle closes for specified assets."""
        
        for asset in assets:
            if asset not in self._monitoring_tasks:
                task = asyncio.create_task(self._monitor_asset(asset))
                self._monitoring_tasks[asset] = task
                logger.info(f"Started monitoring {asset}")
        
        logger.info(f"Started monitoring {len(assets)} assets: {assets}")
    
    async def stop_monitoring(self, assets: Optional[List[str]] = None) -> None:
        """Stop monitoring for specified assets or all assets."""
        
        if assets is None:
            assets = list(self._monitoring_tasks.keys())
        
        for asset in assets:
            if asset in self._monitoring_tasks:
                task = self._monitoring_tasks[asset]
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                del self._monitoring_tasks[asset]
                logger.info(f"Stopped monitoring {asset}")
    
    async def add_level_for_monitoring(
        self,
        level_id: str,
        asset: str,
        level_value: float,
        direction: str,
        pattern_type: str,
        agent_id: str,
        prediction_data: Dict[str, Any],
        historical_candles: pd.DataFrame,
        callback: Optional[Callable] = None
    ) -> None:
        """
        Add a level for real-time monitoring with pattern validation.
        
        Parameters
        ----------
        level_id : str
            Unique identifier for this level
        asset : str
            Asset symbol to monitor
        level_value : float
            Price level to monitor
        direction : str
            Direction of the trade (long/short)
        pattern_type : str
            Type of pattern to validate
        agent_id : str
            Agent ID that generated this level
        prediction_data : Dict[str, Any]
            Full prediction data including buffer, confidence, etc.
        historical_candles : pd.DataFrame
            Historical candles for pattern context validation
        callback : Callable, optional
            Callback function to execute when trigger is found
        """
        
        # CRITICAL: Validate that historical candles are provided
        if historical_candles is None or len(historical_candles) == 0:
            logger.error(
                f"CRITICAL ERROR: Cannot add level {level_id} - NO HISTORICAL CANDLES PROVIDED",
                extra={
                    "level_id": level_id,
                    "asset": asset,
                    "level_value": level_value,
                    "direction": direction,
                    "pattern_type": pattern_type,
                    "agent_id": agent_id,
                    "error": "No historical candles provided for pattern validation"
                }
            )
            raise ValueError(f"No historical candles provided for level {level_id} - pattern validation requires historical data")
        
        level_data = {
            "level_id": level_id,
            "asset": asset,
            "level_value": level_value,
            "direction": direction,
            "pattern_type": pattern_type,
            "agent_id": agent_id,
            "prediction_data": prediction_data,
            "historical_candles": historical_candles,
            "callback": callback,
            "created_at": datetime.now(timezone.utc),
            "triggered": False
        }
        
        self._active_levels[level_id] = level_data
        
        # Add to slot matcher for basic tracking
        await self.slot_matcher.create_slot_match(
            asset=asset,
            slot_id=int(time.time()),  # Use timestamp as slot ID
            slot_timestamp=datetime.now(timezone.utc),
            gpt_response_id=level_id,
            level_value=level_value,
            direction=direction,
            agent_id=agent_id,
            prompt_hash=prediction_data.get("prompt_hash", ""),
            confidence=prediction_data.get("confidence"),
            pattern=pattern_type,
            broker_type="MT5",
            gpt_mode=prediction_data.get("gpt_mode", "realtime"),
            historical_candles=historical_candles
        )
        
        logger.info(
            f"Added level {level_id} for monitoring: {asset} {direction} at {level_value} "
            f"(pattern: {pattern_type})"
        )
    
    async def remove_level(self, level_id: str) -> None:
        """Remove a level from monitoring."""
        
        if level_id in self._active_levels:
            level_data = self._active_levels[level_id]
            del self._active_levels[level_id]
            logger.info(f"Removed level {level_id} from monitoring")
    
    async def _monitor_asset(self, asset: str) -> None:
        """Monitor 5-minute candle closes for a specific asset."""
        
        logger.info(f"Starting candle monitoring for {asset}")
        
        while True:
            try:
                await self._check_for_new_candle(asset)
                await asyncio.sleep(self._poll_interval)
                
            except asyncio.CancelledError:
                logger.info(f"Candle monitoring cancelled for {asset}")
                break
            except Exception as e:
                logger.error(f"Error monitoring {asset}: {e}")
                self._stats["errors"] += 1
                await asyncio.sleep(self._poll_interval)
    
    async def _check_for_new_candle(self, asset: str) -> None:
        """Check for new 5-minute candle close using rolling window approach."""
        
        try:
            # Get cache for this asset
            cache = get_cache(asset, "5m")
            
            # Check if cache is valid and fresh
            if not cache.is_cache_valid() or not cache.is_fresh():
                logger.warning(f"Cache invalid or stale for {asset}, skipping this check")
                return
            
            # Fetch only the latest closed candle using lightweight helper
            new_candle = await self.candle_service.get_latest_closed_candle(asset, "5m")
            if not new_candle:
                logger.warning(f"Failed to fetch latest candle for {asset}")
                return
            
            # Check if this is a new candle
            last_time = self._last_candle_times.get(asset)
            if last_time and new_candle.time <= last_time:
                logger.debug(f"No new candle for {asset}, latest: {new_candle.time}")
                return  # No new candle
            
            # Update last candle time
            self._last_candle_times[asset] = new_candle.time
            
            # Rolling window update: Add new candle, remove oldest
            update_success = await cache.update_with_new_candle(new_candle, self.candle_service)
            if not update_success:
                logger.warning(f"Rolling window update failed for {asset}, using fallback")
                # Fallback to full refetch
                await cache.fallback_refetch(self.candle_service, "MT5", "monitor")
                return
            
            logger.debug(
                "Rolling window updated",
                extra={
                    "asset": asset,
                    "time": new_candle.time.isoformat(),
                    "window_size": len(cache._candles),
                }
            )
            
            # Process triggers for this new candle using cached data
            # Convert Candle object to dict for compatibility
            candle_dict = {
                "time": new_candle.time,
                "open": float(new_candle.open),
                "high": float(new_candle.high),
                "low": float(new_candle.low),
                "close": float(new_candle.close),
                "volume": float(new_candle.volume) if new_candle.volume else 0
            }
            await self._process_candle_triggers_with_cache(asset, candle_dict, cache)
            
            self._stats["candles_processed"] += 1
            
        except Exception as e:
            logger.error(f"Error checking new candle for {asset}: {e}")
            self._stats["errors"] += 1
    
    async def _get_latest_candle(self, asset: str) -> Optional[Dict[str, Any]]:
        """Get the latest 5-minute candle from MT5."""
        
        try:
            # Use MT5 adapter to get latest candle
            if hasattr(self.mt5_adapter, 'get_latest_candle'):
                candle = await self.mt5_adapter.get_latest_candle(asset, "M5")
                return candle
            
            # Fallback: use candle service
            c5_raw, _ = await self.candle_service.fetch(asset, n5=1, n15=1)
            if c5_raw and len(c5_raw) > 0:
                latest = c5_raw[-1]
                
                # Convert to dict format
                if hasattr(latest, '__dict__'):
                    return {
                        "time": getattr(latest, 'time', None),
                        "open": float(getattr(latest, 'open', 0)),
                        "high": float(getattr(latest, 'high', 0)),
                        "low": float(getattr(latest, 'low', 0)),
                        "close": float(getattr(latest, 'close', 0)),
                        "volume": float(getattr(latest, 'volume', 0))
                    }
                elif isinstance(latest, dict):
                    return {
                        "time": latest.get('time'),
                        "open": float(latest.get('open', 0)),
                        "high": float(latest.get('high', 0)),
                        "low": float(latest.get('low', 0)),
                        "close": float(latest.get('close', 0)),
                        "volume": float(latest.get('volume', 0))
                    }
            
            return None
            
        except Exception as e:
            logger.error(f"Error getting latest candle for {asset}: {e}")
            return None
    
    async def _process_candle_triggers(self, asset: str, candle: Dict[str, Any]) -> None:
        """Process all active triggers for a new candle."""
        
        triggered_levels = []
        
        # Check all active levels for this asset
        for level_id, level_data in self._active_levels.items():
            if level_data["asset"] != asset or level_data["triggered"]:
                continue
            
            try:
                # Validate pattern with historical context
                is_triggered = await self._validate_trigger(
                    level_data, candle
                )
                
                if is_triggered:
                    triggered_levels.append((level_id, level_data))
                    level_data["triggered"] = True
                    
            except Exception as e:
                logger.error(f"Error validating trigger for {level_id}: {e}")
                self._stats["errors"] += 1
        
        # Execute callbacks for triggered levels
        for level_id, level_data in triggered_levels:
            await self._execute_trigger_callback(level_id, level_data, candle)
            self._stats["triggers_found"] += 1
    
    async def _process_candle_triggers_with_cache(
        self, 
        asset: str, 
        candle: Dict[str, Any], 
        cache: Any
    ) -> None:
        """Process all active triggers for a new candle using cached data."""
        
        triggered_levels = []
        
        # Check all active levels for this asset
        for level_id, level_data in self._active_levels.items():
            if level_data["asset"] != asset or level_data["triggered"]:
                continue
            
            try:
                # Use cached historical candles and current candle
                historical_candles = cache.get_historical_candles()
                current_candle = cache.get_current_candle()
                
                if historical_candles is None or current_candle is None:
                    logger.warning(f"No cached data available for {level_id}")
                    continue
                
                # Validate pattern with cached historical context
                is_triggered = await self._validate_trigger_with_cache(
                    level_data, current_candle, historical_candles
                )
                
                if is_triggered:
                    triggered_levels.append((level_id, level_data))
                    level_data["triggered"] = True
                    
            except Exception as e:
                logger.error(f"Error validating trigger for {level_id}: {e}")
                self._stats["errors"] += 1
        
        # Execute callbacks for triggered levels
        for level_id, level_data in triggered_levels:
            await self._execute_trigger_callback(level_id, level_data, candle)
            self._stats["triggers_found"] += 1
    
    async def _validate_trigger_with_cache(
        self,
        level_data: Dict[str, Any],
        current_candle: Dict[str, Any],
        historical_candles: pd.DataFrame
    ) -> bool:
        """
        Validate if a trigger condition is met using cached data.
        """
        
        level_value = level_data["level_value"]
        direction = level_data["direction"]
        pattern_type = level_data["pattern_type"]
        prediction_data = level_data["prediction_data"]
        
        # Create prediction dict for trigger check
        pred_dict = {
            "condition": pattern_type,
            "level": level_value,
            "direction": direction,
            "buffer": prediction_data.get("buffer", 0.0),
            "confidence": prediction_data.get("confidence"),
            "pattern": prediction_data.get("pattern")
        }
        
        # Calculate dynamic buffer using ATR
        level = level_value
        current_price = current_candle.get("close", 0) if current_candle else 0
        
        # Calculate ATR for volatility
        market_volatility = 0.0
        if len(historical_candles) > 14:
            atr = calculate_atr(historical_candles, period=14)
            # Normalize ATR to volatility factor (0.0 to 1.0)
            market_volatility = min(1.0, atr / current_price) if current_price > 0 else 0.0
        
        # Calculate dynamic buffer
        dynamic_buffer = 0.0
        if level is not None and current_price > 0:
            try:
                dynamic_buffer = calculate_dynamic_buffer(
                    asset=level_data["asset"],
                    level=level,
                    current_price=current_price,
                    historical_data=historical_candles,
                    market_volatility=market_volatility
                )
            except Exception as e:
                logger.error(f"Error calculating dynamic buffer: {e}")
                # Fallback to base buffer from asset config
                from .trigger_check import get_asset_config
                try:
                    config = get_asset_config(level_data["asset"])
                    dynamic_buffer = config["base_buffer"]
                except:
                    dynamic_buffer = 10.0  # Default fallback
        
        # Use enhanced trigger check with calculated buffer
        try:
            trigger_met = enhanced_entry_trigger_met(
                pred=pred_dict,
                historical_candles=historical_candles,
                current_candle=current_candle,
                buffer=dynamic_buffer,
                asset=level_data["asset"]
            )
            
            if trigger_met:
                logger.info(
                    f"Trigger validated with cache: {level_data['asset']} "
                    f"{direction} {pattern_type} at {level_value}"
                )
            
            return trigger_met
            
        except Exception as e:
            logger.error(f"Error in cached trigger validation: {e}")
            return False
    
    async def _validate_trigger(
        self,
        level_data: Dict[str, Any],
        current_candle: Dict[str, Any]
    ) -> bool:
        """
        Validate if a trigger condition is met using enhanced pattern validation.
        """
        
        level_value = level_data["level_value"]
        direction = level_data["direction"]
        pattern_type = level_data["pattern_type"]
        prediction_data = level_data["prediction_data"]
        historical_candles = level_data["historical_candles"]
        level_id = level_data["level_id"]
        
        # REQUIRE historical candles for pattern validation - NO FALLBACK
        if historical_candles is None or len(historical_candles) == 0:
            logger.error(
                f"NO HISTORICAL CANDLES AVAILABLE for {level_id} - SKIPPING TRIGGER VALIDATION",
                extra={
                    "level_id": level_id,
                    "asset": level_data["asset"],
                    "level_value": level_value,
                    "direction": direction,
                    "pattern_type": pattern_type,
                    "error": "No historical candles for pattern validation"
                }
            )
            return False  # No fallback - skip validation
        
        # Calculate dynamic buffer using ATR
        current_price = current_candle.get("close", 0)
        
        # Calculate ATR for volatility
        market_volatility = 0.0
        if len(historical_candles) > 14:
            atr = calculate_atr(historical_candles, period=14)
            # Normalize ATR to volatility factor (0.0 to 1.0)
            market_volatility = min(1.0, atr / current_price) if current_price > 0 else 0.0
        
        # Calculate dynamic buffer
        dynamic_buffer = 0.0
        if level_value is not None and current_price > 0:
            try:
                dynamic_buffer = calculate_dynamic_buffer(
                    asset=level_data["asset"],
                    level=level_value,
                    current_price=current_price,
                    historical_data=historical_candles,
                    market_volatility=market_volatility
                )
            except Exception as e:
                logger.error(f"Error calculating dynamic buffer for {level_id}: {e}")
                # Fallback to base buffer from asset config
                from .trigger_check import get_asset_config
                try:
                    config = get_asset_config(level_data["asset"])
                    dynamic_buffer = config["base_buffer"]
                except:
                    dynamic_buffer = 10.0  # Default fallback
        
        # Use enhanced pattern validation with calculated buffer
        return validate_pattern_with_history(
            pattern_type=pattern_type,
            level=level_value,
            direction=direction,
            historical_candles=historical_candles,
            current_candle=current_candle,
            asset=level_data["asset"],
            buffer=dynamic_buffer
        )
    
    async def _execute_trigger_callback(
        self,
        level_id: str,
        level_data: Dict[str, Any],
        trigger_candle: Dict[str, Any]
    ) -> None:
        """Execute callback when a trigger is found."""
        
        try:
            callback = level_data.get("callback")
            if callback:
                await callback(
                    level_id=level_id,
                    level_data=level_data,
                    trigger_candle=trigger_candle
                )
                self._stats["trades_executed"] += 1
            
            logger.info(
                f"Trigger executed for {level_id}: {level_data['asset']} "
                f"{level_data['direction']} at {level_data['level_value']} "
                f"(pattern: {level_data['pattern_type']})"
            )
            
        except Exception as e:
            logger.error(f"Error executing trigger callback for {level_id}: {e}")
            self._stats["errors"] += 1
    
    def get_stats(self) -> Dict[str, Any]:
        """Get monitoring statistics."""
        
        return {
            **self._stats,
            "active_levels": len(self._active_levels),
            "monitored_assets": len(self._monitoring_tasks),
            "last_candle_times": {
                asset: time.isoformat() if time else None
                for asset, time in self._last_candle_times.items()
            }
        }
    
    def get_active_levels(self) -> Dict[str, Dict[str, Any]]:
        """Get all active levels being monitored."""
        
        return dict(self._active_levels)
    
    async def cleanup_expired_levels(self, max_age_hours: int = 24) -> None:
        """Clean up levels that have been active for too long."""
        
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        expired_levels = []
        
        for level_id, level_data in self._active_levels.items():
            if level_data["created_at"] < cutoff_time:
                expired_levels.append(level_id)
        
        for level_id in expired_levels:
            await self.remove_level(level_id)
            logger.info(f"Cleaned up expired level: {level_id}")
        
        if expired_levels:
            logger.info(f"Cleaned up {len(expired_levels)} expired levels")


# Global instance for easy access
_realtime_monitor: Optional[RealtimeCandleMonitor] = None


def get_realtime_monitor() -> Optional[RealtimeCandleMonitor]:
    """Get the global real-time monitor instance."""
    return _realtime_monitor


def set_realtime_monitor(monitor: RealtimeCandleMonitor) -> None:
    """Set the global real-time monitor instance."""
    global _realtime_monitor
    _realtime_monitor = monitor


async def initialize_realtime_monitor(mt5_adapter: MT5Adapter, candle_service: Any) -> RealtimeCandleMonitor:
    """Initialize the global real-time monitor."""
    global _realtime_monitor
    _realtime_monitor = RealtimeCandleMonitor(mt5_adapter, candle_service)
    return _realtime_monitor
