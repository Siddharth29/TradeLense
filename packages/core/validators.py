"""Pure validation functions for trade analysis, reused by judge and runner.

These functions provide deterministic checks outside of LLM processing
for consistent trade validation across the system.
"""

from typing import List, Dict, Any
import statistics


def median_range_5m(c5_window: List[Dict[str, Any]]) -> float:
    """Calculate median range (high - low) for 5-minute candles.
    
    Args:
        c5_window: List of 5-minute candle dictionaries with 'high' and 'low' keys
        
    Returns:
        Median range value across the candle window
        
    Raises:
        ValueError: If window is empty or candles missing required fields
    """
    if not c5_window:
        raise ValueError("c5_window cannot be empty")
    
    ranges = []
    for candle in c5_window:
        if 'high' not in candle or 'low' not in candle:
            raise ValueError("Candle missing required 'high' or 'low' field")
        
        high = float(candle['high'])
        low = float(candle['low'])
        ranges.append(high - low)
    
    return statistics.median(ranges)


def rr(entry: float, stop: float, target: float, side: str) -> float:
    """Calculate risk-reward ratio for a trade.
    
    Args:
        entry: Entry price
        stop: Stop loss price
        target: Target price
        side: Trade direction, either "buy", "sell", or "flat"
        
    Returns:
        Risk-reward ratio (positive value), or 0.0 for flat signals
        
    Raises:
        ValueError: If side is invalid or trade setup is invalid
    """
    if side not in {"buy", "sell", "flat"}:
        raise ValueError(f"Invalid side '{side}'. Must be 'buy', 'sell', or 'flat'")
    
    # Flat signals don't need risk-reward calculation
    if side == "flat":
        return 0.0
    
    entry = float(entry)
    stop = float(stop)
    target = float(target)
    
    if side == "buy":
        if stop >= entry:
            raise ValueError("For buy trades, stop must be below entry")
        if target <= entry:
            raise ValueError("For buy trades, target must be above entry")
        
        risk = entry - stop
        reward = target - entry
    else:  # sell
        if stop <= entry:
            raise ValueError("For sell trades, stop must be above entry")
        if target >= entry:
            raise ValueError("For sell trades, target must be below entry")
        
        risk = stop - entry
        reward = entry - target
    
    if risk <= 0:
        raise ValueError("Risk must be positive")
    
    return reward / risk


def inside_window(
    entry: float, 
    stop: float, 
    target: float, 
    c5_window: List[Dict[str, Any]], 
    c15_window: List[Dict[str, Any]]
) -> bool:
    """Check if trade levels (entry, stop, target) are within recent price action.
    
    Args:
        entry: Entry price
        stop: Stop loss price  
        target: Target price
        c5_window: List of 5-minute candles
        c15_window: List of 15-minute candles
        
    Returns:
        True if all levels are within the combined price range of both windows
        
    Raises:
        ValueError: If windows are empty or missing required fields
    """
    if not c5_window or not c15_window:
        raise ValueError("Both c5_window and c15_window must be non-empty")
    
    # Collect all highs and lows from both timeframes
    all_highs = []
    all_lows = []
    
    for candle in c5_window + c15_window:
        if 'high' not in candle or 'low' not in candle:
            raise ValueError("Candle missing required 'high' or 'low' field")
        
        all_highs.append(float(candle['high']))
        all_lows.append(float(candle['low']))
    
    window_high = max(all_highs)
    window_low = min(all_lows)
    
    # Check if all trade levels are within the price window
    levels = [float(entry), float(stop), float(target)]
    return all(window_low <= level <= window_high for level in levels)


def anti_scalp(
    entry: float, 
    target: float, 
    side: str, 
    c5_window: List[Dict[str, Any]], 
    k: float = 6.0
) -> bool:
    """Check if trade target meets minimum distance threshold to avoid scalping.
    
    Uses median range multiplied by factor k to ensure adequate profit potential
    relative to recent market volatility.
    
    Args:
        entry: Entry price
        target: Target price
        side: Trade direction, either "buy", "sell", or "flat"
        c5_window: List of 5-minute candles for volatility calculation
        k: Multiplier factor for median range (default 6.0)
        
    Returns:
        True if target distance exceeds k * median_range, or True for flat signals
        
    Raises:
        ValueError: If side is invalid or window is empty
    """
    if side not in {"buy", "sell", "flat"}:
        raise ValueError(f"Invalid side '{side}'. Must be 'buy', 'sell', or 'flat'")
    
    # Flat signals don't need anti-scalp validation
    if side == "flat":
        return True
    
    if not c5_window:
        raise ValueError("c5_window cannot be empty")
    
    entry = float(entry)
    target = float(target)
    k = float(k)
    
    # Calculate minimum distance threshold
    median_range = median_range_5m(c5_window)
    min_distance = k * median_range
    
    # Calculate actual distance based on trade direction
    if side == "buy":
        if target <= entry:
            return False  # Invalid buy trade
        actual_distance = target - entry
    else:  # sell
        if target >= entry:
            return False  # Invalid sell trade
        actual_distance = entry - target
    
    return actual_distance >= min_distance


def validate_expert_proposal(
    proposal: Dict[str, Any],
    c5_candles: List[Dict[str, Any]],
    min_rr: float = 2.0,
    anti_scalp_k: float = 1.5
) -> Dict[str, Any]:
    """Validate expert proposal with ONLY anti-scalp and RR checks.
    
    Called AFTER receiving expert API response to validate each proposal.
    Performs exactly TWO validations:
    1. Risk/Reward (RR) ratio >= min_rr
    2. Anti-scalp check: target distance >= k * median_range
    
    Args:
        proposal: Dict with keys: signal, entry, stop, target, ttl_minutes, 
                  confidence, active_window_c5, active_window_c15, rationale
        c5_candles: List of 5-minute candles for anti-scalp calculation
        min_rr: Minimum required risk/reward ratio (default 2.0)
        anti_scalp_k: Multiplier for median range in anti-scalp check (default 1.5)
        
    Returns:
        {
            "valid": bool,           # True if BOTH checks pass
            "rr": float,             # Risk/reward ratio value
            "rr_pass": bool,         # True if RR >= min_rr
            "anti_scalp": float,     # Actual target distance
            "anti_scalp_min": float, # Minimum required distance
            "anti_scalp_pass": bool, # True if distance >= minimum
            "errors": List[str]      # Error messages if validation fails
        }
    
    Example:
        proposal = {
            "signal": "buy",
            "entry": 121820,
            "stop": 121110,
            "target": 122260,
            "ttl_minutes": 90,
            "confidence": 0.72,
            "active_window_c5": 60,
            "active_window_c15": 30,
            "rationale": "Bullish setup"
        }
        result = validate_expert_proposal(proposal, c5_candles)
        if result["valid"]:
            print("Proposal passed validation")
    """
    signal = proposal.get("signal", "").lower()
    entry = proposal.get("entry")
    stop = proposal.get("stop")
    target = proposal.get("target")
    
    # Flat signals pass automatically
    if signal == "flat":
        return {
            "valid": True,
            "rr": 0.0,
            "rr_pass": True,
            "anti_scalp": 0.0,
            "anti_scalp_min": 0.0,
            "anti_scalp_pass": True,
            "errors": []
        }
    
    # Check required fields
    if not all([signal, entry is not None, stop is not None, target is not None]):
        return {
            "valid": False,
            "rr": None,
            "rr_pass": False,
            "anti_scalp": None,
            "anti_scalp_min": None,
            "anti_scalp_pass": False,
            "errors": ["Missing required fields: signal, entry, stop, target"]
        }
    
    errors = []
    
    # Convert to float
    try:
        entry = float(entry)
        stop = float(stop)
        target = float(target)
    except (ValueError, TypeError) as e:
        return {
            "valid": False,
            "rr": None,
            "rr_pass": False,
            "anti_scalp": None,
            "anti_scalp_min": None,
            "anti_scalp_pass": False,
            "errors": [f"Invalid numeric values: {e}"]
        }
    
    # ========================================================================
    # VALIDATION 1: RISK/REWARD RATIO
    # ========================================================================
    try:
        rr_value = rr(entry, stop, target, signal)
        # Use tolerance for floating point comparison to handle precision issues
        # especially important for Forex pairs with small decimal values
        EPSILON = 1e-6
        rr_pass = (rr_value >= min_rr - EPSILON)
        if not rr_pass:
            errors.append(f"RR {rr_value:.2f} < {min_rr}")
    except ValueError as e:
        return {
            "valid": False,
            "rr": None,
            "rr_pass": False,
            "anti_scalp": None,
            "anti_scalp_min": None,
            "anti_scalp_pass": False,
            "errors": [f"RR calculation failed: {e}"]
        }
    
    # ========================================================================
    # VALIDATION 2: ANTI-SCALP
    # ========================================================================
    try:
        median_range_val = median_range_5m(c5_candles)
        min_distance = anti_scalp_k * median_range_val
        
        # Calculate actual distance
        if signal == "buy":
            actual_distance = target - entry
        else:  # sell
            actual_distance = entry - target
        
        # Use tolerance for floating point comparison
        EPSILON = 1e-6
        anti_scalp_pass = actual_distance >= (min_distance - EPSILON)
        if not anti_scalp_pass:
            errors.append(
                f"Anti-scalp failed: {actual_distance:.2f} < {min_distance:.2f} "
                f"(median_range={median_range_val:.2f} * k={anti_scalp_k})"
            )
    except ValueError as e:
        return {
            "valid": False,
            "rr": rr_value,
            "rr_pass": rr_pass,
            "anti_scalp": None,
            "anti_scalp_min": None,
            "anti_scalp_pass": False,
            "errors": [f"Anti-scalp calculation failed: {e}"]
        }
    
    # Both checks must pass
    valid = rr_pass and anti_scalp_pass
    
    return {
        "valid": valid,
        "rr": round(rr_value, 2),
        "rr_pass": rr_pass,
        "anti_scalp": round(actual_distance, 2),
        "anti_scalp_min": round(min_distance, 2),
        "anti_scalp_pass": anti_scalp_pass,
        "errors": errors
    }


__all__ = ["median_range_5m", "rr", "inside_window", "anti_scalp", "validate_expert_proposal"]

