"""Enhanced slot-based candle matching system with sophisticated pattern validation."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple
import logging
import pandas as pd

from packages.core.logger import get_logger
from packages.core.trigger_check import validate_pattern_with_history, enhanced_entry_trigger_met, calculate_dynamic_buffer
import numpy as np
from packages.db.repositories import comprehensive_logging as comp_logging
from packages.db.repositories import gpt_responses as gpt_repo

logger = get_logger(__name__)


def calculate_atr(candles: pd.DataFrame, period: int = 14) -> float:
    """Calculate Average True Range (ATR) from candle data."""
    if len(candles) < period + 1:
        return 0.0
    
    # Calculate True Range for each candle
    high = candles['high'].values
    low = candles['low'].values
    close = candles['close'].values
    
    # True Range = max(high-low, abs(high-prev_close), abs(low-prev_close))
    tr = np.maximum(
        high[1:] - low[1:],  # high - low
        np.maximum(
            np.abs(high[1:] - close[:-1]),  # abs(high - prev_close)
            np.abs(low[1:] - close[:-1])    # abs(low - prev_close)
        )
    )
    
    # Calculate ATR as simple moving average of True Range
    atr = np.mean(tr[-period:]) if len(tr) >= period else np.mean(tr)
    
    return float(atr)


class SlotCandleMatcher:
    """Enhanced slot-based candle matcher with sophisticated pattern validation."""
    
    def __init__(self):
        self._active_matches: Dict[str, Dict[str, Any]] = {}  # match_id -> match_data
        self._slot_cache: Dict[int, Dict[str, Any]] = {}  # slot_id -> slot_data
        self._historical_candles: Dict[str, pd.DataFrame] = {}  # asset -> historical_candles
        self._logger = get_logger(__name__)
    
    async def create_slot_match(
        self,
        asset: str,
        slot_id: int,
        slot_timestamp: datetime,
        gpt_response_id: str,
        level_value: float,
        direction: str,
        agent_id: str,
        prompt_hash: str,
        confidence: Optional[float] = None,
        pattern: Optional[str] = None,
        broker_type: Optional[str] = None,
        gpt_mode: Optional[str] = None,
        trigger_check_id: Optional[str] = None,
        historical_candles: Optional[pd.DataFrame] = None,
        prediction_data: Optional[Dict[str, Any]] = None,
        ttl_minutes: Optional[int] = None,
    ) -> str:
        """Create a new slot-candle match record when GPT draws a level."""
        
        # CRITICAL: Validate that historical candles are provided
        if historical_candles is None or len(historical_candles) == 0:
            self._logger.error(
                f"CRITICAL ERROR: Cannot create slot match - NO HISTORICAL CANDLES PROVIDED",
                extra={
                    "asset": asset,
                    "slot_id": slot_id,
                    "level_value": level_value,
                    "direction": direction,
                    "pattern": pattern,
                    "agent_id": agent_id,
                    "error": "No historical candles provided for pattern validation"
                }
            )
            raise ValueError(f"No historical candles provided for slot match - pattern validation requires historical data")
        
        level_drawn_at = datetime.now(timezone.utc)
        
        # Calculate expiry time if TTL is provided
        expires_at = None
        if ttl_minutes is not None:
            expires_at = level_drawn_at + timedelta(minutes=ttl_minutes)
        
        match_id = await comp_logging.store_slot_candle_match(
            asset=asset,
            slot_id=slot_id,
            slot_timestamp=slot_timestamp,
            gpt_response_id=gpt_response_id,
            level_drawn_at=level_drawn_at,
            level_value=level_value,
            direction=direction,
            trigger_met=False,
            agent_id=agent_id,
            prompt_hash=prompt_hash,
            trigger_check_id=trigger_check_id,
            confidence=confidence,
            pattern=pattern,
            broker_type=broker_type,
            gpt_mode=gpt_mode,
        )
        
        # Store in active matches for monitoring with enhanced data
        self._active_matches[match_id] = {
            "asset": asset,
            "slot_id": slot_id,
            "level_value": level_value,
            "direction": direction,
            "level_drawn_at": level_drawn_at,
            "agent_id": agent_id,
            "prompt_hash": prompt_hash,
            "pattern": pattern,
            "confidence": confidence,
            "historical_candles": historical_candles,
            "prediction_data": prediction_data or {},
            "triggered": False,
            "expires_at": expires_at,
        }
        
        # Cache slot data
        self._slot_cache[slot_id] = {
            "asset": asset,
            "timestamp": slot_timestamp,
            "level_value": level_value,
            "direction": direction,
        }
        
        self._logger.info(
            f"Created slot-candle match for {asset} slot {slot_id} with level {level_value} direction {direction}",
            extra={
                "asset": asset,
                "slot_id": slot_id,
                "agent_id": agent_id,
                "level_value": level_value,
                "direction": direction,
                "match_id": match_id,
            }
        )
        
        return match_id
    
    async def check_trigger_match(
        self,
        asset: str,
        candle_timestamp: datetime,
        open_price: float,
        high_price: float,
        low_price: float,
        close_price: float,
        volume: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """
        Check if any active levels are triggered by the current candle using sophisticated pattern validation.
        """
        
        triggered_matches = []
        current_candle = {
            "time": candle_timestamp,
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "volume": volume or 0
        }
        
        # Check for expired matches first
        current_time = datetime.now(timezone.utc)
        expired_matches = []
        for match_id, match_data in self._active_matches.items():
            expires_at = match_data.get("expires_at")
            if expires_at and current_time > expires_at:
                expired_matches.append(match_id)
        
        # Remove expired matches
        for match_id in expired_matches:
            self._logger.info(
                f"Removing expired level match {match_id} for {asset}",
                extra={
                    "asset": asset,
                    "match_id": match_id,
                    "expires_at": self._active_matches[match_id].get("expires_at"),
                    "current_time": current_time
                }
            )
            del self._active_matches[match_id]
        
        for match_id, match_data in self._active_matches.items():
            if match_data["asset"] != asset or match_data.get("triggered", False):
                continue
                
            level_value = match_data["level_value"]
            direction = match_data["direction"]
            pattern = match_data.get("pattern", "breakout")
            historical_candles = match_data.get("historical_candles")
            prediction_data = match_data.get("prediction_data", {})
            
            try:
                # REQUIRE historical candles for pattern validation - NO FALLBACK
                if historical_candles is None or len(historical_candles) == 0:
                    self._logger.error(
                        f"NO HISTORICAL CANDLES AVAILABLE for {match_id} - SKIPPING TRIGGER CHECK",
                        extra={
                            "asset": asset,
                            "match_id": match_id,
                            "level_value": level_value,
                            "direction": direction,
                            "pattern": pattern,
                            "error": "No historical candles for pattern validation"
                        }
                    )
                    continue  # Skip this match - no fallback
                
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
                            asset=asset,
                            level=level_value,
                            current_price=current_price,
                            historical_data=historical_candles,
                            market_volatility=market_volatility
                        )
                    except Exception as e:
                        self._logger.error(f"Error calculating dynamic buffer for {match_id}: {e}")
                        # Fallback to base buffer from asset config
                        from packages.core.trigger_check import get_asset_config
                        try:
                            config = get_asset_config(asset)
                            dynamic_buffer = config["base_buffer"]
                        except:
                            dynamic_buffer = 10.0  # Default fallback
                
                # Use sophisticated pattern validation with calculated buffer
                is_triggered = validate_pattern_with_history(
                    pattern_type=pattern,
                    level=level_value,
                    direction=direction,
                    historical_candles=historical_candles,
                    current_candle=current_candle,
                    asset=asset,
                    buffer=dynamic_buffer
                )
                
                if is_triggered:
                    # Mark as triggered to prevent duplicate triggers
                    match_data["triggered"] = True
                    
                    # Update the match with trigger information
                    await comp_logging.update_slot_candle_match_trigger(
                        match_id=match_id,
                        trigger_met=True,
                        trigger_timestamp=datetime.now(timezone.utc),
                        trigger_candle_timestamp=candle_timestamp,
                        trigger_candle_open=open_price,
                        trigger_candle_high=high_price,
                        trigger_candle_low=low_price,
                        trigger_candle_close=close_price,
                        trigger_candle_volume=volume,
                    )
                    
                    triggered_matches.append({
                        "match_id": match_id,
                        "asset": asset,
                        "level_value": level_value,
                        "direction": direction,
                        "pattern": pattern,
                        "confidence": match_data.get("confidence"),
                        "trigger_candle": current_candle,
                        "match_data": match_data,
                        "validation_method": "sophisticated" if historical_candles is not None else "simple"
                    })
                    
                    self._logger.info(
                        f"Pattern validated and triggered for {asset} match {match_id} at {candle_timestamp}",
                        extra={
                            "asset": asset,
                            "match_id": match_id,
                            "level_value": level_value,
                            "direction": direction,
                            "pattern": pattern,
                            "confidence": match_data.get("confidence"),
                            "validation_method": "sophisticated" if historical_candles is not None else "simple",
                            "trigger_candle_timestamp": candle_timestamp,
                            "trigger_candle_open": open_price,
                            "trigger_candle_high": high_price,
                            "trigger_candle_low": low_price,
                            "trigger_candle_close": close_price,
                        }
                    )
                    
            except Exception as e:
                self._logger.error(
                    f"Error validating pattern for {match_id}: {e}",
                    extra={
                        "asset": asset,
                        "match_id": match_id,
                        "level_value": level_value,
                        "direction": direction,
                        "pattern": pattern,
                        "error": str(e)
                    }
                )
        
        return triggered_matches
    
    def update_historical_candles(self, asset: str, candles: pd.DataFrame) -> None:
        """
        Update historical candles for an asset to be used in pattern validation.
        
        Parameters
        ----------
        asset : str
            Asset symbol
        candles : pd.DataFrame
            Historical candle data with columns: time, open, high, low, close, volume
        """
        
        if len(candles) > 0:
            self._historical_candles[asset] = candles.copy()
            self._logger.debug(f"Updated historical candles for {asset}: {len(candles)} candles")
            
            # Update historical candles for all active matches of this asset
            for match_id, match_data in self._active_matches.items():
                if match_data["asset"] == asset:
                    match_data["historical_candles"] = candles.copy()
    
    def get_historical_candles(self, asset: str) -> Optional[pd.DataFrame]:
        """Get historical candles for an asset."""
        return self._historical_candles.get(asset)
    
    async def cleanup_expired_matches(self, max_age_hours: int = 24) -> None:
        """
        Clean up expired matches that have been active for too long.
        
        Parameters
        ----------
        max_age_hours : int
            Maximum age in hours before a match is considered expired
        """
        
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        expired_matches = []
        
        for match_id, match_data in self._active_matches.items():
            if match_data["level_drawn_at"] < cutoff_time:
                expired_matches.append(match_id)
        
        for match_id in expired_matches:
            del self._active_matches[match_id]
            self._logger.info(f"Cleaned up expired match: {match_id}")
        
        if expired_matches:
            self._logger.info(f"Cleaned up {len(expired_matches)} expired matches")
    
    async def execute_trade_from_trigger(
        self,
        match_id: str,
        entry_price: float,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        trade_id: Optional[str] = None,
    ) -> bool:
        """Mark a triggered match as having an executed trade."""
        
        if match_id not in self._active_matches:
            self._logger.warning(f"Match {match_id} not found in active matches")
            return False
        
        success = await comp_logging.update_slot_candle_match_trigger(
            match_id=match_id,
            trigger_met=True,
            entry_price=entry_price,
            trade_executed=True,
            trade_id=trade_id,
        )
        
        if success:
            # Remove from active matches as it's now executed
            del self._active_matches[match_id]
            
            self._logger.info(
                f"Trade executed from trigger for match {match_id}",
                extra={
                    "match_id": match_id,
                    "entry_price": entry_price,
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "trade_id": trade_id,
                }
            )
        
        return success
    
    async def store_hourly_candle_with_metadata(
        self,
        asset: str,
        symbol: str,
        slot_id: int,
        slot_timestamp: datetime,
        open_price: float,
        high_price: float,
        low_price: float,
        close_price: float,
        candle_timestamp: datetime,
        volume: Optional[float] = None,
        market_session: Optional[str] = None,
    ) -> str:
        """Store an hourly candle with comprehensive metadata."""
        
        # Count signals and triggers for this slot
        gpt_signals_count = len([m for m in self._active_matches.values() 
                                if m["asset"] == asset and m["slot_id"] == slot_id])
        
        # Get triggers met count from database
        slot_matches = await comp_logging.get_slot_candle_matches(
            asset=asset,
            start_time=slot_timestamp,
            end_time=slot_timestamp + timedelta(hours=1),
            limit=1000,
        )
        triggers_met_count = len([m for m in slot_matches if m.get("trigger_met", False)])
        trades_executed_count = len([m for m in slot_matches if m.get("trade_executed", False)])
        
        candle_id = await comp_logging.store_hourly_candle(
            asset=asset,
            symbol=symbol,
            slot_id=slot_id,
            slot_timestamp=slot_timestamp,
            open=open_price,
            high=high_price,
            low=low_price,
            close=close_price,
            candle_timestamp=candle_timestamp,
            volume=volume,
            market_session=market_session,
            gpt_signals_count=gpt_signals_count,
            triggers_met_count=triggers_met_count,
            trades_executed_count=trades_executed_count,
        )
        
        self._logger.info(
            f"Stored hourly candle with metadata for {asset} {symbol} slot {slot_id}",
            extra={
                "asset": asset,
                "symbol": symbol,
                "slot_id": slot_id,
                "open": open_price,
                "high": high_price,
                "low": low_price,
                "close": close_price,
                "gpt_signals_count": gpt_signals_count,
                "triggers_met_count": triggers_met_count,
                "trades_executed_count": trades_executed_count,
            }
        )
        
        return candle_id
    
    def get_active_matches(self, asset: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """Get active matches, optionally filtered by asset."""
        if asset:
            return {k: v for k, v in self._active_matches.items() if v["asset"] == asset}
        return self._active_matches.copy()
    
    def get_slot_cache(self) -> Dict[int, Dict[str, Any]]:
        """Get the slot cache."""
        return self._slot_cache.copy()
    
    async def cleanup_old_matches(self, max_age_hours: int = 24) -> int:
        """Clean up old matches that are no longer active."""
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        cleaned_count = 0
        
        matches_to_remove = []
        for match_id, match_data in self._active_matches.items():
            if match_data["level_drawn_at"] < cutoff_time:
                matches_to_remove.append(match_id)
        
        for match_id in matches_to_remove:
            del self._active_matches[match_id]
            cleaned_count += 1
        
        if cleaned_count > 0:
            self._logger.info(f"Cleaned up {cleaned_count} old matches")
        
        return cleaned_count


# Global instance
_slot_matcher = SlotCandleMatcher()


def get_slot_matcher() -> SlotCandleMatcher:
    """Get the global slot matcher instance."""
    return _slot_matcher


__all__ = [
    "SlotCandleMatcher",
    "get_slot_matcher",
]
