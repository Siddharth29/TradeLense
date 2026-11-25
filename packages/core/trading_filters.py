"""
Trading Filters Service - Apply the same filtering conditions from backtest to live trading.

This service implements the same filtering logic used in the backtest to ensure
only high-quality trades are executed in live trading.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass

from .config import (
    TRADING_FILTER_MIN_RR,
    TRADING_FILTER_MIN_TARGET_POINTS,
    TRADING_FILTER_SLIPPAGE,
)

logger = logging.getLogger(__name__)


@dataclass
class FilterResult:
    """Result of applying trading filters to a signal."""
    passed: bool
    reasons: List[str]
    slippage_applied: bool = False
    adjusted_entry: Optional[float] = None
    adjusted_stop: Optional[float] = None
    adjusted_target: Optional[float] = None


class TradingFilters:
    """
    Trading filters service that applies the same conditions as the backtest.
    
    Filters applied:
    1. Minimum Risk/Reward ratio (default: 2.0)
    2. Minimum target points (asset-specific)
    3. Slippage consideration
    4. Active trade tracking (no multiple trades per asset)
    """
    
    def __init__(self):
        self.filters = {
            'min_risk_reward': TRADING_FILTER_MIN_RR,
            'min_target_points': dict(TRADING_FILTER_MIN_TARGET_POINTS),
            'slippage': dict(TRADING_FILTER_SLIPPAGE),
        }
        
        # Active trades tracking
        self.active_trades: Dict[str, Dict[str, Any]] = {}
        
        logger.info("TradingFilters initialized with backtest conditions")
    
    def apply_filters(
        self, 
        signal: Dict[str, Any], 
        asset: str,
        current_price: Optional[float] = None
    ) -> FilterResult:
        """
        Apply comprehensive trading filters to determine if signal should be executed.
        
        This method implements the same filtering logic used in backtest to ensure
        only high-quality trades are executed in live trading.
        
        Args:
            signal: Signal data from GPT response
            asset: Asset symbol
            current_price: Current market price (optional)
            
        Returns:
            FilterResult with pass/fail status and reasons
        """
        filter_result = FilterResult(
            passed=True,
            reasons=[],
            slippage_applied=False
        )
        
        # Extract and validate signal parameters
        level = signal.get('level', 0)
        direction = signal.get('direction', '')
        stop_loss = signal.get('stop', 0)
        take_profit = signal.get('target', 0)
        confidence = signal.get('confidence', 0)
        pattern = signal.get('pattern', '')
        
        # Validate required fields
        if not level or level <= 0:
            filter_result.passed = False
            filter_result.reasons.append(f"Invalid level: {level}")
            return filter_result
        
        if not direction or direction.lower() not in ['long', 'short', 'buy', 'sell']:
            filter_result.passed = False
            filter_result.reasons.append(f"Invalid direction: {direction}")
            return filter_result
        
        if not stop_loss or stop_loss <= 0:
            filter_result.passed = False
            filter_result.reasons.append(f"Invalid stop loss: {stop_loss}")
            return filter_result
        
        if not take_profit or take_profit <= 0:
            filter_result.passed = False
            filter_result.reasons.append(f"Invalid take profit: {take_profit}")
            return filter_result
        
        # Normalize direction
        direction = direction.lower()
        if direction in ['buy']:
            direction = 'long'
        elif direction in ['sell']:
            direction = 'short'
        
        # Convert confidence to float if needed
        if isinstance(confidence, str):
            confidence_map = {'high': 0.8, 'medium': 0.6, 'low': 0.4}
            confidence = confidence_map.get(confidence.lower(), 0.6)
        elif confidence is None:
            confidence = 0.6
        
        # Filter 1: Check for active trade on same asset
        if asset in self.active_trades:
            active_trade = self.active_trades[asset]
            signal_time = datetime.now(timezone.utc)
            
            # Check if active trade is still running
            if signal_time < active_trade['exit_time']:
                filter_result.passed = False
                filter_result.reasons.append(
                    f"Active trade on {asset} until {active_trade['exit_time']}"
                )
                logger.info(f"🚫 BLOCKING signal for {asset}: Active trade exists until {active_trade['exit_time']}")
                return filter_result
            else:
                # Active trade has ended, remove from tracking
                del self.active_trades[asset]
                logger.info(f"✅ Active trade for {asset} has ended, removing from tracking")
        
        # Filter 2: Validate stop/target logic
        if direction == 'long':
            if stop_loss >= level:
                filter_result.passed = False
                filter_result.reasons.append(f"Long trade: stop ({stop_loss}) >= level ({level})")
                return filter_result
            if take_profit <= level:
                filter_result.passed = False
                filter_result.reasons.append(f"Long trade: target ({take_profit}) <= level ({level})")
                return filter_result
        elif direction == 'short':
            if stop_loss <= level:
                filter_result.passed = False
                filter_result.reasons.append(f"Short trade: stop ({stop_loss}) <= level ({level})")
                return filter_result
            if take_profit >= level:
                filter_result.passed = False
                filter_result.reasons.append(f"Short trade: target ({take_profit}) >= level ({level})")
                return filter_result
        
        # Filter 3: Minimum Risk/Reward Ratio
        if level and stop_loss and take_profit:
            if direction == 'long':
                risk = abs(level - stop_loss)
                reward = abs(take_profit - level)
            else:  # short
                risk = abs(stop_loss - level)
                reward = abs(level - take_profit)
            
            if risk > 0:
                rr_ratio = reward / risk
                if rr_ratio < self.filters['min_risk_reward']:
                    filter_result.passed = False
                    filter_result.reasons.append(
                        f"Low RR: {rr_ratio:.2f} < {self.filters['min_risk_reward']}"
                    )
        
        # Filter 4: Market hours validation (for Indian markets only)
        if asset in ['NIFTY', 'BANKNIFTY', 'FINNIFTY']:
            from datetime import datetime, time
            from zoneinfo import ZoneInfo
            
            # Convert to IST for Indian markets
            ist = ZoneInfo("Asia/Kolkata")
            now_ist = datetime.now(ist)
            current_time = now_ist.time()
            
            # Indian market hours: 9:15 AM to 3:30 PM IST
            market_open = time(9, 15)
            market_close = time(15, 30)
            
            if not (market_open <= current_time <= market_close):
                filter_result.passed = False
                filter_result.reasons.append(f"Outside market hours: {current_time} IST")
                logger.info(f"🚫 FILTERED signal for {asset}: Outside market hours: {current_time} IST")
            else:
                logger.info(f"✅ Market hours check passed for {asset}: {current_time} IST")
        else:
            # MT5 assets (BTC, GBP, XAU, ETH) run 24/7
            logger.info(f"✅ MT5 asset {asset} - 24/7 trading allowed")
        
        # Filter 5: Pattern validation
        if pattern and pattern.lower() not in [
            'breakout', 'retest', 'liquidity sweep', 'range breakout', 
            'fvg fill', 'orderblock', 'compression breakout', 'bos'
        ]:
            filter_result.passed = False
            filter_result.reasons.append(f"Unsupported pattern: {pattern}")
        
        # Filter 6: Confidence threshold
        if confidence < 0.5:  # Minimum 50% confidence
            filter_result.passed = False
            filter_result.reasons.append(f"Low confidence: {confidence:.2f} < 0.5")
        
        # Filter 7: Minimum Target Points (No scalp moves)
        if take_profit and level:
            target_points = abs(take_profit - level)
            min_target = self.filters['min_target_points'].get(
                asset, 
                self.filters['min_target_points']['default']
            )
            
            if target_points < min_target:
                filter_result.passed = False
                filter_result.reasons.append(
                    f"Small target: {target_points:.5f} < {min_target} (scalp move)"
                )
                logger.info(f"🚫 FILTERED signal for {asset}: Small target: {target_points:.5f} < {min_target} (scalp move)")
            else:
                logger.info(f"✅ Target size check passed for {asset}: {target_points:.5f} >= {min_target}")
        
        # Apply slippage if filters pass
        if filter_result.passed:
            slippage = self.filters['slippage'].get(
                asset, 
                self.filters['slippage']['default']
            )
            
            if direction.upper() in ['BUY', 'LONG']:
                # For long positions, add slippage to entry (worse price)
                adjusted_entry = level + slippage
                adjusted_stop = stop_loss + slippage
                adjusted_target = take_profit + slippage
            else:  # SELL/SHORT
                # For short positions, subtract slippage from entry (worse price)
                adjusted_entry = level - slippage
                adjusted_stop = stop_loss - slippage
                adjusted_target = take_profit - slippage
            
            filter_result.slippage_applied = True
            filter_result.adjusted_entry = adjusted_entry
            filter_result.adjusted_stop = adjusted_stop
            filter_result.adjusted_target = adjusted_target
            
            logger.info(f"📊 Applied slippage {slippage} to {asset} entry level")
        
        # Comprehensive logging
        if not filter_result.passed:
            logger.info(
                f"🚫 FILTERED signal for {asset}: {', '.join(filter_result.reasons)}",
                extra={
                    "asset": asset,
                    "direction": direction,
                    "level": level,
                    "stop": stop_loss,
                    "target": take_profit,
                    "pattern": pattern,
                    "confidence": confidence,
                    "filter_reasons": filter_result.reasons,
                    "current_price": current_price
                }
            )
        else:
            logger.info(
                f"✅ Signal for {asset} passed all filters",
                extra={
                    "asset": asset,
                    "direction": direction,
                    "level": level,
                    "stop": stop_loss,
                    "target": take_profit,
                    "pattern": pattern,
                    "confidence": confidence,
                    "risk_reward_ratio": abs(take_profit - level) / abs(level - stop_loss) if direction == 'long' else abs(level - take_profit) / abs(stop_loss - level),
                    "target_points": abs(take_profit - level),
                    "slippage_applied": filter_result.slippage_applied,
                    "current_price": current_price
                }
            )
        
        return filter_result
    
    def track_active_trade(
        self, 
        asset: str, 
        entry_time: datetime, 
        exit_time: datetime,
        trade_id: str
    ) -> None:
        """
        Track an active trade to prevent multiple trades on same asset.
        
        Args:
            asset: Asset symbol
            entry_time: Trade entry time
            exit_time: Expected trade exit time
            trade_id: Unique trade identifier
        """
        self.active_trades[asset] = {
            'entry_time': entry_time,
            'exit_time': exit_time,
            'trade_id': trade_id
        }
        
        logger.info(f"📊 Tracking active trade for {asset}: {entry_time} to {exit_time}")
    
    def clear_active_trade(self, asset: str) -> None:
        """Clear active trade tracking for an asset."""
        if asset in self.active_trades:
            del self.active_trades[asset]
            logger.info(f"✅ Cleared active trade tracking for {asset}")
    
    def get_active_trades(self) -> Dict[str, Dict[str, Any]]:
        """Get all currently active trades."""
        return self.active_trades.copy()
    
    def cleanup_expired_trades(self) -> None:
        """Remove expired trades from tracking."""
        now = datetime.now(timezone.utc)
        expired_assets = []
        
        for asset, trade_info in self.active_trades.items():
            if now > trade_info['exit_time']:
                expired_assets.append(asset)
        
        for asset in expired_assets:
            del self.active_trades[asset]
            logger.info(f"🧹 Cleaned up expired trade for {asset}")
    
    def update_filter_config(self, new_config: Dict[str, Any]) -> None:
        """Update filter configuration."""
        self.filters.update(new_config)
        logger.info(f"Updated filter configuration: {new_config}")
    
    def get_filter_stats(self) -> Dict[str, Any]:
        """Get current filter statistics."""
        return {
            'active_trades_count': len(self.active_trades),
            'active_assets': list(self.active_trades.keys()),
            'filter_config': self.filters.copy()
        }


# Global instance for use across the application
trading_filters = TradingFilters()
