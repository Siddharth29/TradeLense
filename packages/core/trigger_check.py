import pandas as pd
import numpy as np
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple

from .assets import Asset
from .logger import get_logger

logger = get_logger(__name__)


# Asset-specific buffer configurations
ASSET_BUFFER_CONFIG = {
    # MT5 assets (pip-based)
    Asset.XAUUSD.value: {
        "base_buffer": 2.5,    # Gold: ~$2.5 buffer
        "min_buffer": 1.0,
        "max_buffer": 8.0,
        "tick_size": 0.01,
        "volatility_factor": 1.8,
        "session_multipliers": {
            "london": 1.5,     # London session
            "ny": 1.8,         # NY session
            "asian": 1.2,      # Asian session
            "overlap": 2.0,    # Session overlaps
        }
    },
    Asset.BTCUSD.value: {
        "base_buffer": 50.0,   # Bitcoin: ~$50 buffer
        "min_buffer": 20.0,
        "max_buffer": 200.0,
        "tick_size": 0.01,
        "volatility_factor": 2.0,
        "session_multipliers": {
            "london": 1.3,
            "ny": 1.6,
            "asian": 1.1,
            "overlap": 1.8,
        }
    },
    Asset.GBPUSD.value: {
        "base_buffer": 0.0015, # GBP/USD: ~15 pips
        "min_buffer": 0.0005,
        "max_buffer": 0.005,
        "tick_size": 0.00001,
        "volatility_factor": 1.4,
        "session_multipliers": {
            "london": 1.6,
            "ny": 1.4,
            "asian": 1.1,
            "overlap": 1.9,
        }
    },
    Asset.EURUSD.value: {
        "base_buffer": 0.0012, # EUR/USD: ~12 pips
        "min_buffer": 0.0004,
        "max_buffer": 0.004,
        "tick_size": 0.00001,
        "volatility_factor": 1.3,
        "session_multipliers": {
            "london": 1.5,
            "ny": 1.4,
            "asian": 1.1,
            "overlap": 1.8,
        }
    },
    Asset.ETHUSD.value: {
        "base_buffer": 3.0,    # Ethereum: ~$3 buffer
        "min_buffer": 1.0,
        "max_buffer": 12.0,
        "tick_size": 0.01,
        "volatility_factor": 1.9,
        "session_multipliers": {
            "london": 1.4,
            "ny": 1.7,
            "asian": 1.2,
            "overlap": 1.8,
        }
    },
}

# Pattern-specific thresholds configuration
PATTERN_THRESHOLDS = {
    # Default thresholds
    "default": {
        "min_touch_pct": 0.3,      # Minimum touch percentage for retest
        "bos_span": 2,             # Swing detection span for BOS
        "min_range_bars": 5,       # Minimum bars for range detection
        "fvg_tolerance": 0.0,      # FVG gap tolerance
        "liquidity_touch_count": 2, # Minimum touches for liquidity level
    },
    # Asset-specific overrides
    Asset.XAUUSD.value: {
        "min_touch_pct": 0.2,
        "bos_span": 2,
        "min_range_bars": 3,
        "fvg_tolerance": 0.1,
        "liquidity_touch_count": 2,
    },
    Asset.BTCUSD.value: {
        "min_touch_pct": 0.15,
        "bos_span": 2,
        "min_range_bars": 3,
        "fvg_tolerance": 5.0,
        "liquidity_touch_count": 2,
    },
}


def get_asset_config(asset: str) -> Dict[str, Any]:
    """Get buffer configuration for an asset."""
    asset_upper = asset.upper()
    
    # Direct match first
    if asset_upper in ASSET_BUFFER_CONFIG:
        return ASSET_BUFFER_CONFIG[asset_upper]
    
    # Try common variations and mappings
    asset_mappings = {
        # NSE index variations
        'NIFTY50': 'NIFTY',
        'NIFTY_50': 'NIFTY',
        'NIFTY-50': 'NIFTY',
        'NSE:NIFTY50': 'NIFTY',
        'NIFTY 50': 'NIFTY',
        
        # Bank Nifty variations  
        'BANKNIFTY': 'BANKNIFTY',
        'BANK_NIFTY': 'BANKNIFTY',
        'BANK-NIFTY': 'BANKNIFTY',
        'NSE:BANKNIFTY': 'BANKNIFTY',
        'BANK NIFTY': 'BANKNIFTY',
        
        # MT5 variations
        'GOLD': 'XAUUSD',
        'XAU/USD': 'XAUUSD',
        'XAU-USD': 'XAUUSD',
        'BITCOIN': 'BTCUSD',
        'BTC/USD': 'BTCUSD',
        'BTC-USD': 'BTCUSD',
        'ETHEREUM': 'ETHUSD',
        'ETH/USD': 'ETHUSD',
        'ETH-USD': 'ETHUSD',
        'GBP/USD': 'GBPUSD',
        'GBP-USD': 'GBPUSD',
    }
    
    # Check if it's a known variation
    if asset_upper in asset_mappings:
        mapped_asset = asset_mappings[asset_upper]
        if mapped_asset in ASSET_BUFFER_CONFIG:
            return ASSET_BUFFER_CONFIG[mapped_asset]
    
    # Check for partial matches (for complex symbols)
    for key in ASSET_BUFFER_CONFIG.keys():
        if key in asset_upper or asset_upper in key:
            return ASSET_BUFFER_CONFIG[key]
    
    # CRITICAL: No fallbacks in production - fail if asset not configured
    logger.error(f"CRITICAL: No buffer configuration found for asset: {asset} - CANNOT PROCEED WITHOUT PROPER CONFIGURATION")
    raise ValueError(f"Asset {asset} not configured - production system cannot use fallback configurations")


def get_pattern_thresholds(asset: str) -> Dict[str, Any]:
    """Get pattern-specific thresholds for an asset, falling back to ``default``.

    ``PATTERN_THRESHOLDS`` ships an explicit ``"default"`` row, but it was
    unreachable: this raised for any asset not literally keyed in the table —
    which is GBPUSD, EURUSD, XAGUSD, USTEC and ETHUSD, i.e. five of the six
    traded instruments. Every caller (``_chk_bos_structure``, the FVG check, and
    therefore ``validate_pattern_with_history`` /
    ``enhanced_entry_trigger_met``) blew up with a ValueError on those assets
    instead of using the fallback that was sitting right there.

    Suffix-tolerant in the same way as the rest of the codebase, so a broker's
    demo ``XAUUSDm`` resolves to ``XAUUSD`` rather than silently dropping to the
    default row.
    """
    key = (asset or "").strip().upper()
    if key in PATTERN_THRESHOLDS:
        return PATTERN_THRESHOLDS[key]
    if key.endswith("M") and len(key) > 4 and key[:-1] in PATTERN_THRESHOLDS:
        return PATTERN_THRESHOLDS[key[:-1]]
    return PATTERN_THRESHOLDS["default"]


def get_market_session(asset: str, current_time: datetime) -> str:
    """Determine current market session for buffer adjustments."""
    
    # MT5 assets — classify by UTC session (London / NY / Asian / overlap).
    utc_hour = current_time.hour

    # London: 8:00-17:00 UTC; NY: 13:00-22:00 UTC; Asian: 0:00-9:00 UTC;
    # London-NY overlap: 13:00-17:00 UTC.
    if 13 <= utc_hour < 17:      # London-NY overlap
        return "overlap"
    elif 8 <= utc_hour < 17:     # London session
        return "london"
    elif 13 <= utc_hour < 22:    # NY session
        return "ny"
    else:                        # Asian session
        return "asian"


def _snap_to_tick(x: float, tick: float | None) -> float:
    """Snap value to nearest tick size."""
    if not tick or tick <= 0: 
        return x
    return round(x / tick) * tick


def _validate_future(df: pd.DataFrame) -> bool:
    """Validate future candle data for proper structure and no NaNs."""
    if df is None or len(df) == 0: 
        logger.debug("Future data validation failed: empty or None dataframe")
        return False
    
    req = {"open","high","low","close"}
    if not req.issubset(df.columns): 
        logger.debug(f"Future data validation failed: missing required columns {req - set(df.columns)}")
        return False
    
    if df[["open","high","low","close"]].isna().any().any(): 
        logger.debug("Future data validation failed: NaN values found in OHLC data")
        return False
    
    # assume df is oldest->newest; if not, sort by time column if present
    return True


def calculate_dynamic_buffer(
    asset: str,
    level: float,
    current_price: float,
    historical_data: pd.DataFrame,
    market_volatility: float,
) -> float:
    """Calculate ATR-aware dynamic buffer with tick snapping."""
    
    logger.info(f"=== calculate_dynamic_buffer START ===")
    logger.info(f"Input params: asset={asset}, level={level}, current_price={current_price}, market_volatility={market_volatility}")
    logger.info(f"historical_data: {len(historical_data) if historical_data is not None else 'None'} rows")
    
    try:
        cfg = get_asset_config(asset)
        logger.info(f"Asset config loaded: {cfg}")
        
        base = cfg["base_buffer"]
        logger.info(f"Base buffer: {base}")
        
        if "tick_size" not in cfg:
            logger.error(f"No tick_size found in asset configuration for: {asset}")
            raise ValueError(f"No tick_size found in asset configuration for: {asset}")
        tick = cfg["tick_size"]
        logger.info(f"Tick size: {tick}")

        # Volatility multiplier - market_volatility is now required
        vol_mult = 1.0 + (market_volatility * cfg["volatility_factor"])
        logger.info(f"Volatility calculation: market_volatility={market_volatility}, volatility_factor={cfg['volatility_factor']}, vol_mult={vol_mult}")

        # Session multiplier
        current_time = datetime.now(timezone.utc)
        logger.info(f"Current time: {current_time}")
        
        sess = get_market_session(asset, current_time)
        logger.info(f"Market session: {sess}")
        
        if sess not in cfg["session_multipliers"]:
            logger.error(f"No session multiplier found for session: {sess} in asset: {asset}")
            logger.error(f"Available sessions: {list(cfg['session_multipliers'].keys())}")
            raise ValueError(f"No session multiplier found for session: {sess} in asset: {asset}")
        sess_mult = cfg["session_multipliers"][sess]
        logger.info(f"Session multiplier: {sess_mult}")

        buf = base * vol_mult * sess_mult
        logger.info(f"Buffer before min/max: {buf}")
        
        buf = max(cfg["min_buffer"], min(cfg["max_buffer"], buf))
        logger.info(f"Buffer after min/max: {buf} (min={cfg['min_buffer']}, max={cfg['max_buffer']})")
        
        buf = _snap_to_tick(buf, tick)
        logger.info(f"Buffer after tick snapping: {buf}")
        
        logger.info(
            f"Dynamic buffer for {asset}: base={base:.3f}, "
            f"vol_mult={vol_mult:.2f}, session={sess_mult:.2f}, "
            f"final={buf:.3f}, tick={tick}"
        )
        
        logger.info(f"=== calculate_dynamic_buffer END: returning {buf} ===")
        return buf
        
    except Exception as e:
        logger.error(f"Error in calculate_dynamic_buffer: {e}")
        logger.error(f"Error type: {type(e).__name__}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise


def _chk_retest(level: float, fut: pd.DataFrame, buffer: float, asset: str) -> bool:
    """Enhanced retest check with dynamic buffer and touch confirmation."""
    if len(fut) == 0:
        return False
    
    # CRITICAL: No fallbacks in production - fail if buffer is invalid
    if buffer == 0.0:
        logger.error(f"CRITICAL: Invalid buffer (0.0) for retest check on {asset} - CANNOT PROCEED WITH INVALID DATA")
        return False
    
    for _, row in fut.iterrows():
        # Check if candle touches the level within buffer
        if row['low'] <= level + buffer and row['high'] >= level - buffer:
            # Calculate how much of the candle touches the level
            candle_range = row['high'] - row['low']
            if candle_range > 0:
                touch_range = min(row['high'], level + buffer) - max(row['low'], level - buffer)
                touch_percentage = touch_range / candle_range
                
                # Require at least 30% of the candle to touch the level
                if touch_percentage >= 0.3:
                    logger.info(f"Retest confirmed at {level} with buffer {buffer:.3f} and {touch_percentage:.2%} touch")
                    return True
    
    return False


def _chk_breakout(level: float, direction: str, fut: pd.DataFrame, buffer: float, asset: str) -> bool:
    """Enhanced breakout check with dynamic buffer and confirmation."""
    if len(fut) == 0:
        return False
    
    first = fut.iloc[0]
    
    # CRITICAL: No fallbacks in production - fail if buffer is invalid
    if buffer == 0.0:
        logger.error(f"CRITICAL: Invalid buffer (0.0) for breakout check on {asset} - CANNOT PROCEED WITH INVALID DATA")
        return False
    
    # Enhanced breakout logic with confirmation
    if direction == "long":
        # Long breakout: close above level - buffer AND high breaks level
        if first.close > level - buffer and first.high > level:
            logger.info(f"Long breakout confirmed at {level} with buffer {buffer:.3f}")
            return True
    else:
        # Short breakout: close below level + buffer AND low breaks level
        if first.close < level + buffer and first.low < level:
            logger.info(f"Short breakout confirmed at {level} with buffer {buffer:.3f}")
            return True
    
    return False


def _find_fvgs(df: pd.DataFrame, tol: float) -> list[tuple[int,str,float,float]]:
    """Find Fair Value Gaps in the dataframe."""
    out = []
    for i in range(2, len(df)):
        # bull: low[i] > high[i-2] + tol
        if df.iloc[i]["low"] > df.iloc[i-2]["high"] + tol:
            out.append((i, "bull", df.iloc[i-2]["high"], df.iloc[i]["low"]))
        # bear: high[i] < low[i-2] - tol
        elif df.iloc[i]["high"] < df.iloc[i-2]["low"] - tol:
            out.append((i, "bear", df.iloc[i]["high"], df.iloc[i-2]["low"]))
    return out


def _chk_fvg_fill(level: tuple, fut: pd.DataFrame, buffer: float) -> bool:
    """Legacy FVG fill check - basic implementation."""
    lo, hi = level
    return ((fut['low'] <= hi + buffer) & (fut['high'] >= lo - buffer)).any()


def _chk_liq_sweep(level: float, direction: str, fut: pd.DataFrame, buffer: float, asset: str) -> bool:
    """Enhanced liquidity sweep check - must sweep through level then show reversal."""
    if len(fut) == 0: 
        return False
    
    if buffer == 0.0:
        logger.error(f"CRITICAL: Invalid buffer (0.0) for liquidity sweep check on {asset} - CANNOT PROCEED WITH INVALID DATA")
        return False

    for i, (_, row) in enumerate(fut.iterrows()):
        if direction == "long":
            if row.low <= level - buffer:  # swept below
                if row.close > row.open: 
                    logger.info(f"Long liquidity sweep confirmed at {level} with buffer {buffer:.3f}")
                    return True
                if i + 1 < len(fut) and fut.iloc[i+1]['close'] > fut.iloc[i+1]['open']: 
                    logger.info(f"Long liquidity sweep confirmed at {level} with buffer {buffer:.3f}")
                    return True
        else:
            if row.high >= level + buffer: # swept above
                if row.close < row.open: 
                    logger.info(f"Short liquidity sweep confirmed at {level} with buffer {buffer:.3f}")
                    return True
                if i + 1 < len(fut) and fut.iloc[i+1]['close'] < fut.iloc[i+1]['open']: 
                    logger.info(f"Short liquidity sweep confirmed at {level} with buffer {buffer:.3f}")
                    return True
    return False


def _chk_range_retest(level: float, direction: str, fut: pd.DataFrame, buffer: float, asset: str) -> bool:
    """Range breakout → later retest (no same-bar pass)."""
    if len(fut) == 0: 
        return False
    
    if buffer == 0.0:
        logger.error(f"CRITICAL: Invalid buffer (0.0) for range retest check on {asset} - CANNOT PROCEED WITH INVALID DATA")
        return False

    broke = False
    for _, row in fut.iterrows():
        if not broke:
            if direction == "long":  
                broke = row.high >= level + buffer
            else:                    
                broke = row.low  <= level - buffer
            continue
        # only after break:
        if direction == "long" and row.low  <= level - buffer: 
            logger.info(f"Range retest confirmed at {level} with buffer {buffer:.3f}")
            return True
        if direction == "short" and row.high >= level + buffer: 
            logger.info(f"Range retest confirmed at {level} with buffer {buffer:.3f}")
            return True
    return False


def _chk_compression_breakout(level: float, direction: str, fut: pd.DataFrame, buffer: float, asset: str) -> bool:
    """
    Compression breakout check - enhanced version for compression patterns.
    
    A compression breakout requires:
    1. Price to have been in a tight range (compression) recently
    2. Current candle to break out of the compression with momentum
    3. Confirmation that the breakout is sustained
    """
    if len(fut) == 0:
        return False
    
    # CRITICAL: No fallbacks in production - fail if buffer is invalid
    if buffer == 0.0:
        logger.error(f"CRITICAL: Invalid buffer (0.0) for compression breakout check on {asset} - CANNOT PROCEED WITH INVALID DATA")
        return False
    
    # Enhanced breakout logic with compression-specific confirmation
    if direction == "long":
        # Long compression breakout: close above level - buffer AND high breaks level with momentum
        first = fut.iloc[0]
        if first.close > level - buffer and first.high > level:
            # Additional confirmation: check for bullish momentum (close > open)
            if first.close > first.open:
                logger.info(f"Long compression breakout confirmed at {level} with buffer {buffer:.3f}")
                return True
            # Or check if next candle confirms the breakout
            elif len(fut) > 1 and fut.iloc[1]['close'] > level - buffer:
                logger.info(f"Long compression breakout confirmed at {level} with buffer {buffer:.3f} (next candle confirmation)")
                return True
    else:
        # Short compression breakout: close below level + buffer AND low breaks level with momentum
        first = fut.iloc[0]
        if first.close < level + buffer and first.low < level:
            # Additional confirmation: check for bearish momentum (close < open)
            if first.close < first.open:
                logger.info(f"Short compression breakout confirmed at {level} with buffer {buffer:.3f}")
                return True
            # Or check if next candle confirms the breakout
            elif len(fut) > 1 and fut.iloc[1]['close'] < level + buffer:
                logger.info(f"Short compression breakout confirmed at {level} with buffer {buffer:.3f} (next candle confirmation)")
                return True
    
    return False




def _is_swing_high(df: pd.DataFrame, i: int, span: int) -> bool:
    """Check if candle at index i is a swing high."""
    if i - span < 0 or i + span >= len(df): 
        return False
    h = df.iloc[i]["high"]
    return all(df.iloc[j]["high"] < h for j in range(i - span, i + span + 1) if j != i)


def _is_swing_low(df: pd.DataFrame, i: int, span: int) -> bool:
    """Check if candle at index i is a swing low."""
    if i - span < 0 or i + span >= len(df): 
        return False
    l = df.iloc[i]["low"]
    return all(df.iloc[j]["low"] > l for j in range(i - span, i + span + 1) if j != i)


def _last_swing_index(df: pd.DataFrame, span: int, kind: str) -> Optional[int]:
    """Find the last swing high or low in the dataframe."""
    for i in range(len(df)-span-1, span-1, -1):
        if kind == "high" and _is_swing_high(df, i, span): 
            return i
        if kind == "low"  and _is_swing_low(df,  i, span): 
            return i
    return None


def _chk_bos_structure(level: float, direction: str, fut: pd.DataFrame, buffer: float, asset: str) -> bool:
    """BOS via swing structure - break the last opposite swing extreme and hold."""
    thresholds = get_pattern_thresholds(asset)
    span = thresholds["bos_span"]
    
    if len(fut) < span*2+2: 
        return False
    
    if buffer == 0.0:
        logger.error(f"CRITICAL: Invalid buffer (0.0) for BOS structure check on {asset} - CANNOT PROCEED WITH INVALID DATA")
        return False

    if direction == "long":
        sidx = _last_swing_index(fut, span, "high")
        if sidx is None:
            raise ValueError(f"No swing high found in future data for BOS structure check")
        swing_high = fut.iloc[sidx]["high"]
        
        # Find the bar that breaks the swing high
        broke = False
        break_bar_idx = -1
        for i, (_, row) in enumerate(fut.iterrows()):
            if row.high >= swing_high + buffer:
                broke = True
                break_bar_idx = i
                break
        
        if not broke: 
            logger.debug(f"BOS long: no break of swing high {swing_high} at {level}")
            return False
        
        # Check if the next bar holds above swing high
        if break_bar_idx + 1 < len(fut):
            next_bar = fut.iloc[break_bar_idx + 1]
            result = next_bar["close"] >= swing_high - buffer
            if result:
                logger.info(f"BOS long confirmed: broke swing high {swing_high} at {level}")
            return result
        else:
            return False
    else:
        sidx = _last_swing_index(fut, span, "low")
        if sidx is None:
            raise ValueError(f"No swing low found in future data for BOS structure check")
        swing_low = fut.iloc[sidx]["low"]
        
        # Find the bar that breaks the swing low
        broke = False
        break_bar_idx = -1
        for i, (_, row) in enumerate(fut.iterrows()):
            if row.low <= swing_low - buffer:
                broke = True
                break_bar_idx = i
                break
        
        if not broke: 
            logger.debug(f"BOS short: no break of swing low {swing_low} at {level}")
            return False
        
        # Check if the next bar holds below swing low
        if break_bar_idx + 1 < len(fut):
            next_bar = fut.iloc[break_bar_idx + 1]
            result = next_bar["close"] <= swing_low + buffer
            if result:
                logger.info(f"BOS short confirmed: broke swing low {swing_low} at {level}")
            return result
        else:
            return False


def _chk_bos(level: float, direction: str, fut: pd.DataFrame, buffer: float, asset: str) -> bool:
    """Legacy BOS function - redirects to structure check."""
    return _chk_bos_structure(level, direction, fut, buffer, asset)


_CHECK_MAP = {
    'retest': _chk_retest,
    'orderblock': _chk_retest,
    'breakout': _chk_breakout,
    'compression breakout': _chk_compression_breakout,
    'fvg fill': _chk_fvg_fill,
    'liquidity sweep': _chk_liq_sweep,
    'range breakout retest': _chk_range_retest,
    'bos': _chk_bos,
}


# Deterministic routing by specificity - most specific patterns first
_CHECK_ORDER = [
    "range breakout retest",
    "compression breakout", 
    "liquidity sweep",
    "fvg fill",
    "orderblock",
    "retest",
    "bos",
    "breakout",
]


def entry_trigger_met(pred: dict, fut: pd.DataFrame, buffer: float, asset: str) -> bool:
    """Return ``True`` if the price action in ``fut`` satisfies the entry
    condition or pattern described by ``pred``.

    Enhanced with deterministic routing by specificity and ATR-aware buffers.
    Supports both Indian indices (points-based) and MT5 assets (pip-based).

    Parameters
    ----------
    pred : dict
        Prediction dictionary with condition, level, direction, etc.
    fut : pd.DataFrame
        Future candle data
    buffer : float, optional
        Buffer override (if 0.0, will calculate dynamic buffer)
    asset : str, optional
        Asset symbol for buffer calculation (default: "NIFTY")

    Returns
    -------
    bool
        True if trigger conditions are met with realistic market buffers
    """

    # Early validation
    if not _validate_future(fut):
        return False

    condition = pred.get("condition")
    pattern = pred.get("pattern")
    
    if not condition and not pattern:
        logger.error(f"No condition or pattern found in prediction")
        return False
    
    raw = (condition or pattern).lower().strip()
    if not raw:
        logger.error(f"Empty condition/pattern string in prediction")
        return False
    
    level = pred.get("level")
    direction = pred.get("direction")
    buf = pred.get("buffer", buffer)
    
    # CRITICAL: No fallbacks in production - fail if buffer is invalid
    if buf == 0.0:
        logger.error(f"CRITICAL: Invalid buffer (0.0) for trigger check on {asset} - CANNOT PROCEED WITH INVALID DATA")
        return False

    # Deterministic routing by specificity - check most specific patterns first
    # First check for exact matches
    if raw in _CHECK_ORDER:
        key = raw
    else:
        # Then check for substring matches in order of specificity
        key = None
        for k in _CHECK_ORDER:
            if k in raw:
                key = k
                break
        
        if key is None:
            logger.error(f"No matching pattern found for condition: {raw}")
            return False
    
    if key:
        if key == "range breakout retest":
            return _chk_range_retest(level, direction, fut, buf, asset) if (level is not None and direction) else False
        
        if key == "compression breakout":
            return _chk_compression_breakout(level, direction, fut, buf, asset) if (level is not None and direction) else False
        
        if key == "liquidity sweep":
            return _chk_liq_sweep(level, direction, fut, buf, asset) if (level is not None and direction) else False
        
        if key == "fvg fill":
            if level is None: 
                return False
            lv = tuple(level) if isinstance(level,(list,tuple)) else (level, level)
            return _chk_fvg_fill(lv, fut, buf)
        
        if key in ("retest","orderblock"):
            return _chk_retest(level, fut, buf, asset) if level is not None else False
        
        if key == "bos":
            return _chk_bos_structure(level, direction, fut, buf, asset) if (level is not None and direction) else False
        
        if key == "breakout":
            return _chk_breakout(level, direction, fut, buf, asset) if (level is not None and direction) else False

    # No fallback - explicit failure if no specific pattern matched
    logger.error(f"No specific pattern matched for condition: {raw}")
    return False


# Enhanced Pattern Validation Functions with Historical Context
def validate_pattern_with_history(
    pattern_type: str,
    level: float,
    direction: str,
    historical_candles: pd.DataFrame,
    current_candle: Dict[str, Any],
    asset: str,
    buffer: float
) -> bool:
    """
    Validate patterns using both historical context and current trigger.
    
    Parameters
    ----------
    pattern_type : str
        Type of pattern to validate (liquidity_sweep, range_breakout, etc.)
    level : float
        The level to check
    direction : str
        Direction of the trade (long/short)
    historical_candles : pd.DataFrame
        Historical candles for pattern context validation
    current_candle : Dict[str, Any]
        Current 5-minute candle data
    asset : str
        Asset symbol for buffer calculation
    buffer : float
        Buffer for level validation
        
    Returns
    -------
    bool
        True if pattern is valid with historical context
    """
    
    if historical_candles is None or len(historical_candles) == 0:
        logger.error(f"CRITICAL ERROR: No historical candles provided for pattern validation - CANNOT VALIDATE PATTERN")
        return False
    
    # CRITICAL: No fallbacks in production - fail if buffer is invalid
    if buffer == 0.0:
        logger.error(f"CRITICAL: Invalid buffer (0.0) for pattern validation on {asset} - CANNOT PROCEED WITH INVALID DATA")
        return False
    
    pattern_type_lower = pattern_type.lower()
    
    if 'liquidity' in pattern_type_lower and 'sweep' in pattern_type_lower:
        return validate_liquidity_sweep_with_history(level, direction, historical_candles, current_candle, buffer, asset)
    
    elif 'range' in pattern_type_lower and 'breakout' in pattern_type_lower:
        return validate_range_breakout_with_history(level, direction, historical_candles, current_candle, buffer, asset)
    
    elif 'breakout' in pattern_type_lower:
        return validate_breakout_with_history(level, direction, historical_candles, current_candle, buffer, asset)
    
    elif 'retest' in pattern_type_lower or 'orderblock' in pattern_type_lower:
        return validate_retest_with_history(level, direction, historical_candles, current_candle, buffer, asset)
    
    elif 'fvg' in pattern_type_lower:
        return validate_fvg_fill_with_history(level, direction, historical_candles, current_candle, buffer, asset)
    
    else:
        # Default to simple breakout validation
        return validate_breakout_with_history(level, direction, historical_candles, current_candle, buffer, asset)


def validate_liquidity_sweep_with_history(
    level: float,
    direction: str,
    historical_candles: pd.DataFrame,
    current_candle: Dict[str, Any],
    buffer: float,
    asset: str
) -> bool:
    """
    Validate liquidity sweep with proper historical context.
    
    A liquidity sweep requires:
    1. The level to have acted as support/resistance multiple times (liquidity pool)
    2. Current candle to sweep the liquidity (break the level)
    3. Reversal confirmation in the same or next candle
    """
    
    # Get recent candles for liquidity pool analysis (last 20-30 candles)
    if len(historical_candles) < 10:
        logger.error(f"CRITICAL ERROR: Insufficient historical candles for liquidity sweep validation - need at least 10, got {len(historical_candles)}")
        return False
    
    recent_candles = historical_candles.tail(30)
    
    if direction == "long":
        # For long liquidity sweep, level should have acted as support
        # Count how many times price touched the level from above (support touches)
        support_touches = 0
        for _, row in recent_candles.iterrows():
            # Check if candle's low touched the level (support)
            if row['low'] <= level + buffer and row['high'] >= level - buffer:
                support_touches += 1
        
        # Need at least 2-3 support touches to form a liquidity pool
        if support_touches < 2:
            logger.debug(f"Insufficient support touches for liquidity pool: {support_touches}")
            return False
        
        # Check if current candle sweeps the liquidity (breaks below support)
        current_low = current_candle.get('low', 0)
        current_high = current_candle.get('high', 0)
        current_close = current_candle.get('close', 0)
        current_open = current_candle.get('open', 0)
        
        # Sweep condition: low breaks below the level
        if current_low <= level - buffer:
            # Look for bullish reversal confirmation
            if current_close > current_open:  # Bullish candle
                logger.info(f"Long liquidity sweep confirmed at {level} with {support_touches} support touches")
                return True
            # Check if it's a hammer/doji pattern (close near high after sweep)
            elif current_close > (current_high + current_low) / 2:  # Close in upper half
                logger.info(f"Long liquidity sweep confirmed with reversal pattern at {level}")
                return True
    
    else:  # direction == "short"
        # For short liquidity sweep, level should have acted as resistance
        # Count how many times price touched the level from below (resistance touches)
        resistance_touches = 0
        for _, row in recent_candles.iterrows():
            # Check if candle's high touched the level (resistance)
            if row['high'] >= level - buffer and row['low'] <= level + buffer:
                resistance_touches += 1
        
        # Need at least 2-3 resistance touches to form a liquidity pool
        if resistance_touches < 2:
            logger.debug(f"Insufficient resistance touches for liquidity pool: {resistance_touches}")
            return False
        
        # Check if current candle sweeps the liquidity (breaks above resistance)
        current_low = current_candle.get('low', 0)
        current_high = current_candle.get('high', 0)
        current_close = current_candle.get('close', 0)
        current_open = current_candle.get('open', 0)
        
        # Sweep condition: high breaks above the level
        if current_high >= level + buffer:
            # Look for bearish reversal confirmation
            if current_close < current_open:  # Bearish candle
                logger.info(f"Short liquidity sweep confirmed at {level} with {resistance_touches} resistance touches")
                return True
            # Check if it's a shooting star/doji pattern (close near low after sweep)
            elif current_close < (current_high + current_low) / 2:  # Close in lower half
                logger.info(f"Short liquidity sweep confirmed with reversal pattern at {level}")
                return True
    
    return False


def validate_range_breakout_with_history(
    level: float,
    direction: str,
    historical_candles: pd.DataFrame,
    current_candle: Dict[str, Any],
    buffer: float,
    asset: str
) -> bool:
    """
    Validate range breakout with proper historical context.
    
    A range breakout requires:
    1. Price to have been contained within a range previously
    2. Current candle to break out of the range with volume/strength
    3. Confirmation that the breakout is sustained
    """
    
    # Get recent candles for range analysis (last 20-40 candles)
    if len(historical_candles) < 15:
        logger.error(f"CRITICAL ERROR: Insufficient historical candles for range breakout validation - need at least 15, got {len(historical_candles)}")
        return False
    
    recent_candles = historical_candles.tail(40)
    
    if direction == "long":
        # For long breakout, check if price was previously below the level
        # Count how many recent candles had highs below the level
        contained_candles = 0
        for _, row in recent_candles.iterrows():
            if row['high'] < level - buffer:
                contained_candles += 1
        
        # Need at least 10-15 candles to show range containment
        if contained_candles < 10:
            logger.debug(f"Insufficient range containment for breakout: {contained_candles}")
            return False
        
        # Check current candle breakout
        current_high = current_candle.get('high', 0)
        current_close = current_candle.get('close', 0)
        current_open = current_candle.get('open', 0)
        
        # Breakout condition: high breaks above level AND close confirms
        if current_high > level + buffer and current_close > level - buffer:
            # Additional strength check: close should be above open (bullish momentum)
            if current_close > current_open:
                logger.info(f"Long range breakout confirmed at {level} with {contained_candles} contained candles")
                return True
    
    else:  # direction == "short"
        # For short breakout, check if price was previously above the level
        # Count how many recent candles had lows above the level
        contained_candles = 0
        for _, row in recent_candles.iterrows():
            if row['low'] > level + buffer:
                contained_candles += 1
        
        # Need at least 10-15 candles to show range containment
        if contained_candles < 10:
            logger.debug(f"Insufficient range containment for breakout: {contained_candles}")
            return False
        
        # Check current candle breakout
        current_low = current_candle.get('low', 0)
        current_close = current_candle.get('close', 0)
        current_open = current_candle.get('open', 0)
        
        # Breakout condition: low breaks below level AND close confirms
        if current_low < level - buffer and current_close < level + buffer:
            # Additional strength check: close should be below open (bearish momentum)
            if current_close < current_open:
                logger.info(f"Short range breakout confirmed at {level} with {contained_candles} contained candles")
                return True
    
    return False


def validate_breakout_with_history(
    level: float,
    direction: str,
    historical_candles: pd.DataFrame,
    current_candle: Dict[str, Any],
    buffer: float,
    asset: str
) -> bool:
    """
    Validate general breakout with historical context.
    """
    
    # Get recent candles for trend analysis
    if len(historical_candles) < 10:
        logger.error(f"CRITICAL ERROR: Insufficient historical candles for breakout validation - need at least 10, got {len(historical_candles)}")
        return False
    
    recent_candles = historical_candles.tail(20)
    
    if direction == "long":
        # Check if recent candles show bullish momentum building
        bullish_candles = 0
        for _, row in recent_candles.iterrows():
            if row['close'] > row['open']:
                bullish_candles += 1
        
        # Current candle breakout
        current_high = current_candle.get('high', 0)
        current_close = current_candle.get('close', 0)
        
        if current_high > level + buffer and current_close > level - buffer:
            logger.info(f"Long breakout confirmed at {level} with {bullish_candles} bullish candles")
            return True
    
    else:  # direction == "short"
        # Check if recent candles show bearish momentum building
        bearish_candles = 0
        for _, row in recent_candles.iterrows():
            if row['close'] < row['open']:
                bearish_candles += 1
        
        # Current candle breakout
        current_low = current_candle.get('low', 0)
        current_close = current_candle.get('close', 0)
        
        if current_low < level - buffer and current_close < level + buffer:
            logger.info(f"Short breakout confirmed at {level} with {bearish_candles} bearish candles")
            return True
    
    return False


def validate_retest_with_history(
    level: float,
    direction: str,
    historical_candles: pd.DataFrame,
    current_candle: Dict[str, Any],
    buffer: float,
    asset: str
) -> bool:
    """
    Validate retest with historical context.
    """
    
    # Get recent candles to check if level was previously broken
    if len(historical_candles) < 10:
        logger.error(f"CRITICAL ERROR: Insufficient historical candles for retest validation - need at least 10, got {len(historical_candles)}")
        return False
    
    recent_candles = historical_candles.tail(20)
    
    if direction == "long":
        # Check if level was previously broken to the upside
        level_broken = False
        for _, row in recent_candles.iterrows():
            if row['high'] > level + buffer:
                level_broken = True
                break
        
        if not level_broken:
            logger.debug("Level not previously broken for retest validation")
            return False
        
        # Current candle retest
        current_low = current_candle.get('low', 0)
        current_high = current_candle.get('high', 0)
        current_close = current_candle.get('close', 0)
        
        # Retest condition: candle touches level and bounces
        if current_low <= level + buffer and current_high >= level - buffer:
            # Check for bounce (close above level)
            if current_close > level - buffer:
                logger.info(f"Long retest confirmed at {level}")
                return True
    
    else:  # direction == "short"
        # Check if level was previously broken to the downside
        level_broken = False
        for _, row in recent_candles.iterrows():
            if row['low'] < level - buffer:
                level_broken = True
                break
        
        if not level_broken:
            logger.debug("Level not previously broken for retest validation")
            return False
        
        # Current candle retest
        current_low = current_candle.get('low', 0)
        current_high = current_candle.get('high', 0)
        current_close = current_candle.get('close', 0)
        
        # Retest condition: candle touches level and bounces
        if current_high >= level - buffer and current_low <= level + buffer:
            # Check for bounce (close below level)
            if current_close < level + buffer:
                logger.info(f"Short retest confirmed at {level}")
                return True
    
    return False


def validate_fvg_fill_with_history(
    level: Tuple[float, float],
    direction: str,
    historical_candles: pd.DataFrame,
    current_candle: Dict[str, Any],
    buffer: float,
    asset: str
) -> bool:
    """FVG formation + fill (actual gap, not just "touch")."""
    if not isinstance(level, (list, tuple)) or len(level) != 2: 
        return False
    
    fvg_low, fvg_high = float(level[0]), float(level[1])
    thresholds = get_pattern_thresholds(asset)
    fvg_tolerance = thresholds["fvg_tolerance"]

    recent = historical_candles.tail(30)
    fvgs = _find_fvgs(recent, tol=fvg_tolerance)

    # Confirm band matches a detected FVG
    band_exists = any((fvg_low >= lo - buffer and fvg_high <= hi + buffer) for _, _, lo, hi in fvgs)
    if not band_exists: 
        logger.debug(f"FVG validation failed: no actual FVG found for band {fvg_low}-{fvg_high}")
        return False

    cur_lo = current_candle.get("low")
    cur_hi = current_candle.get("high")
    
    if cur_lo is None or cur_hi is None:
        logger.error(f"Missing low or high values in current candle for FVG fill validation")
        return False
    
    cur_lo = float(cur_lo)
    cur_hi = float(cur_hi)
    result = (cur_lo <= fvg_high + buffer) and (cur_hi >= fvg_low - buffer)
    
    if result:
        logger.info(f"FVG fill confirmed at {fvg_low}-{fvg_high}")
    
    return result


def enhanced_entry_trigger_met(
    pred: dict,
    historical_candles: pd.DataFrame,
    current_candle: Dict[str, Any],
    buffer: float,
    asset: str
) -> bool:
    """
    Enhanced entry trigger check with historical pattern validation.
    
    Parameters
    ----------
    pred : dict
        Prediction dictionary with condition, level, direction, etc.
    historical_candles : pd.DataFrame
        Historical candles for pattern context validation
    current_candle : Dict[str, Any]
        Current 5-minute candle data
    buffer : float
        Buffer for level validation (must be explicitly provided)
    asset : str
        Asset symbol for buffer calculation (must be explicitly provided)
        
    Returns
    -------
    bool
        True if trigger conditions are met with proper pattern validation
    """
    
    condition = pred.get("condition")
    pattern = pred.get("pattern")
    
    if not condition and not pattern:
        logger.error(f"No condition or pattern found in enhanced prediction")
        return False
    
    raw = condition or pattern
    if not raw:
        logger.error(f"Empty condition/pattern string in enhanced prediction")
        return False
    
    level = pred.get("level")
    direction = pred.get("direction")
    if level is None or direction is None:
        return False
    
    # Buffer must be explicitly provided - no default calculation
    logger.info(f"=== enhanced_entry_trigger_met buffer check ===")
    logger.info(f"pred.get('buffer'): {pred.get('buffer')}")
    logger.info(f"buffer parameter: {buffer}")
    logger.info(f"pred keys: {list(pred.keys()) if pred else 'None'}")
    
    buf = pred.get("buffer", buffer)
    logger.info(f"Final buf value: {buf}")
    
    if buf == 0.0:
        logger.error(f"CRITICAL: Invalid buffer (0.0) for enhanced trigger check on {asset} - CANNOT PROCEED WITH INVALID DATA")
        logger.error(f"pred buffer: {pred.get('buffer')}, parameter buffer: {buffer}")
        return False
    
    # Use enhanced pattern validation with historical context
    return validate_pattern_with_history(
        pattern_type=raw,
        level=level,
        direction=direction,
        historical_candles=historical_candles,
        current_candle=current_candle,
        asset=asset,
        buffer=buf
    )
