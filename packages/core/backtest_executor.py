"""Backtest executor for simulating code execution and trade outcomes."""

from __future__ import annotations

from typing import Any, Dict, List, Optional
import pandas as pd

from .trade_sim import simulate_trade
from .code_executor import execute_generated_code
from .logger import get_logger
from .utils import _first_not_none

logger = get_logger(__name__)


class BacktestExecutor:
    """Simulates code execution and trade outcomes on historical candles."""
    
    def __init__(self):
        """Initialize the backtest executor."""
        pass
    
    def simulate_code_execution(
        self,
        code: str | None,
        candles_5m: List[Dict[str, Any]],
        start_index: int = 0
    ) -> Dict[str, Any]:
        """Simulate code execution on historical candles.
        
        This method executes monitoring code on historical candles using the
        same sandbox environment as live trading, ensuring alignment between
        backtest and live flows.
        
        Parameters
        ----------
        code : str, optional
            Monitoring code to execute (Python code from code sniper)
        candles_5m : List[Dict[str, Any]]
            List of 5-minute candle dictionaries
        start_index : int
            Starting index in the candles list (default: 0)
        
        Returns
        -------
        Dict[str, Any]
            Execution result with trigger status, decision, error (if any)
        """
        if not code or not candles_5m:
            return {
                "code_executed": False,
                "start_index": start_index,
                "candles_processed": 0,
                "trigger_met": False,
                "error": "No code or candles provided"
            }
        
        try:
            # Get current candle and historical candles for execution
            if len(candles_5m) < start_index + 1:
                return {
                    "code_executed": False,
                    "error": f"Insufficient candles: need at least {start_index + 1}, got {len(candles_5m)}"
                }
            
            # Get current candle (at start_index)
            current_candle = candles_5m[start_index] if start_index < len(candles_5m) else candles_5m[-1]
            
            # Get last 40 historical candles before current (for indicators)
            historical_start = max(0, start_index - 40)
            historical_candles = candles_5m[historical_start:start_index] if start_index > 0 else []

            # If not enough historical candles, use what we have
            if len(historical_candles) < 40 and start_index > 0:
                logger.warning(f"Only {len(historical_candles)} historical candles available for code execution")
            
            # Execute code using the same sandbox as live trading
            # Pass backtest_mode=True to skip time alignment validation
            result = execute_generated_code(
                code=code,
                current_candle=current_candle,
                historical_candles=historical_candles,
                backtest_mode=True
            )
            
            # Add execution metadata
            result["code_executed"] = True
            result["start_index"] = start_index
            result["candles_processed"] = 1
            result["current_candle_index"] = start_index
            result["historical_candles_count"] = len(historical_candles)
            
            return result
            
        except Exception as e:
            logger.error(f"Code execution failed in backtest: {e}")
            return {
                "code_executed": True,
                "error": str(e),
                "start_index": start_index,
                "candles_processed": 0,
                "trigger_met": False
            }
    
    def simulate_trade_outcome(
        self,
        signal: Dict[str, Any],
        future_candles: pd.DataFrame,
        lot_size: float | None = None,
    ) -> Dict[str, Any]:
        """Calculate trade result from signal and future candles.
        
        Parameters
        ----------
        signal : Dict[str, Any]
            Signal dictionary with keys: direction, level/entry_price, stop/stoploss, target
        future_candles : pd.DataFrame
            DataFrame of future candles (indexed by datetime) to simulate on
        lot_size : float, optional
            Lot size for PnL scaling (forwarded to simulate_trade).
        
        Returns
        -------
        Dict[str, Any]
            Trade outcome with entry/exit prices, P&L, result, etc.
        """
        try:
            # Normalize signal format
            pred = {
                "direction": signal.get("direction"),
                "level": _first_not_none(signal.get("level"), signal.get("entry_price"), signal.get("entry")),
                "stop": _first_not_none(signal.get("stop"), signal.get("stoploss"), signal.get("stop_loss")),
                "target": _first_not_none(signal.get("target"), signal.get("take_profit_1")),
                "pattern": _first_not_none(signal.get("pattern"), signal.get("condition")),
                "condition": _first_not_none(signal.get("condition"), signal.get("pattern")),
            }
            
            # Use existing simulate_trade function
            # Pass asset if available in signal data
            asset_symbol = signal.get("asset") or "default"
            outcome = simulate_trade(pred, future_candles, asset=asset_symbol, lot_size=lot_size)
            
            # Add signal data to outcome
            outcome["signal_data"] = signal
            
            return outcome
            
        except Exception as e:
            logger.error(f"Failed to simulate trade outcome: {e}")
            return {
                "result": "Error",
                "error": str(e),
                "profit": 0.0,
                "entry": None,
                "exit_price": None
            }

