"""Advanced market structure analysis for real-time trading decisions."""

from __future__ import annotations

import numpy as np
import pandas as pd
from typing import Dict, List, Tuple, Any, Optional
from datetime import datetime, timezone
from .logger import get_logger
from .config import PROMPT_FLOAT_DECIMALS, TTL_DEFAULT_MINUTES

logger = get_logger(__name__)


class MarketStructureAnalyzer:
    """Analyzes market structure to identify support/resistance, breakouts, and patterns."""
    
    def __init__(self, lookback_periods: int = 20):
        self.lookback_periods = lookback_periods
    
    def analyze_market_structure(self, historical_candles: List[Dict], current_candle: Dict) -> Dict[str, Any]:
        """Comprehensive market structure analysis."""
        
        if len(historical_candles) < self.lookback_periods:
            logger.warning(f"Insufficient historical data: {len(historical_candles)} candles")
            return self._default_structure()
        
        df = pd.DataFrame(historical_candles + [current_candle])
        df['time'] = pd.to_datetime(df['time'])
        df = df.set_index('time').sort_index()
        
        # Extract OHLC data
        high = df['high'].values
        low = df['low'].values
        close = df['close'].values
        
        analysis = {
            'support_levels': self._identify_support_levels(high, low, close),
            'resistance_levels': self._identify_resistance_levels(high, low, close),
            'trend_direction': self._determine_trend_direction(close),
            'trend_strength': self._calculate_trend_strength(close),
            'pivot_points': self._calculate_pivot_points(high, low, close),
            'fibonacci_levels': self._calculate_fibonacci_levels(high, low),
            'market_regime': self._identify_market_regime(high, low, close)
        }
        
        return analysis
    
    def _identify_support_levels(self, high: np.ndarray, low: np.ndarray, close: np.ndarray) -> List[float]:
        """Identify key support levels: the swing lows NEAREST below current price.

        Support must sit BELOW the current price — a swing low overhead is not
        support. The previous implementation returned the 3 *highest* swing lows
        with no price filter, so after a pullback it routinely reported levels
        above price as "support" (measured wrong on ~70% of live judge calls,
        all symbols). We filter to lows below price and keep the nearest few.
        """

        # Find swing lows (local minima)
        swing_lows = []
        window = 3

        for i in range(window, len(low) - window):
            is_swing_low = all(low[i] <= low[i-j] for j in range(1, window+1)) and \
                          all(low[i] <= low[i+j] for j in range(1, window+1))

            if is_swing_low:
                swing_lows.append(low[i])

        clustered_levels = self._cluster_levels(swing_lows, tolerance=0.001)
        price = float(close[-1]) if len(close) else None
        if price is None:
            return sorted(clustered_levels[-3:])
        below = [lv for lv in clustered_levels if lv < price]
        nearest = sorted(below, key=lambda lv: price - lv)[:3]  # closest below price
        return sorted(nearest)

    def _identify_resistance_levels(self, high: np.ndarray, low: np.ndarray, close: np.ndarray) -> List[float]:
        """Identify key resistance levels: the swing highs NEAREST above current price.

        Resistance must sit ABOVE the current price — a swing high below price is
        not resistance. Mirror of :meth:`_identify_support_levels`; filters to
        highs above price and keeps the nearest few.
        """

        # Find swing highs (local maxima)
        swing_highs = []
        window = 3

        for i in range(window, len(high) - window):
            is_swing_high = all(high[i] >= high[i-j] for j in range(1, window+1)) and \
                           all(high[i] >= high[i+j] for j in range(1, window+1))

            if is_swing_high:
                swing_highs.append(high[i])

        clustered_levels = self._cluster_levels(swing_highs, tolerance=0.001)
        price = float(close[-1]) if len(close) else None
        if price is None:
            return sorted(clustered_levels[-3:])
        above = [lv for lv in clustered_levels if lv > price]
        nearest = sorted(above, key=lambda lv: lv - price)[:3]  # closest above price
        return sorted(nearest)
    
    def _determine_trend_direction(self, close: np.ndarray) -> str:
        """Determine overall trend direction using multiple timeframes.

        The slope is normalised by price so the threshold is unit-free: the
        old absolute ±0.001 threshold made GBPUSD (price ~1.3) permanently
        "sideways" and BTC (price ~60k) never "sideways".
        """

        if len(close) < 10:
            return "sideways"

        # Short-term trend (last 5 candles)
        short_trend = np.polyfit(range(5), close[-5:], 1)[0]

        # Medium-term trend (last 10 candles)
        medium_trend = np.polyfit(range(10), close[-10:], 1)[0]

        # Long-term trend (all candles)
        long_trend = np.polyfit(range(len(close)), close, 1)[0]

        # Weighted trend score, as a FRACTION of price per candle
        avg_price = float(np.mean(close))
        if avg_price <= 0:
            return "sideways"
        trend_score = (0.5 * long_trend + 0.3 * medium_trend + 0.2 * short_trend) / avg_price

        # 0.003%/candle ≈ 0.36%/h drift on 5m bars — a real directional push
        threshold = 3e-5
        if trend_score > threshold:
            return "bullish"
        elif trend_score < -threshold:
            return "bearish"
        else:
            return "sideways"
    
    def _calculate_trend_strength(self, close: np.ndarray) -> float:
        """Calculate trend strength using R-squared of linear regression."""
        
        if len(close) < 10:
            return 0.0
        
        x = np.arange(len(close))
        y = close
        
        # Linear regression
        coeffs = np.polyfit(x, y, 1)
        y_pred = np.polyval(coeffs, x)
        
        # R-squared calculation
        ss_res = np.sum((y - y_pred) ** 2)
        ss_tot = np.sum((y - np.mean(y)) ** 2)
        
        if ss_tot == 0:
            return 0.0
        
        r_squared = 1 - (ss_res / ss_tot)
        return max(0.0, min(1.0, r_squared))
    
    def _calculate_pivot_points(self, high: np.ndarray, low: np.ndarray, close: np.ndarray) -> Dict[str, float]:
        """Calculate pivot points from the PRIOR period's range.

        Standard floor pivots use the previous *period's* H/L/C — not a single
        bar. The old code used only the last 5m candle's OHLC, producing a
        meaningless micro-pivot (range = one 5m bar). We aggregate the provided
        window: prior-period high = max(high), low = min(low), close = last
        close. (This field is OFF by default — CONTEXT_INCLUDE_PIVOT_POINTS —
        but is now correct should it ever be enabled.)
        """

        if len(high) == 0:
            return {}

        prev_high = float(np.max(high))
        prev_low = float(np.min(low))
        prev_close = float(close[-1])

        pivot = (prev_high + prev_low + prev_close) / 3
        
        return {
            'pivot': pivot,
            'r1': 2 * pivot - prev_low,
            'r2': pivot + (prev_high - prev_low),
            'r3': prev_high + 2 * (pivot - prev_low),
            's1': 2 * pivot - prev_high,
            's2': pivot - (prev_high - prev_low),
            's3': prev_low - 2 * (prev_high - pivot)
        }
    
    def _calculate_fibonacci_levels(self, high: np.ndarray, low: np.ndarray) -> Dict[str, float]:
        """Calculate Fibonacci retracement levels."""
        
        if len(high) == 0 or len(low) == 0:
            return {}
        
        swing_high = np.max(high[-self.lookback_periods:])
        swing_low = np.min(low[-self.lookback_periods:])
        range_size = swing_high - swing_low
        
        return {
            'fib_23.6': swing_high - 0.236 * range_size,
            'fib_38.2': swing_high - 0.382 * range_size,
            'fib_50.0': swing_high - 0.500 * range_size,
            'fib_61.8': swing_high - 0.618 * range_size,
            'fib_78.6': swing_high - 0.786 * range_size,
            'swing_high': swing_high,
            'swing_low': swing_low
        }
    
    
    
    def _identify_market_regime(self, high: np.ndarray, low: np.ndarray, close: np.ndarray) -> str:
        """Identify current market regime (trending, ranging, volatile).

        Thresholds are scaled for the 5m bars this analyzer actually receives.
        The old daily-scale bands (>2% / <0.5% ATR) made the field a constant
        "low_volatility" on every asset — the trend/ranging branch below was
        unreachable (verified on real June data, report §5).
        """

        if len(close) < 10:
            return "unknown"

        # Calculate volatility (ATR)
        atr = self._calculate_atr(high, low, close, period=14)
        current_atr = atr[-1] if len(atr) > 0 else 0

        avg_price = np.mean(close[-10:])
        volatility_pct = (current_atr / avg_price) * 100 if avg_price > 0 else 0

        # Extreme-volatility short-circuits (5m scale): a 0.3% 5m ATR is a
        # violent tape; below 0.02% nothing tradable is happening.
        if volatility_pct > 0.3:
            return "high_volatility"
        elif volatility_pct < 0.02:
            return "low_volatility"

        # Check for trending vs ranging
        trend_strength = self._calculate_trend_strength(close)

        if trend_strength > 0.7:
            return "strong_trend"
        elif trend_strength > 0.4:
            return "weak_trend"
        else:
            return "ranging"
    
    def _calculate_atr(self, high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
        """Calculate Average True Range."""
        
        if len(high) < period:
            return np.array([])
        
        # True Range calculation
        tr = np.zeros(len(high))
        tr[0] = high[0] - low[0]
        
        for i in range(1, len(high)):
            tr[i] = max(
                high[i] - low[i],
                abs(high[i] - close[i-1]),
                abs(low[i] - close[i-1])
            )
        
        # ATR calculation using exponential moving average
        atr = np.zeros(len(tr))
        atr[0] = np.mean(tr[:period])
        
        alpha = 2.0 / (period + 1)
        for i in range(1, len(tr)):
            atr[i] = alpha * tr[i] + (1 - alpha) * atr[i-1]
        
        return atr
    
    def _cluster_levels(self, levels: List[float], tolerance: float = 0.001) -> List[float]:
        """Cluster nearby price levels to avoid redundancy."""
        
        if not levels:
            return []
        
        levels = sorted(levels)
        clustered = [levels[0]]
        
        for level in levels[1:]:
            # Check if level is close to any existing cluster
            is_close = any(abs(level - cluster) / cluster < tolerance for cluster in clustered)
            
            if not is_close:
                clustered.append(level)
        
        return clustered
    
    def _default_structure(self) -> Dict[str, Any]:
        """Return default structure when insufficient data."""
        
        return {
            'support_levels': [],
            'resistance_levels': [],
            'trend_direction': 'sideways',
            'trend_strength': 0.0,
            'pivot_points': {},
            'fibonacci_levels': {},
            'market_regime': 'unknown'
        }


def build_advanced_context(
    expert_signals: Dict[str, Any],
    historical_candles: List[Dict],
    current_candle: Dict,
    market_analyzer: Optional[MarketStructureAnalyzer] = None,
    ttl_minutes: int = TTL_DEFAULT_MINUTES,
    source_timeframe: str = "5m",
    include_market_breadth: Optional[bool] = None,
    include_pivots: Optional[bool] = None,
    include_fibonacci: Optional[bool] = None,
    use_real_macd: Optional[bool] = None,
) -> Dict[str, Any]:
    """Build comprehensive context for sophisticated trading code execution.

    Noise fields (single-candle pivots, short-window fibonacci, single-asset
    market-breadth) are gated behind config toggles so the context can be kept
    lean. ``source_timeframe`` is stamped onto ``market_structure`` so the model
    is not misled about which timeframe the computed S/R levels come from.
    All flags fall back to ``config`` defaults when left ``None``.
    """
    from .config import (
        CONTEXT_INCLUDE_MARKET_BREADTH, CONTEXT_INCLUDE_PIVOT_POINTS,
        CONTEXT_INCLUDE_FIBONACCI, CONTEXT_USE_REAL_MACD,
    )
    if include_market_breadth is None:
        include_market_breadth = CONTEXT_INCLUDE_MARKET_BREADTH
    if include_pivots is None:
        include_pivots = CONTEXT_INCLUDE_PIVOT_POINTS
    if include_fibonacci is None:
        include_fibonacci = CONTEXT_INCLUDE_FIBONACCI
    if use_real_macd is None:
        use_real_macd = CONTEXT_USE_REAL_MACD

    if market_analyzer is None:
        market_analyzer = MarketStructureAnalyzer()

    # Analyze market structure
    market_structure = market_analyzer.analyze_market_structure(historical_candles, current_candle)
    # Stamp the true source timeframe so the levels are not mistaken for HTF.
    if isinstance(market_structure, dict):
        market_structure['source_timeframe'] = source_timeframe
        if not include_pivots:
            market_structure.pop('pivot_points', None)
        if not include_fibonacci:
            market_structure.pop('fibonacci_levels', None)

    # Analyze price action patterns
    price_action = analyze_price_action_patterns(historical_candles, current_candle)

    # Calculate volatility metrics
    volatility = calculate_volatility_metrics(historical_candles, current_candle)

    # Determine trading session
    session = determine_trading_session(current_candle.get('time'))

    context = {
        'expert_signals': expert_signals,
        'market_structure': market_structure,
        'price_action': price_action,
        'volatility': volatility,
        'session': session,
        'current_price': current_candle.get('close', 0),
        'price_change': calculate_price_change(historical_candles, current_candle),
        'volume_trend': analyze_volume_trend(historical_candles),
        'momentum': calculate_momentum_indicators(historical_candles, use_real_macd=use_real_macd),
        'time_remaining': ttl_minutes,
    }
    if include_market_breadth:
        context['market_breadth'] = calculate_market_breadth(historical_candles)

    return context


def analyze_price_action_patterns(historical_candles: List[Dict], current_candle: Dict) -> Dict[str, Any]:
    """Analyze price action patterns for breakout/reversal detection."""
    
    if len(historical_candles) < 5:
        return {'pattern': 'insufficient_data'}
    
    # Convert to numpy arrays for analysis
    highs = np.array([c['high'] for c in historical_candles + [current_candle]])
    lows = np.array([c['low'] for c in historical_candles + [current_candle]])
    closes = np.array([c['close'] for c in historical_candles + [current_candle]])
    
    patterns = {}
    
    # Breakout detection
    recent_high = np.max(highs[-6:-1])
    recent_low = np.min(lows[-6:-1])
    current_close = current_candle['close']
    
    # Check for breakout above recent high
    if current_close > recent_high:
        patterns['breakout'] = {
            'direction': 'up',
            'level': recent_high,
            'strength': (current_close - recent_high) / recent_high,
            'confirmed': True
        }
    # Check for breakdown below recent low
    elif current_close < recent_low:
        patterns['breakout'] = {
            'direction': 'down',
            'level': recent_low,
            'strength': (recent_low - current_close) / recent_low,
            'confirmed': True
        }
    else:
        patterns['breakout'] = {'direction': 'none', 'confirmed': False}
    
    # Reversal pattern detection (simplified)
    if len(closes) >= 10:
        # Check for double top/bottom patterns
        last_10_closes = closes[-10:]
        if len(np.unique(np.round(last_10_closes, PROMPT_FLOAT_DECIMALS))) <= 3:
            patterns['reversal'] = {
                'pattern': 'consolidation',
                'confidence': 0.6
            }
    
    return patterns


def calculate_volatility_metrics(historical_candles: List[Dict], current_candle: Dict) -> Dict[str, Any]:
    """Calculate comprehensive volatility metrics."""
    
    if len(historical_candles) < 10:
        return {'atr': 0, 'volatility_rank': 0.5, 'volatility_trend': 'stable'}
    
    # Calculate ATR
    highs = np.array([c['high'] for c in historical_candles + [current_candle]])
    lows = np.array([c['low'] for c in historical_candles + [current_candle]])
    closes = np.array([c['close'] for c in historical_candles + [current_candle]])
    
    # True Range calculation
    tr = np.zeros(len(highs))
    tr[0] = highs[0] - lows[0]
    
    for i in range(1, len(highs)):
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1])
        )
    
    # ATR (14-period)
    atr = np.mean(tr[-14:]) if len(tr) >= 14 else np.mean(tr)
    
    # Volatility ranking (0-1 scale)
    price_level = current_candle['close']
    volatility_pct = (atr / price_level) * 100 if price_level > 0 else 0
    volatility_rank = min(1.0, volatility_pct / 3.0)  # Normalize to 0-1
    
    # Volatility trend
    recent_atr = np.mean(tr[-5:]) if len(tr) >= 5 else atr
    earlier_atr = np.mean(tr[-10:-5]) if len(tr) >= 10 else atr
    
    if recent_atr > earlier_atr * 1.1:
        volatility_trend = 'increasing'
    elif recent_atr < earlier_atr * 0.9:
        volatility_trend = 'decreasing'
    else:
        volatility_trend = 'stable'
    
    return {
        'atr': atr,
        'volatility_rank': volatility_rank,
        'volatility_trend': volatility_trend,
        'volatility_pct': volatility_pct
    }


def determine_trading_session(timestamp_str: str) -> str:
    """Determine trading session based on timestamp."""
    
    try:
        if isinstance(timestamp_str, str):
            dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
        else:
            dt = timestamp_str
        
        hour = dt.hour

        # Simplified session detection (UTC). The old `21 <= hour < 7` branch
        # could never be True, so 21:00–06:59 was mislabeled 'other'.
        if 12 <= hour < 16:    # London/NY overlap (highest liquidity)
            return 'london_ny'
        elif 7 <= hour < 12:   # London session
            return 'london'
        elif 16 <= hour < 21:  # NY session
            return 'ny'
        else:                  # 21:00–06:59 UTC
            return 'asian'
    except:
        return 'unknown'


def calculate_price_change(historical_candles: List[Dict], current_candle: Dict) -> float:
    """Calculate recent price change percentage."""
    
    if not historical_candles:
        return 0.0
    
    previous_close = historical_candles[-1]['close']
    current_close = current_candle['close']
    
    return ((current_close - previous_close) / previous_close) * 100


def analyze_volume_trend(historical_candles: List[Dict]) -> Dict[str, Any]:
    """Analyze volume trend and patterns."""
    
    if len(historical_candles) < 5:
        return {'trend': 'stable', 'ratio': 1.0}
    
    volumes = [c.get('volume', 1) for c in historical_candles]
    recent_volume = np.mean(volumes[-3:])
    avg_volume = np.mean(volumes)
    
    volume_ratio = recent_volume / avg_volume if avg_volume > 0 else 1.0
    
    if volume_ratio > 1.3:
        trend = 'increasing'
    elif volume_ratio < 0.7:
        trend = 'decreasing'
    else:
        trend = 'stable'
    
    return {'trend': trend, 'ratio': volume_ratio}


def calculate_momentum_indicators(historical_candles: List[Dict], use_real_macd: Optional[bool] = None) -> Dict[str, Any]:
    """Calculate momentum indicators.

    ``use_real_macd`` (defaults to ``config.CONTEXT_USE_REAL_MACD``):
      - True  → proper EMA(12)/EMA(26) MACD line + EMA(9) signal + histogram.
      - False → legacy SMA-difference (kept for backward compatibility).
    The legacy path mislabelled simple means as ``ema_12``/``ema_26`` and is
    not a real MACD; prefer the real one.
    """
    if use_real_macd is None:
        from .config import CONTEXT_USE_REAL_MACD
        use_real_macd = CONTEXT_USE_REAL_MACD

    if len(historical_candles) < 14:
        return {'rsi': 50, 'macd': 0, 'momentum': 'neutral'}

    closes = np.array([c['close'] for c in historical_candles])

    # Simple RSI calculation
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)

    avg_gain = np.mean(gains[-14:])
    avg_loss = np.mean(losses[-14:])

    if avg_loss == 0:
        rsi = 100
    else:
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

    macd_signal = None
    macd_hist = None
    if use_real_macd and len(closes) >= 26:
        # Real EMA-based MACD (12, 26, 9)
        s = pd.Series(closes, dtype="float64")
        ema_12 = s.ewm(span=12, adjust=False).mean()
        ema_26 = s.ewm(span=26, adjust=False).mean()
        macd_line = ema_12 - ema_26
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        macd = float(macd_line.iloc[-1])
        macd_signal = float(signal_line.iloc[-1])
        macd_hist = macd - macd_signal
    else:
        # Legacy SMA-difference (NOT a real MACD)
        ema_12 = np.mean(closes[-12:]) if len(closes) >= 12 else np.mean(closes)
        ema_26 = np.mean(closes[-26:]) if len(closes) >= 26 else np.mean(closes)
        macd = float(ema_12 - ema_26)

    # Momentum classification (uses MACD histogram when available, else line)
    macd_dir = macd_hist if macd_hist is not None else macd
    if rsi > 70:
        momentum = 'overbought'
    elif rsi < 30:
        momentum = 'oversold'
    elif macd_dir > 0:
        momentum = 'bullish'
    elif macd_dir < 0:
        momentum = 'bearish'
    else:
        momentum = 'neutral'

    out = {
        'rsi': rsi,
        'macd': macd,
        'momentum': momentum
    }
    if macd_signal is not None:
        out['macd_signal'] = macd_signal
        out['macd_hist'] = macd_hist
    return out


def calculate_market_breadth(historical_candles: List[Dict]) -> Dict[str, Any]:
    """Calculate market breadth indicators."""
    
    if len(historical_candles) < 10:
        return {'advance_decline': 0, 'breadth': 'neutral'}
    
    closes = np.array([c['close'] for c in historical_candles])
    
    # Simple advance/decline calculation
    advances = np.sum(np.diff(closes) > 0)
    declines = np.sum(np.diff(closes) < 0)
    total = advances + declines
    
    if total == 0:
        advance_decline = 0
    else:
        advance_decline = (advances - declines) / total
    
    if advance_decline > 0.3:
        breadth = 'strong_bullish'
    elif advance_decline > 0.1:
        breadth = 'bullish'
    elif advance_decline < -0.3:
        breadth = 'strong_bearish'
    elif advance_decline < -0.1:
        breadth = 'bearish'
    else:
        breadth = 'neutral'
    
    return {
        'advance_decline': advance_decline,
        'breadth': breadth
    }


def _atr_from_ohlc(highs: List[float], lows: List[float], closes: List[float], period: int) -> Optional[float]:
    """Simple ATR (mean of last `period` true ranges). Returns None if too short."""
    n = len(closes)
    if n < 2 or len(highs) != n or len(lows) != n:
        return None
    trs = [highs[0] - lows[0]]
    for i in range(1, n):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    p = min(period, len(trs))
    return float(np.mean(trs[-p:])) if p > 0 else None


def _trend_label(closes: List[float], fast: int, slow: int, min_sep_pct: float) -> str:
    """Classify a close series as up/down/range via EMA(fast) vs EMA(slow) separation."""
    if len(closes) < max(slow, 2):
        return "na"
    s = pd.Series(closes, dtype="float64")
    ema_f = float(s.ewm(span=fast, adjust=False).mean().iloc[-1])
    ema_s = float(s.ewm(span=slow, adjust=False).mean().iloc[-1])
    if ema_s == 0:
        return "na"
    sep_pct = (ema_f / ema_s - 1.0) * 100.0
    if abs(sep_pct) < min_sep_pct:
        return "range"
    return "up" if sep_pct > 0 else "down"


def compute_htf_context(htf_candles: Dict[str, List[Dict]], asset: Optional[str] = None,
                        as_of: Optional[datetime] = None) -> Dict[str, Any]:
    """Compute higher-timeframe trend/regime/ATR from each asset's OWN candles.

    ``htf_candles`` is ``{interval: [{time,open,high,low,close,volume}, ...]}`` as
    produced by the runner's HTF fetch. Returns ``{}`` when no HTF candles are
    supplied (so callers stay inert when HTF context is disabled). All thresholds
    come from ``config`` and are per-asset overridable — nothing here is hardcoded
    per instrument, and trend/ATR are derived from the asset's own series so the
    classification is portable across BTC/XAU/GBP/etc.

    Output (only for the timeframes present): ``trend_<tf>``, ``atr_<tf>``,
    ``atr_pct_<tf>``, plus a consensus ``htf_trend`` (4h+1d agreement) and
    ``regime`` (high/normal/low volatility).

    ``as_of`` names the UTC session the ``er_1h_session_pctile`` is read against.
    It defaults to the newest supplied HTF candle's time, which is ≈ now in live
    and the replayed bar in a backtest — so neither caller can accidentally
    percentile a historical reading against the wall clock's session.
    """
    if not htf_candles:
        return {}
    from .config import (
        HTF_TREND_EMA_FAST, HTF_TREND_EMA_SLOW,
        HTF_TREND_MIN_SEPARATION_PCT, HTF_TREND_MIN_SEPARATION_PCT_BY_ASSET,
        HTF_REGIME_HIGH_VOL_ATR_PCT, HTF_REGIME_LOW_VOL_ATR_PCT,
        HTF_REGIME_ATR_PCT_BY_ASSET, ATR_STOP_PERIOD, resolve_for_asset,
    )
    min_sep = resolve_for_asset(HTF_TREND_MIN_SEPARATION_PCT_BY_ASSET, asset, HTF_TREND_MIN_SEPARATION_PCT)
    bands = resolve_for_asset(HTF_REGIME_ATR_PCT_BY_ASSET, asset, None) or {}
    hi_band = bands.get("high", HTF_REGIME_HIGH_VOL_ATR_PCT)
    lo_band = bands.get("low", HTF_REGIME_LOW_VOL_ATR_PCT)

    if as_of is None:
        # Newest bar across the supplied timeframes ≈ "now" for whoever built
        # them. None stays None when no candle carries a parseable time, which
        # leaves session_er_percentile on its own wall-clock default.
        stamps = [
            candle_time_utc(c)
            for cs in (htf_candles or {}).values()
            for c in (cs[-1:] if cs else [])
        ]
        stamps = [s for s in stamps if s is not None]
        as_of = max(stamps) if stamps else None

    out: Dict[str, Any] = {}
    for interval, candles in (htf_candles or {}).items():
        if not candles:
            continue
        try:
            closes = [float(c["close"]) for c in candles if c.get("close") is not None]
            highs = [float(c["high"]) for c in candles if c.get("high") is not None]
            lows = [float(c["low"]) for c in candles if c.get("low") is not None]
        except (KeyError, TypeError, ValueError):
            continue
        if not closes:
            continue
        out[f"trend_{interval}"] = _trend_label(closes, HTF_TREND_EMA_FAST, HTF_TREND_EMA_SLOW, min_sep)
        atr = _atr_from_ohlc(highs, lows, closes, ATR_STOP_PERIOD)
        if atr is not None and closes[-1]:
            out[f"atr_{interval}"] = round(atr, 6)
            out[f"atr_pct_{interval}"] = round(atr / closes[-1] * 100.0, 4)

    # Efficiency ratio on 1h closes: |net move| / sum(|bar-to-bar moves|).
    # ER < REGIME_ER_CHOP_MAX ⇒ chop (whipsaw tape — fade edges, don't chase);
    # ER ≥ REGIME_ER_TREND_MIN ⇒ trending. Injected so prompts can switch
    # entry STYLE per regime (report §8: BTC chop entries were −$140).
    from .config import (
        REGIME_ER_BARS, REGIME_ER_CHOP_MAX, REGIME_ER_TREND_MIN,
        REGIME_ER_CHOP_MAX_BY_ASSET, REGIME_ER_TREND_MIN_BY_ASSET,
    )
    # Per-asset ER thresholds (e.g. GBP fades a wider chop zone). resolve_for_asset
    # falls back to the global default so all other assets are byte-identical.
    er_chop_max = resolve_for_asset(REGIME_ER_CHOP_MAX_BY_ASSET, asset, REGIME_ER_CHOP_MAX)
    er_trend_min = resolve_for_asset(REGIME_ER_TREND_MIN_BY_ASSET, asset, REGIME_ER_TREND_MIN)
    h1 = (htf_candles or {}).get("1h") or []
    try:
        closes_1h = [float(c["close"]) for c in h1 if c.get("close") is not None]
    except (KeyError, TypeError, ValueError):
        closes_1h = []
    if len(closes_1h) >= REGIME_ER_BARS + 1:
        window = closes_1h[-(REGIME_ER_BARS + 1):]
        net = abs(window[-1] - window[0])
        path = sum(abs(window[i + 1] - window[i]) for i in range(len(window) - 1))
        er = (net / path) if path > 0 else 0.0
        out["er_1h"] = round(er, 3)
        out["regime_hint"] = (
            "chop" if er < er_chop_max
            else "trending" if er >= er_trend_min
            else "mixed"
        )
        # Session context (2026-07-18): the bare er_1h number hides HOW dead the
        # tape is (freeze week 1: EUR buys fired at session ER 0.003 — the 8th
        # percentile of EUR-Asia history — 0W-4L). Percentile vs this asset's
        # own 4-year distribution FOR THIS UTC SESSION gives the judge a scale.
        #
        # SCOPE CORRECTION 2026-07-24 (user directive; same rationale as the
        # 07-23 sl_anchor seal). This pctile was injected for EVERY asset with a
        # profiler table (all of them), so the shared-prompt line "pctile<=20 =>
        # entries need an extreme edge or nothing" silently made metals/BTC/USTEC
        # more selective too. Measured: expert-flat rose ~42->~70/day and trades
        # ~14->~4/day across the 07-18 boundary while the tape (er_1h/regime) was
        # unchanged; on non-FX the cut was not a clean win (XAU/XAG were the wk1
        # profit engine, Asia session +$36). Gate the injection to the redesign's
        # approved scope (EUR/GBP) so every other asset sees byte-identical week-1
        # context and the "when present" clauses in the prompt go inert for them.
        #
        # 2026-08-06: this gate now reads its OWN table rather than borrowing the
        # stop-floor's. The two were re-enabled for the same assets on the same
        # day, but they are different levers — geometry vs selectivity — and
        # sharing one flag is how the 07-18/07-23 scope divergence happened in
        # the first place. See the config note for why the trade-count cut is
        # now intended rather than incidental.
        from .config import SESSION_PCTILE_CONTEXT_ENABLED_BY_ASSET
        if resolve_for_asset(SESSION_PCTILE_CONTEXT_ENABLED_BY_ASSET, asset, False):
            try:
                from .session_stats import session_of, session_er_percentile
                pctile = session_er_percentile(asset, er, as_of)
                if pctile is not None:
                    out["session"] = session_of(as_of)
                    out["er_1h_session_pctile"] = pctile
            except Exception:
                pass  # context enrichment must never break the cycle

    # Consensus HTF trend: prefer 4h+1d agreement, else whichever is directional.
    t4, t1d = out.get("trend_4h"), out.get("trend_1d")
    if t4 in ("up", "down") and t4 == t1d:
        out["htf_trend"] = t4
    elif t4 in ("up", "down"):
        out["htf_trend"] = t4
    elif t1d in ("up", "down"):
        out["htf_trend"] = t1d
    else:
        out["htf_trend"] = "mixed"

    # Regime from a reference ATR% (1h preferred, else 4h, else 1d).
    ref = out.get("atr_pct_1h") or out.get("atr_pct_4h") or out.get("atr_pct_1d")
    if ref is not None:
        out["regime"] = (
            "high_volatility" if ref >= hi_band else
            "low_volatility" if ref < lo_band else "normal"
        )
    return out


def _kaufman_er(closes: List[float]) -> Optional[float]:
    """Kaufman efficiency ratio |net| / Σ|step| over a close series (≥2 closes)."""
    if len(closes) < 2:
        return None
    net = abs(closes[-1] - closes[0])
    path = sum(abs(closes[i + 1] - closes[i]) for i in range(len(closes) - 1))
    return (net / path) if path > 0 else 0.0


def _candle_utc(c: Dict) -> Optional[Any]:
    """Candle 'time' as an aware-UTC datetime (ISO str / datetime / epoch), else None."""
    from datetime import datetime, timezone
    t = c.get("time") if isinstance(c, dict) else None
    if t is None:
        return None
    if isinstance(t, str):
        try:
            dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
        except ValueError:
            return None
    elif isinstance(t, (int, float)):
        try:
            dt = datetime.fromtimestamp(float(t), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif hasattr(t, "isoformat"):  # datetime / pd.Timestamp
        dt = t.to_pydatetime() if hasattr(t, "to_pydatetime") else t
    else:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def candle_time_utc(candle: Dict) -> Optional[Any]:
    """Public alias of :func:`_candle_utc` — a candle's time as aware UTC.

    Exported because every caller that must be replay-correct needs the same
    "what time is it, according to the data" answer that the session helpers
    are keyed on (see ``recommended_stop_distance(as_of=...)``).
    """
    return _candle_utc(candle)


def compute_session_tape(c5_candles: List[Dict], htf_trend: Optional[str],
                         asset: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """What the CURRENT UTC session is actually doing — the LP-08 observation.

    2026-07-25, Phase 0 of the LP-08 fix plan (label-vs-tape). ``htf_trend`` is
    computed from 1h/4h/1d lookbacks and stays stale for hours after an
    intraday reversal; this measures the live session from the 5m series the
    cycle already has in memory, so the conflict is observable per cycle:

      direction   sign of (last close − session open)
      er_session  Kaufman ER over the elapsed session's closes
      er_2h       trailing-2h ER (24×5m) — LP-14's spike-vs-sustained check
      conflict    htf label directional AND session opposite AND the session
                  move is both directional (er_session ≥ SESSION_TAPE_MIN_ER)
                  and persistent (er_2h ≥ SESSION_TAPE_MIN_ER_2H)

    Pure measurement: no config *mode* is consulted here (callers gate via
    ``config.session_tape_mode_for``), so the replay/research scripts reuse the
    exact shipped math. Returns None when the signal does not exist yet
    (< SESSION_TAPE_MIN_BARS elapsed 5m bars in-session — the same "when
    present" semantics as the session pctile), so callers stay inert.
    "Now" is the LAST CANDLE's time, not the wall clock — replay parity.
    """
    from .config import SESSION_TAPE_MIN_BARS, SESSION_TAPE_MIN_ER, SESSION_TAPE_MIN_ER_2H
    if not c5_candles:
        return None
    last_dt = _candle_utc(c5_candles[-1])
    if last_dt is None:
        return None
    try:
        from .session_stats import session_bounds
        sess_name, sess_start, _ = session_bounds(last_dt)
    except Exception:
        return None
    sess: List[Dict] = []
    for c in c5_candles:
        dt = _candle_utc(c)
        if dt is not None and dt >= sess_start:
            sess.append(c)
    if len(sess) < SESSION_TAPE_MIN_BARS:
        return None
    try:
        open0 = float(sess[0]["open"])
        closes = [float(c["close"]) for c in sess if c.get("close") is not None]
        all_closes = [float(c["close"]) for c in c5_candles if isinstance(c, dict) and c.get("close") is not None]
    except (KeyError, TypeError, ValueError):
        return None
    if len(closes) < 2 or not open0:
        return None
    move = closes[-1] - open0
    direction = "up" if move > 0 else "down" if move < 0 else "flat"
    er_session = _kaufman_er([open0] + closes)
    er_1h_5m = _kaufman_er(all_closes[-13:]) if len(all_closes) >= 13 else None
    er_2h_5m = _kaufman_er(all_closes[-25:]) if len(all_closes) >= 25 else None
    conflict = bool(
        htf_trend in ("up", "down")
        and direction in ("up", "down")
        and direction != htf_trend
        and er_session is not None and er_session >= SESSION_TAPE_MIN_ER
        and er_2h_5m is not None and er_2h_5m >= SESSION_TAPE_MIN_ER_2H
    )
    return {
        "session": sess_name,
        "bars": len(sess),
        "direction": direction,
        "move_pct": round(move / open0 * 100.0, 4),
        "er_session": round(er_session, 3) if er_session is not None else None,
        "er_2h": round(er_2h_5m, 3) if er_2h_5m is not None else None,
        "er_ratio_2h_1h": (round(er_2h_5m / er_1h_5m, 2)
                           if er_2h_5m is not None and er_1h_5m else None),
        "htf_trend": htf_trend,
        "conflict": conflict,
    }


def atr_from_candles(candles: List[Dict], period: Optional[int] = None) -> Optional[float]:
    """Public helper: ATR (price units) from a list of OHLC candle dicts."""
    if period is None:
        from .config import ATR_STOP_PERIOD
        period = ATR_STOP_PERIOD
    try:
        highs = [float(c["high"]) for c in candles if c.get("high") is not None]
        lows = [float(c["low"]) for c in candles if c.get("low") is not None]
        closes = [float(c["close"]) for c in candles if c.get("close") is not None]
    except (KeyError, TypeError, ValueError):
        return None
    return _atr_from_ohlc(highs, lows, closes, period)


def recommended_stop_distance(atr_ref: Optional[float], price: Optional[float], asset: Optional[str] = None,
                              as_of: Optional[datetime] = None) -> Optional[float]:
    """Volatility-scaled SL distance (price units), or None when disabled/invalid.

    SL_distance = clamp(ATR_STOP_MULT × ATR, floor = min %-band × price,
                        ceil = BACKTEST_MAX_SL_DISTANCE).

    Bounding by the existing per-asset min %-floor and max-SL guard guarantees
    the recommendation can never be rejected by the downstream validators, and
    keeps it adaptive per asset (ATR is from that asset's own data). Returns
    None unless ``config.ATR_STOP_ENABLED`` is True — so this is inert by default.

    ``as_of`` is the time the stop is being placed at, used to pick the UTC
    session for the noise term. Live passes the newest candle's time (≈ now, so
    behaviour is unchanged); the backtest MUST pass the checkpoint, otherwise
    the session floor for a replayed 2024 bar is read off whatever session the
    machine happens to be in when the replay runs.
    """
    from .config import (
        ATR_STOP_ENABLED, ATR_STOP_MULT, ATR_STOP_MULT_BY_ASSET,
        CS_MIN_DISTANCE_TO_SL_PCT, CS_MIN_DISTANCE_TO_SL_PCT_BY_ASSET,
        BACKTEST_MAX_SL_DISTANCE, resolve_for_asset,
    )
    if not ATR_STOP_ENABLED:
        return None
    if not atr_ref or not price or atr_ref <= 0 or price <= 0:
        return None
    mult = resolve_for_asset(ATR_STOP_MULT_BY_ASSET, asset, ATR_STOP_MULT)
    raw = float(mult) * float(atr_ref)
    floor_pct = resolve_for_asset(CS_MIN_DISTANCE_TO_SL_PCT_BY_ASSET, asset, CS_MIN_DISTANCE_TO_SL_PCT)
    floor = float(floor_pct) / 100.0 * float(price)
    ceil = resolve_for_asset(BACKTEST_MAX_SL_DISTANCE, asset, BACKTEST_MAX_SL_DISTANCE.get("default"))
    dist = max(raw, floor)
    # Session-noise term (2026-07-18): the live ATR term tracks dead sessions
    # down into noise; the 4-year session quantile doesn't. Inert (None) unless
    # the asset is enabled in SESSION_NOISE_FLOOR_ENABLED_BY_ASSET and the
    # profiler table exists — every other asset is byte-identical.
    try:
        from .session_stats import session_noise_floor_pct
        sess_pct = session_noise_floor_pct(asset, as_of)
    except Exception as e:  # never let context stats break the trade path
        logger.warning(f"session noise floor lookup failed for {asset}: {e}")
        sess_pct = None
    if sess_pct:
        sess_dist = float(sess_pct) / 100.0 * float(price)
        if sess_dist > dist:
            logger.info(
                f"SL floor raised by session noise for {asset}: "
                f"{dist:.6g} -> {sess_dist:.6g} ({sess_pct:.3f}% of price; "
                f"ATR term {raw:.6g} had collapsed below session noise)"
            )
            dist = sess_dist
    if ceil and dist > float(ceil):
        # The per-asset max-SL cap is overriding the volatility-scaled floor —
        # the resulting stop sits INSIDE current noise (the exact defect the
        # ATR floor exists to prevent; routine for the tight FX caps in high
        # vol). Logged loudly so the registry cap gets recalibrated instead of
        # silently reintroducing noise stops.
        logger.warning(
            f"SL cap binds below volatility floor for {asset}: "
            f"ATR-scaled distance {dist:.6g} capped to {float(ceil):.6g} "
            f"(mult={mult}, atr={float(atr_ref):.6g}) — stop will sit inside noise"
        )
        dist = float(ceil)
    return float(dist)


__all__ = [
    'MarketStructureAnalyzer',
    'build_advanced_context',
    'analyze_price_action_patterns',
    'calculate_volatility_metrics',
    'determine_trading_session',
    'compute_htf_context',
    'compute_session_tape',
    'candle_time_utc',
    'atr_from_candles',
    'recommended_stop_distance',
]
