"""Validation script to compare backtest trade timestamps with actual market data.

This script validates that:
1. Entry timestamps match actual trigger candle times
2. Exit timestamps match actual candle close times
3. Stop loss values match actual prices hit
4. Entry/exit prices align with market data
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from packages.core.historical_candle_service import HistoricalCandleService
from packages.core.logger import get_logger
from packages.db.repositories import backtest as backtest_repo

logger = get_logger(__name__)


class BacktestTimestampValidator:
    """Validates backtest trade timestamps against actual market data."""
    
    def __init__(self, asset: str = "BTCUSD"):
        self.asset = asset
        self.candle_service = HistoricalCandleService()
        self.validation_results: List[Dict[str, Any]] = []
    
    async def validate_backtest(
        self,
        backtest_id: str,
        tolerance_minutes: int = 5
    ) -> Dict[str, Any]:
        """Validate all trades in a backtest against market data.
        
        Parameters
        ----------
        backtest_id : str
            Backtest ID to validate
        tolerance_minutes : int
            Maximum allowed difference in minutes for timestamp validation (default: 5)
        
        Returns
        -------
        Dict[str, Any]
            Validation results with mismatches and statistics
        """
        logger.info(f"Starting validation for backtest: {backtest_id}")
        
        # Fetch backtest data
        backtest_doc = await backtest_repo.get_backtest(backtest_id)
        if not backtest_doc:
            raise ValueError(f"Backtest {backtest_id} not found")
        
        iterations = backtest_doc.get("iterations", [])
        if not iterations:
            raise ValueError(f"Backtest {backtest_id} has no iterations")
        
        # Extract trades from iterations
        trades = self._extract_trades_from_iterations(iterations)
        logger.info(f"Found {len(trades)} trades to validate")
        
        # Validate each trade
        validation_results = []
        for i, trade in enumerate(trades):
            try:
                result = await self._validate_trade(trade, tolerance_minutes)
                validation_results.append(result)
                
                if not result["is_valid"]:
                    logger.warning(
                        f"Trade {i+1}/{len(trades)} validation failed: {result['errors']}"
                    )
            except Exception as e:
                logger.error(f"Error validating trade {i+1}: {e}")
                validation_results.append({
                    "trade_index": i,
                    "is_valid": False,
                    "errors": [f"Validation error: {str(e)}"]
                })
        
        # Calculate statistics
        valid_count = sum(1 for r in validation_results if r["is_valid"])
        invalid_count = len(validation_results) - valid_count
        
        summary = {
            "backtest_id": backtest_id,
            "total_trades": len(trades),
            "valid_trades": valid_count,
            "invalid_trades": invalid_count,
            "validation_rate": valid_count / len(trades) if trades else 0.0,
            "tolerance_minutes": tolerance_minutes,
            "results": validation_results
        }
        
        logger.info(
            f"Validation complete: {valid_count}/{len(trades)} trades valid "
            f"({summary['validation_rate']*100:.1f}%)"
        )
        
        return summary
    
    def _extract_trades_from_iterations(
        self,
        iterations: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Extract completed trades from iterations.
        
        Only includes trades that have both entry_time and exit_time.
        """
        trades = []
        
        for iteration in iterations:
            outcome = iteration.get("outcome") or iteration.get("trade_outcome")
            if not outcome:
                continue
            
            result = outcome.get("result", "")
            entry_time = outcome.get("entry_time")
            exit_time = outcome.get("exit_time")
            
            # Only include trades that have both entry and exit times
            if entry_time and exit_time and result not in ["Open", "No Entry"]:
                trade = {
                    "iteration": iteration.get("iteration"),
                    "iteration_timestamp": iteration.get("timestamp") or iteration.get("iteration_timestamp"),
                    "direction": outcome.get("direction"),
                    "entry": outcome.get("entry"),
                    "stop": outcome.get("stop"),
                    "target": outcome.get("target"),
                    "entry_time": entry_time,
                    "exit_time": exit_time,
                    "exit_price": outcome.get("exit_price"),
                    "profit": outcome.get("profit", 0.0),
                    "result": result,
                    "entry_timestamp": iteration.get("prediction", {}).get("entry_timestamp"),
                    "pattern": iteration.get("prediction", {}).get("pattern"),
                    "trade_closed_at": outcome.get("trade_closed_at") or iteration.get("trade_outcome", {}).get("trade_closed_at")
                }
                trades.append(trade)
        
        return trades
    
    async def _validate_trade(
        self,
        trade: Dict[str, Any],
        tolerance_minutes: int
    ) -> Dict[str, Any]:
        """Validate a single trade against market data.
        
        Returns
        -------
        Dict[str, Any]
            Validation result with is_valid flag and list of errors
        """
        errors = []
        warnings = []
        
        # Parse timestamps
        entry_time = self._parse_timestamp(trade.get("entry_time"))
        exit_time = self._parse_timestamp(trade.get("exit_time"))
        trade_closed_at = self._parse_timestamp(trade.get("trade_closed_at"))
        
        if not entry_time or not exit_time:
            return {
                "trade_index": trade.get("iteration"),
                "is_valid": False,
                "errors": ["Missing entry_time or exit_time"]
            }
        
        if trade_closed_at and exit_time:
            time_diff_closed = abs((exit_time - trade_closed_at).total_seconds() / 60)
            if time_diff_closed > tolerance_minutes:
                errors.append(
                    f"trade_closed_at {trade_closed_at} differs from exit_time {exit_time} by {time_diff_closed:.1f} minutes"
                )
        elif trade_closed_at is None:
            warnings.append("trade_closed_at missing from trade outcome")
        
        # Fetch candles for entry and exit times
        start_date = entry_time - pd.Timedelta(minutes=10)
        end_date = exit_time + pd.Timedelta(minutes=10)
        
        try:
            candles_5m = await self.candle_service.fetch_historical_5m(
                self.asset,
                start_date,
                end_date,
                limit=1000
            )
        except Exception as e:
            return {
                "trade_index": trade.get("iteration"),
                "is_valid": False,
                "errors": [f"Failed to fetch candles: {str(e)}"]
            }
        
        if not candles_5m:
            return {
                "trade_index": trade.get("iteration"),
                "is_valid": False,
                "errors": ["No candles available for validation period"]
            }
        
        # Convert to DataFrame for easier manipulation
        df = pd.DataFrame(candles_5m)
        if 'time' in df.columns:
            df['time'] = pd.to_datetime(df['time'])
            df = df.set_index('time')
        
        # Validate entry timestamp
        entry_validation = self._validate_entry_timestamp(
            entry_time,
            trade.get("entry_timestamp"),
            df,
            tolerance_minutes
        )
        if not entry_validation["is_valid"]:
            errors.extend(entry_validation["errors"])
        if entry_validation.get("warnings"):
            warnings.extend(entry_validation["warnings"])
        
        # Validate exit timestamp
        exit_validation = self._validate_exit_timestamp(
            exit_time,
            trade.get("result"),
            df,
            tolerance_minutes
        )
        if not exit_validation["is_valid"]:
            errors.extend(exit_validation["errors"])
        if exit_validation.get("warnings"):
            warnings.extend(exit_validation["warnings"])
        
        # Validate stop loss
        if trade.get("result") in ["SL", "BE"]:
            sl_validation = self._validate_stop_loss(
                trade,
                df,
                entry_time,
                exit_time
            )
            if not sl_validation["is_valid"]:
                errors.extend(sl_validation["errors"])
            if sl_validation.get("warnings"):
                warnings.extend(sl_validation["warnings"])
        
        # Validate entry price
        entry_price_validation = self._validate_entry_price(
            trade,
            df,
            entry_time
        )
        if not entry_price_validation["is_valid"]:
            errors.extend(entry_price_validation["errors"])
        
        # Validate exit price
        exit_price_validation = self._validate_exit_price(
            trade,
            df,
            exit_time
        )
        if not exit_price_validation["is_valid"]:
            errors.extend(exit_price_validation["errors"])
        
        return {
            "trade_index": trade.get("iteration"),
            "is_valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
            "entry_time": entry_time.isoformat() if hasattr(entry_time, 'isoformat') else str(entry_time),
            "exit_time": exit_time.isoformat() if hasattr(exit_time, 'isoformat') else str(exit_time),
            "trade_closed_at": trade_closed_at.isoformat() if trade_closed_at and hasattr(trade_closed_at, 'isoformat') else str(trade_closed_at) if trade_closed_at else None,
            "entry_validation": entry_validation,
            "exit_validation": exit_validation
        }
    
    def _parse_timestamp(self, ts: Any) -> Optional[pd.Timestamp]:
        """Parse timestamp from various formats."""
        if ts is None:
            return None
        
        if isinstance(ts, pd.Timestamp):
            return ts
        
        if isinstance(ts, str):
            try:
                return pd.Timestamp(ts)
            except:
                return None
        
        if isinstance(ts, datetime):
            return pd.Timestamp(ts)
        
        return None
    
    def _validate_entry_timestamp(
        self,
        entry_time: pd.Timestamp,
        entry_timestamp_str: Optional[str],
        df: pd.DataFrame,
        tolerance_minutes: int
    ) -> Dict[str, Any]:
        """Validate entry timestamp matches actual candle time."""
        errors = []
        warnings = []
        
        # Find nearest candle to entry time
        nearest_candle_time = df.index[df.index.get_indexer([entry_time], method='nearest')[0]]
        time_diff = abs((nearest_candle_time - entry_time).total_seconds() / 60)
        
        if time_diff > tolerance_minutes:
            errors.append(
                f"Entry timestamp {entry_time} differs from nearest candle "
                f"{nearest_candle_time} by {time_diff:.1f} minutes "
                f"(tolerance: {tolerance_minutes} minutes)"
            )
        
        # Check if entry_timestamp from prediction matches
        if entry_timestamp_str:
            pred_entry_time = self._parse_timestamp(entry_timestamp_str)
            if pred_entry_time:
                pred_diff = abs((pred_entry_time - entry_time).total_seconds() / 60)
                if pred_diff > tolerance_minutes:
                    warnings.append(
                        f"Prediction entry_timestamp {pred_entry_time} differs from "
                        f"outcome entry_time {entry_time} by {pred_diff:.1f} minutes"
                    )
        
        return {
            "is_valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
            "entry_time": entry_time.isoformat(),
            "nearest_candle_time": nearest_candle_time.isoformat(),
            "time_difference_minutes": time_diff
        }
    
    def _validate_exit_timestamp(
        self,
        exit_time: pd.Timestamp,
        result: str,
        df: pd.DataFrame,
        tolerance_minutes: int
    ) -> Dict[str, Any]:
        """Validate exit timestamp matches actual candle close time."""
        errors = []
        warnings = []
        
        # Find nearest candle to exit time
        nearest_candle_time = df.index[df.index.get_indexer([exit_time], method='nearest')[0]]
        time_diff = abs((nearest_candle_time - exit_time).total_seconds() / 60)
        
        if time_diff > tolerance_minutes:
            errors.append(
                f"Exit timestamp {exit_time} differs from nearest candle "
                f"{nearest_candle_time} by {time_diff:.1f} minutes "
                f"(tolerance: {tolerance_minutes} minutes)"
            )
        
        return {
            "is_valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
            "exit_time": exit_time.isoformat(),
            "nearest_candle_time": nearest_candle_time.isoformat(),
            "time_difference_minutes": time_diff
        }
    
    def _validate_stop_loss(
        self,
        trade: Dict[str, Any],
        df: pd.DataFrame,
        entry_time: pd.Timestamp,
        exit_time: pd.Timestamp
    ) -> Dict[str, Any]:
        """Validate stop loss value matches actual price hit."""
        errors = []
        warnings = []
        
        stop_loss = trade.get("stop")
        if stop_loss is None:
            return {
                "is_valid": False,
                "errors": ["Stop loss value is None"]
            }
        
        # Get candles between entry and exit
        mask = (df.index >= entry_time) & (df.index <= exit_time)
        trade_candles = df[mask]
        
        if trade_candles.empty:
            return {
                "is_valid": False,
                "errors": ["No candles found between entry and exit times"]
            }
        
        direction = trade.get("direction")
        result = trade.get("result")
        
        # For break-even, stop loss should equal entry price
        if result == "BE":
            entry_price = trade.get("entry")
            if abs(stop_loss - entry_price) > 0.01:  # Allow small floating point difference
                errors.append(
                    f"Break-even exit: stop loss {stop_loss} should equal entry price {entry_price}"
                )
        
        # Check if stop loss was actually hit in the candles
        if direction == "long":
            min_low = trade_candles['low'].min()
            if min_low > stop_loss:
                warnings.append(
                    f"Stop loss {stop_loss} was not hit (minimum low: {min_low})"
                )
        else:  # short
            max_high = trade_candles['high'].max()
            if max_high < stop_loss:
                warnings.append(
                    f"Stop loss {stop_loss} was not hit (maximum high: {max_high})"
                )
        
        return {
            "is_valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings
        }
    
    def _validate_entry_price(
        self,
        trade: Dict[str, Any],
        df: pd.DataFrame,
        entry_time: pd.Timestamp
    ) -> Dict[str, Any]:
        """Validate entry price matches market data."""
        errors = []
        
        entry_price = trade.get("entry")
        if entry_price is None:
            return {
                "is_valid": False,
                "errors": ["Entry price is None"]
            }
        
        # Find candle at entry time
        nearest_idx = df.index.get_indexer([entry_time], method='nearest')[0]
        if nearest_idx < 0 or nearest_idx >= len(df):
            return {
                "is_valid": False,
                "errors": ["Could not find candle at entry time"]
            }
        
        candle = df.iloc[nearest_idx]
        candle_time = df.index[nearest_idx]
        
        # Entry price should be within candle range
        if not (candle['low'] <= entry_price <= candle['high']):
            errors.append(
                f"Entry price {entry_price} is outside candle range "
                f"[{candle['low']}, {candle['high']}] at {candle_time}"
            )
        
        return {
            "is_valid": len(errors) == 0,
            "errors": errors,
            "candle_time": candle_time.isoformat(),
            "candle_low": float(candle['low']),
            "candle_high": float(candle['high']),
            "entry_price": entry_price
        }
    
    def _validate_exit_price(
        self,
        trade: Dict[str, Any],
        df: pd.DataFrame,
        exit_time: pd.Timestamp
    ) -> Dict[str, Any]:
        """Validate exit price matches market data."""
        errors = []
        
        exit_price = trade.get("exit_price")
        if exit_price is None:
            return {
                "is_valid": False,
                "errors": ["Exit price is None"]
            }
        
        result = trade.get("result")
        
        # Find candle at exit time
        nearest_idx = df.index.get_indexer([exit_time], method='nearest')[0]
        if nearest_idx < 0 or nearest_idx >= len(df):
            return {
                "is_valid": False,
                "errors": ["Could not find candle at exit time"]
            }
        
        candle = df.iloc[nearest_idx]
        candle_time = df.index[nearest_idx]
        
        # Exit price should be within candle range (or equal to SL/TP)
        if not (candle['low'] <= exit_price <= candle['high']):
            # Allow exact match for TP/SL
            target = trade.get("target")
            stop = trade.get("stop")
            if exit_price not in [target, stop]:
                errors.append(
                    f"Exit price {exit_price} is outside candle range "
                    f"[{candle['low']}, {candle['high']}] at {candle_time} "
                    f"(result: {result})"
                )
        
        return {
            "is_valid": len(errors) == 0,
            "errors": errors,
            "candle_time": candle_time.isoformat(),
            "candle_low": float(candle['low']),
            "candle_high": float(candle['high']),
            "exit_price": exit_price,
            "result": result
        }


async def main():
    """Main entry point for validation script."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Validate backtest trade timestamps against market data"
    )
    parser.add_argument(
        "backtest_id",
        help="Backtest ID to validate"
    )
    parser.add_argument(
        "--asset",
        default="NIFTY",
        help="Asset symbol (default: NIFTY)"
    )
    parser.add_argument(
        "--tolerance",
        type=int,
        default=5,
        help="Timestamp tolerance in minutes (default: 5)"
    )
    parser.add_argument(
        "--output",
        help="Output file path for JSON results (optional)"
    )
    
    args = parser.parse_args()
    
    validator = BacktestTimestampValidator(asset=args.asset)
    
    try:
        results = await validator.validate_backtest(
            args.backtest_id,
            tolerance_minutes=args.tolerance
        )
        
        # Print summary
        print(f"\nValidation Summary:")
        print(f"  Backtest ID: {results['backtest_id']}")
        print(f"  Total Trades: {results['total_trades']}")
        print(f"  Valid Trades: {results['valid_trades']}")
        print(f"  Invalid Trades: {results['invalid_trades']}")
        print(f"  Validation Rate: {results['validation_rate']*100:.1f}%")
        
        # Print invalid trades
        invalid_trades = [r for r in results['results'] if not r['is_valid']]
        if invalid_trades:
            print(f"\nInvalid Trades ({len(invalid_trades)}):")
            for trade in invalid_trades:
                print(f"  Trade {trade['trade_index']}:")
                for error in trade.get('errors', []):
                    print(f"    ERROR: {error}")
                for warning in trade.get('warnings', []):
                    print(f"    WARNING: {warning}")
        
        # Save to file if requested
        if args.output:
            with open(args.output, 'w') as f:
                json.dump(results, f, indent=2, default=str)
            print(f"\nResults saved to: {args.output}")
        
        # Exit with error code if validation failed
        if results['invalid_trades'] > 0:
            sys.exit(1)
        
    except Exception as e:
        logger.error(f"Validation failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())


