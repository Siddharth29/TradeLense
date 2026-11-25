"""Execute generated trading code for 5-minute candle monitoring."""

from __future__ import annotations

import ast
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd

from .candles import is_expected_market_gap
from .logger import get_logger

logger = get_logger(__name__)

# Keywords that open an indented block in Python
_BLOCK_OPENERS = frozenset({
    'if', 'elif', 'else', 'for', 'while', 'try', 'except',
    'finally', 'with', 'def', 'class', 'async',
})


def _fill_empty_blocks(code: str) -> str:
    """Insert 'pass' into empty control-flow blocks in GPT-generated code.

    GPT occasionally emits ``if condition:\\n`` with no body, which causes
    IndentationError/SyntaxError at parse time.  This function detects such
    empty blocks and inserts a ``pass`` statement so the code is valid Python.
    """
    lines = code.splitlines(keepends=True)
    result: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        result.append(line)
        stripped = line.rstrip('\n\r')
        lstripped = stripped.lstrip()
        first_word = lstripped.split()[0].rstrip(':') if lstripped.split() else ''
        if (stripped.endswith(':')
                and first_word in _BLOCK_OPENERS
                and not lstripped.startswith('#')):
            current_indent = len(stripped) - len(lstripped)
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j >= len(lines):
                needs_pass = True
            else:
                nxt = lines[j].rstrip('\n\r')
                nxt_l = nxt.lstrip()
                nxt_indent = len(nxt) - len(nxt_l) if nxt_l else current_indent
                needs_pass = nxt_indent <= current_indent
            if needs_pass:
                result.append(' ' * (current_indent + 4) + 'pass\n')
        i += 1
    return ''.join(result)


def _indent_orphan_returns(code: str) -> str:
    """Re-indent a bare ``return`` sitting at the SAME indent as the block
    opener directly above it — it is unambiguously the intended block body
    (a ``return`` is terminal, so it cannot be a sibling statement).

    Without this, ``_fill_empty_blocks`` inserts ``pass`` and the orphaned
    ``return`` executes unconditionally, turning the program into a permanent
    no-op "zombie" (2026-07-08 audit, PATTERN_LIBRARY LP-09). Mirrors the
    implementation in code_sniper_executor.py.
    """
    import re as _re
    ret_re = _re.compile(r'return\b')
    lines = code.splitlines(keepends=True)
    out = list(lines)
    for i, line in enumerate(lines):
        stripped = line.rstrip('\n\r')
        lstripped = stripped.lstrip()
        first_word = lstripped.split()[0].rstrip(':') if lstripped.split() else ''
        if not (stripped.endswith(':')
                and first_word in _BLOCK_OPENERS
                and not lstripped.startswith('#')):
            continue
        indent = len(stripped) - len(lstripped)
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j >= len(lines):
            continue
        nxt = lines[j].rstrip('\n\r')
        nxt_l = nxt.lstrip()
        nxt_indent = len(nxt) - len(nxt_l)
        if nxt_indent == indent and ret_re.match(nxt_l):
            eol = lines[j][len(nxt):]
            out[j] = ' ' * (indent + 4) + nxt_l + eol
    return ''.join(out)


def _sanitize_generated_code(code: str) -> str:
    """Sanitize AI-generated code before sandbox execution.

    Fixes common GPT code issues (in order):
    - Orphan ``return`` at the opener's indent → re-indented as the block body
      (semantics-preserving; LP-09 zombie fix, 2026-07-08)
    - Empty if/else/for/while/try/except blocks → inserts 'pass'

    Only applies repair when a SyntaxError is detected, so valid code passes
    through unchanged. Repairs are logged at WARNING — silent repairs are how
    broken judge programs became invisible no-ops on 2026-07-06/07.
    """
    try:
        ast.parse(code)
        return code
    except SyntaxError:
        pass
    repaired = _indent_orphan_returns(code)
    step = "indent_orphan_returns" if repaired != code else None
    try:
        ast.parse(repaired)
        logger.warning(
            "GPT code auto-repaired before execution",
            extra={"repair": step or "none"},
        )
        return repaired
    except SyntaxError:
        pass
    repaired = _fill_empty_blocks(repaired)
    try:
        ast.parse(repaired)
        logger.warning(
            "GPT code auto-repaired before execution",
            extra={"repair": (step + "+fill_empty_blocks") if step else "fill_empty_blocks"},
        )
    except SyntaxError as e:
        logger.warning(
            "GPT code has unfixable syntax error after auto-repair",
            extra={"syntax_error": str(e)},
        )
    return repaired


class TradingCodeExecutor:
    """Execute generated trading code with safety checks."""
    
    def __init__(self):
        self.allowed_imports = {
            'math', 'statistics', 'numpy', 'pandas'
        }
    
    def execute_trading_code(
        self, 
        code: str, 
        current_candle: Dict[str, Any], 
        historical_candles: List[Dict[str, Any]],
        backtest_mode: bool = False,
        asset: str | None = None,
    ) -> Dict[str, Any]:
        """
        Execute generated trading code with only candle data in a sandboxed environment.
        
        The code is self-contained and calculates all indicators from the provided candles.
        NO external context is available during execution.
        
        Args:
            code: Generated Python trading code
            current_candle: Current 5-minute candle data
            historical_candles: Last 40 historical candles

        Returns:
            Trading decision or hold signal
        """

        # Convert all candle numeric fields to float BEFORE any processing
        def sanitize_candle(candle: Dict[str, Any]) -> Dict[str, Any]:
            """Convert all numeric candle fields to float to prevent string arithmetic errors."""
            sanitized = candle.copy()
            for field in ['open', 'high', 'low', 'close', 'volume']:
                if field in sanitized and sanitized[field] is not None:
                    try:
                        sanitized[field] = float(sanitized[field])
                    except (ValueError, TypeError):
                        sanitized[field] = 0.0
            return sanitized
        
        # Sanitize all candles to ensure numeric types
        current_candle = sanitize_candle(current_candle)
        historical_candles = [sanitize_candle(c) for c in historical_candles]
        
        logger.debug(
            "Sanitized candle data for code execution",
            extra={
                'current_candle_types': {k: type(v).__name__ for k, v in current_candle.items()},
                'historical_count': len(historical_candles),
                'first_historical_types': {k: type(v).__name__ for k, v in historical_candles[0].items()} if historical_candles else {}
            }
        )
        
        try:
            # Sanitize GPT-generated code: fix empty if/else/for blocks before execution
            code = _sanitize_generated_code(code)

            # Validate code safety
            self._validate_code_safety(code)
            
            # Validate candle data before execution
            from .candles import validate_candle_sequence_and_alignment, Candle
            from datetime import timedelta
            
            # Convert candles to Candle objects for validation
            candle_objects = []
            all_candles = historical_candles + [current_candle]
            
            # Log candle intervals before validation for debugging
            logger.debug(
                "CodeExecutor: Preparing candles for validation",
                extra={
                    'historical_count': len(historical_candles),
                    'current_candle_time': current_candle.get('time') if isinstance(current_candle, dict) else None,
                    'combined_count': len(all_candles)
                }
            )
            
            for candle_data in all_candles:
                if isinstance(candle_data, dict):
                    # Convert timestamp to datetime if needed
                    time_value = candle_data.get('time')
                    if not isinstance(time_value, datetime):
                        if isinstance(time_value, str):
                            try:
                                # Parse ISO format timestamp
                                time_value = datetime.fromisoformat(time_value.replace('Z', '+00:00'))
                            except ValueError:
                                # Fallback to current time if parsing fails
                                time_value = datetime.now(timezone.utc)
                        elif hasattr(time_value, 'to_pydatetime'):
                            # Handle pandas Timestamp
                            time_value = time_value.to_pydatetime()
                        elif time_value is not None:
                            # Try pd.to_datetime as fallback
                            time_value = pd.to_datetime(time_value).to_pydatetime()
                    
                    candle_objects.append(Candle(
                        time=time_value,
                        open=candle_data.get('open', 0),
                        high=candle_data.get('high', 0),
                        low=candle_data.get('low', 0),
                        close=candle_data.get('close', 0),
                        volume=candle_data.get('volume', 0)
                    ))
            
            # Sort candles by time to ensure correct sequence
            candle_objects.sort(key=lambda c: c.time)
            
            # Add defensive check: Log intervals between consecutive candles before validation
            if len(candle_objects) > 1:
                interval_details = []
                for i in range(1, len(candle_objects)):
                    prev_time = candle_objects[i-1].time
                    curr_time = candle_objects[i].time
                    time_diff = curr_time - prev_time
                    expected_diff = timedelta(minutes=5)
                    interval_details.append({
                        'index': i,
                        'prev_time': prev_time.isoformat(),
                        'curr_time': curr_time.isoformat(),
                        'interval': str(time_diff),
                        'expected': str(expected_diff),
                        'matches': time_diff == expected_diff
                    })
                
                logger.debug(
                    "CodeExecutor: Candle interval analysis before validation",
                    extra={
                        'candle_count': len(candle_objects),
                        'intervals': interval_details,
                        'first_candle_time': candle_objects[0].time.isoformat() if candle_objects else None,
                        'last_candle_time': candle_objects[-1].time.isoformat() if candle_objects else None
                    }
                )
                
                # Check for any gaps - fail fast with detailed error
                gaps = [d for d in interval_details if not d['matches']]
                if gaps:
                    # Filter out expected market gaps (forex weekends, holidays)
                    real_gaps = []
                    for d in gaps:
                        prev_t = pd.Timestamp(d['prev_time'])
                        curr_t = pd.Timestamp(d['curr_time'])
                        if not is_expected_market_gap(prev_t, curr_t, asset):
                            real_gaps.append(d)
                    
                    if real_gaps:
                        gap_details = [f"Index {d['index']}: {d['prev_time']} -> {d['curr_time']} (got {d['interval']}, expected {d['expected']})" for d in real_gaps]
                        error_msg = f"CRITICAL: Non-consecutive 5-minute candles detected: {', '.join(gap_details)}"
                        logger.error(
                            "CodeExecutor: Non-consecutive candles detected before validation",
                            extra={
                                'error_type': 'CandleIntervalError',
                                'candle_count': len(candle_objects),
                                'gap_count': len(real_gaps),
                                'gap_details': gap_details,
                                'all_intervals': interval_details
                            }
                        )
                        raise ValueError(error_msg)
                    else:
                        logger.info(
                            f"CodeExecutor: {len(gaps)} gap(s) detected but all are expected market closures (asset={asset})",
                            extra={'gap_count': len(gaps), 'asset': asset}
                        )
            
            # Validate candle sequence and alignment (expect 41 candles)
            # In backtest mode, skip time alignment check (historical candles don't align to current time)
            current_time = datetime.now(timezone.utc) if not backtest_mode else None
            is_valid, error_msg = validate_candle_sequence_and_alignment(
                candle_objects,
                current_time=current_time,
                expected_count=41,
                interval="5m",  # Code executor uses 5m candles
                backtest_mode=backtest_mode,
                asset=asset,
            )
            
            if not is_valid:
                # Log detailed candle sequence for debugging
                candle_times = [c.time.isoformat() for c in candle_objects]
                logger.error(
                    "CodeExecutor.execute_code() - candle validation failed before code execution",
                    extra={
                        'error_type': 'ValidationError',
                        'candle_count': len(candle_objects),
                        'current_time': current_time.isoformat() if current_time is not None else None,
                        'current_price': current_candle.get('close', 0),
                        'error': error_msg,
                        'method': 'CodeExecutor.execute_code',
                        'candle_times': candle_times,
                        'first_candle_time': candle_times[0] if candle_times else None,
                        'last_candle_time': candle_times[-1] if candle_times else None,
                        'historical_count': len(historical_candles),
                        'current_candle_time': current_candle.get('time') if isinstance(current_candle, dict) else None
                    }
                )
                raise ValueError(f"Candle validation failed: {error_msg}")
            else:
                logger.info(
                    "[TIMESTAMP-VALIDATION] Rolling window validation passed for code execution",
                    extra={
                        "historical_count": len(historical_candles),
                        "total_candles": len(candle_objects),
                        "first_candle_time": candle_objects[0].time.isoformat() if candle_objects else None,
                        "last_candle_time": candle_objects[-1].time.isoformat() if candle_objects else None,
                        "backtest_mode": backtest_mode,
                        "validation_context": "code_executor_rolling_window"
                    }
                )
            
            # Use simplified sandbox execution for better reliability
            from .simple_sandbox import run_in_sandbox
            
            # Validate candles before processing - filter out any with None time
            def validate_candle(candle):
                """Validate candle has required fields, skip if time is None."""
                if not isinstance(candle, dict):
                    return candle
                if candle.get('time') is None:
                    logger.warning(f"Skipping candle with None time: {candle.get('close', 'unknown')}")
                    return None
                return candle
            
            # Filter out invalid candles
            validated_historical = [c for c in [validate_candle(c) for c in historical_candles] if c is not None]
            validated_current = validate_candle(current_candle)
            
            if validated_current is None:
                raise ValueError(f"Current candle has None time field - cannot execute code")
            
            # CRITICAL: Filter to ensure only consecutive candles are used
            # This prevents gaps from accumulating when candles are skipped upstream
            def filter_consecutive_candles(candles_list, interval_minutes=5):
                """Filter candles to ensure only consecutive intervals, removing any gaps.
                
                Expected market gaps (forex weekends, holidays) are preserved — only
                truly unexpected gaps (data feed issues) cause a candle to be dropped.
                """
                if not candles_list or len(candles_list) <= 1:
                    return candles_list
                
                from datetime import timedelta
                expected_diff = timedelta(minutes=interval_minutes)
                
                # Sort by time first
                sorted_candles = sorted(candles_list, key=lambda c: pd.Timestamp(c.get('time')) if isinstance(c, dict) else c.time)
                
                filtered = [sorted_candles[0]]  # Always include first
                
                for i in range(1, len(sorted_candles)):
                    prev_candle = sorted_candles[i-1]
                    curr_candle = sorted_candles[i]
                    
                    prev_time = pd.Timestamp(prev_candle.get('time')) if isinstance(prev_candle, dict) else prev_candle.time
                    curr_time = pd.Timestamp(curr_candle.get('time')) if isinstance(curr_candle, dict) else curr_candle.time
                    
                    time_diff = curr_time - prev_time
                    
                    if time_diff == expected_diff:
                        filtered.append(curr_candle)
                    elif is_expected_market_gap(prev_time, curr_time, asset):
                        # Weekend / holiday closure — keep the candle; the gap is expected
                        filtered.append(curr_candle)
                        logger.debug(
                            f"Allowed expected market gap: {prev_time} -> {curr_time} (gap: {time_diff})",
                            extra={
                                'prev_time': prev_time.isoformat(),
                                'curr_time': curr_time.isoformat(),
                                'interval': str(time_diff),
                                'asset': asset,
                            }
                        )
                    else:
                        # Unexpected gap in data feed — log and skip this candle
                        logger.warning(
                            f"Filtering out non-consecutive candle: {prev_time} -> {curr_time} (gap: {time_diff})",
                            extra={
                                'prev_time': prev_time.isoformat(),
                                'curr_time': curr_time.isoformat(),
                                'interval': str(time_diff),
                                'expected': str(expected_diff),
                                'asset': asset,
                            }
                        )
                
                return filtered
            
            # Filter historical candles to ensure consecutive sequence
            filtered_historical = filter_consecutive_candles(validated_historical, interval_minutes=5)
            
            # Also check that current candle is consecutive with last historical
            if filtered_historical:
                last_hist_time = pd.Timestamp(filtered_historical[-1].get('time')) if isinstance(filtered_historical[-1], dict) else filtered_historical[-1].time
                current_time = pd.Timestamp(validated_current.get('time')) if isinstance(validated_current, dict) else validated_current.time
                time_diff = current_time - last_hist_time
                
                if time_diff != timedelta(minutes=5):
                    logger.warning(
                        f"Current candle not consecutive with last historical: {last_hist_time} -> {current_time} (gap: {time_diff})",
                        extra={
                            'last_historical_time': last_hist_time.isoformat(),
                            'current_candle_time': current_time.isoformat(),
                            'interval': str(time_diff),
                            'expected': '0:05:00'
                        }
                    )
                    # Still try to use it, but log the issue - validation will catch it
            
            # Convert candles to expected format: [0-39] historical, [40] current to check
            # CRITICAL: Ensure monitor uses historical candles passed to execute_generated_code (not live data)
            candles_5 = filtered_historical + [validated_current]
            
            # Get now value - MUST be string for sandbox (generated code expects string)
            # Uses full isoformat so HybridNow can parse it back to datetime
            # for .hour/.minute access.  The candle timestamps in the arrays
            # use the shorter 'YYYY-MM-DDTHH:MM' format (via _fmt_ts_compact).
            now_value = validated_current.get('time', '')
            if now_value is None:
                raise ValueError("Current candle time is None - cannot proceed")
            if isinstance(now_value, datetime):
                now = now_value.isoformat()
            elif hasattr(now_value, 'isoformat'):
                now = now_value.isoformat()
            elif hasattr(now_value, 'to_pydatetime'):
                now = now_value.to_pydatetime().isoformat()
            else:
                now = str(now_value)
            
            # Normalize datetime fields in candles to ISO strings for JSON serialization
            def normalize_candle_datetimes(candles_list):
                """Convert datetime objects in candles to ISO strings.
                
                Handles all datetime types: datetime, pd.Timestamp, str, and None.
                Ensures consistent string format for sandbox execution.
                """
                normalized = []
                for candle in candles_list:
                    if isinstance(candle, dict):
                        norm_candle = candle.copy()
                        # Convert datetime to ISO string (handle all types)
                        if 'time' in norm_candle:
                            time_value = norm_candle['time']
                            if time_value is None:
                                # Use empty string if None (sandbox expects string)
                                norm_candle['time'] = ''
                            elif isinstance(time_value, datetime):
                                # Standard datetime object
                                norm_candle['time'] = time_value.isoformat()
                            elif hasattr(time_value, 'to_pydatetime'):
                                # pandas Timestamp - convert to datetime then ISO string
                                norm_candle['time'] = time_value.to_pydatetime().isoformat()
                            elif hasattr(time_value, 'isoformat'):
                                # Objects with isoformat method (e.g., datetime subclasses)
                                norm_candle['time'] = time_value.isoformat()
                            elif isinstance(time_value, str):
                                # Already a string - keep as-is but validate format
                                # If it's a datetime string, ensure it's ISO format
                                try:
                                    # Try parsing to validate it's a valid datetime string
                                    pd.to_datetime(time_value)
                                    norm_candle['time'] = time_value
                                except (ValueError, TypeError):
                                    # Invalid datetime string - convert to ISO if possible
                                    try:
                                        parsed = pd.to_datetime(time_value)
                                        norm_candle['time'] = parsed.isoformat()
                                    except (ValueError, TypeError):
                                        # Can't parse - keep as string
                                        norm_candle['time'] = str(time_value)
                            else:
                                # Unknown type - convert to string, then try to parse and format
                                try:
                                    parsed = pd.to_datetime(str(time_value))
                                    norm_candle['time'] = parsed.isoformat()
                                except (ValueError, TypeError):
                                    norm_candle['time'] = str(time_value)
                        normalized.append(norm_candle)
                    else:
                        normalized.append(candle)
                return normalized
            
            # Normalize datetime fields before passing to sandbox
            candles_5 = normalize_candle_datetimes(candles_5)
            
            # Convert candles from dicts to compact arrays [time, open, high, low, close, volume]
            # GPT generates code expecting array format: candle[0]=time, candle[1]=open, etc.
            def _fmt_ts_compact(raw_ts):
                """Format timestamp to 'YYYY-MM-DDTHH:MM' — same format used
                in GPT prompts (multi_expert._fmt_ts) so generated code sees
                the same timestamp format at analysis and execution time."""
                if raw_ts is None:
                    return ''
                try:
                    if isinstance(raw_ts, str):
                        # Truncate ISO string to minute precision
                        return raw_ts[:16]
                    if hasattr(raw_ts, 'strftime'):
                        return raw_ts.strftime("%Y-%m-%dT%H:%M")
                    if hasattr(raw_ts, 'to_pydatetime'):
                        return raw_ts.to_pydatetime().strftime("%Y-%m-%dT%H:%M")
                    return str(raw_ts)[:16]
                except Exception:
                    return str(raw_ts)[:16]

            def convert_to_compact_arrays(candle_list):
                """Convert candle dicts to compact arrays for sandbox execution.

                Uses the same truncated timestamp format as the GPT prompt builder
                so generated code that parses timestamps works consistently.
                """
                compact = []
                for c in candle_list:
                    if isinstance(c, dict):
                        ts = _fmt_ts_compact(c.get('time', ''))

                        # Extract OHLCV as floats
                        o = float(c.get('open', 0.0)) if c.get('open') is not None else 0.0
                        h = float(c.get('high', 0.0)) if c.get('high') is not None else 0.0
                        l = float(c.get('low', 0.0)) if c.get('low') is not None else 0.0
                        cl = float(c.get('close', 0.0)) if c.get('close') is not None else 0.0
                        v = float(c.get('volume', 0.0)) if c.get('volume') is not None else 0.0

                        compact.append([ts, o, h, l, cl, v])
                    else:
                        # Already in array format
                        compact.append(c)
                return compact
            
            # Convert to compact arrays AFTER datetime normalization
            candles_5 = convert_to_compact_arrays(candles_5)
            
            # Log validation that monitor uses historical candles
            logger.debug(
                f"Code executor: Using historical candles for monitoring (converted to arrays)",
                extra={
                    "monitor_candle_source": "historical_passed_to_execute_generated_code",
                    "historical_count": len(historical_candles),
                    "candle_format": "compact_arrays",
                    "first_candle": candles_5[0] if candles_5 else None,
                    "last_candle": candles_5[-1] if candles_5 else None,
                    "backtest_mode": backtest_mode
                }
            )
            
            # Validate: Must be exactly 41 candles
            if len(candles_5) != 41:
                raise ValueError(
                    f"Expected 41 candles for trigger monitoring "
                    f"(40 historical + 1 current), got {len(candles_5)}"
                )
            
            # Validate: Last candle is the current one to check (compare time values)
            # After conversion to compact arrays, timestamps are truncated to
            # 'YYYY-MM-DDTHH:MM'.  Compare using the same format.
            normalized_current_time = candles_5[40][0] if isinstance(candles_5[40], list) else candles_5[40].get('time')
            original_current_time = current_candle.get('time')

            # Truncate both to the compact format for comparison
            original_current_time_str = _fmt_ts_compact(original_current_time)
            normalized_current_time_str = str(normalized_current_time)[:16] if normalized_current_time is not None else ''

            if normalized_current_time_str != original_current_time_str:
                raise ValueError(
                    f"candles_5[40] must be current candle for trigger check. "
                    f"Expected time: {original_current_time_str}, got: {normalized_current_time_str}"
                )
            
            # Validate code doesn't call non-existent functions (fail fast - no fallback)
            FORBIDDEN_FUNCTIONS = ['entry_trigger_met', 'enhanced_entry_trigger_met']
            for func_name in FORBIDDEN_FUNCTIONS:
                # Check for various call formats: func_name(, func_name (, func_name\t(
                import re
                pattern = rf'\b{func_name}\s*\('
                if re.search(pattern, code):
                    # Log the code snippet for debugging
                    logger.error(
                        f"Generated code contains forbidden function call: {func_name}",
                        extra={
                            "forbidden_function": func_name,
                            "code_length": len(code),
                            "code_snippet": code[:500]
                        }
                    )
                    raise ValueError(
                        f"Generated code calls '{func_name}' which is not available in sandbox. "
                        f"Code must be self-contained using only candles_5 and now parameters."
                    )
            
            # Execute in sandbox with timeout and memory limits
            # NO context injection - code must be self-contained
            raw_result = run_in_sandbox(code, candles_5, now)
            
            # Validate required fields (no fallbacks)
            required = ['direction', 'entry', 'stop_loss', 'take_profit_1', 'confidence', 'rationale']
            missing = [f for f in required if f not in raw_result]
            if missing:
                raise ValueError(f"Generated code missing required fields: {missing}")
            
            # Use raw result directly - NO fallback mapping
            result = raw_result
         
            # Validate result format
            self._validate_result_format(result)
            
            return result
            
        except Exception as e:
            error_type = type(e).__name__
            error_message = str(e)
            
            # Log with more context for different error types
            if 'Broken pipe' in error_message or 'BrokenPipeError' in error_type:
                logger.error(
                    f"CRITICAL: Sandbox worker process communication failed - {error_message}",
                    extra={
                        'error_type': error_type,
                        'code_length': len(code),
                        'current_price': current_candle.get('close', 0),
                        'issue': 'sandbox_worker_failure',
                        'recovery_action': 'fallback_to_flat'
                    }
                )
            elif 'unsupported operand type(s)' in error_message and 'str' in error_message:
                logger.error(
                    f"CRITICAL: Type conversion error in trading code - {error_message}",
                    extra={
                        'error_type': error_type,
                        'code_length': len(code),
                        'current_price': current_candle.get('close', 0),
                        'issue': 'string_arithmetic_error',
                        'recovery_action': 'fallback_to_flat',
                        'fix_applied': 'automatic_type_conversion'
                    }
                )
            elif 'TypeError' in error_type:
                logger.error(
                    f"CRITICAL: Type error in trading code execution - {error_message}",
                    extra={
                        'error_type': error_type,
                        'code_length': len(code),
                        'current_price': current_candle.get('close', 0),
                        'issue': 'type_error',
                        'recovery_action': 'fallback_to_flat'
                    }
                )
            else:
                logger.error(
                    f"Failed to execute trading code in sandbox: {error_message}",
                    extra={
                        'error_type': error_type,
                        'code_length': len(code),
                        'current_price': current_candle.get('close', 0)
                    }
                )
            
            # NO FALLBACK - raise error per no-fallback policy
            raise ValueError(f"Code execution failed: {str(e)}")
    
    def _validate_code_safety(self, code: str) -> None:
        """Validate that the code is safe to execute."""
        
        # Only check for required function - no import restrictions
        if 'def run(' not in code:
            raise ValueError("Code must define 'run' function")
        
        # Check code length (prevent extremely long code)
        if len(code) > 10000:  # 10KB limit
            raise ValueError("Generated code is too long")
    
    def _validate_result_format(self, result: Dict[str, Any]) -> None:
        """Validate that the result has the correct format."""
        
        # Validate required fields (direction and rationale always required)
        if 'direction' not in result:
            raise ValueError("Result missing required field: direction")
        if 'rationale' not in result:
            raise ValueError("Result missing required field: rationale")
        
        # Normalize direction: accept 'buy'/'sell'/'hold' from GPT, convert to standard format
        direction = result.get('direction', '').lower()
        if direction in ['buy']:
            normalized_direction = 'long'
        elif direction in ['sell']:
            normalized_direction = 'short'
        elif direction in ['hold', 'flat']:
            normalized_direction = 'hold'  # Keep as 'hold' for monitoring flow
        elif direction in ['long', 'short']:
            # Already normalized
            normalized_direction = direction
        else:
            raise ValueError(f"Invalid direction: {result.get('direction')}. Must be 'buy'/'sell'/'hold'/'long'/'short'/'flat'")
        
        result['direction'] = normalized_direction
        
        # For 'hold' direction, entry/stop_loss/take_profit can be None
        # For 'buy'/'sell'/'long'/'short', they must be present
        if normalized_direction not in ['hold', 'flat']:
            # Trade signal - require entry details
            required_trade_fields = ['entry', 'stop_loss', 'take_profit_1', 'confidence']
            for field in required_trade_fields:
                if field not in result or result[field] is None:
                    raise ValueError(f"Result missing required trade field: {field} (direction: {normalized_direction})")
            # Auto-derive take_profit_2 if missing (GPT frequently omits it)
            if 'take_profit_2' not in result or result['take_profit_2'] is None:
                result['take_profit_2'] = result.get('take_profit_1')
        
        # Validate numeric fields and cast to float (allow None for 'hold' direction)
        numeric_fields = ['entry', 'stop_loss', 'take_profit_1', 'take_profit_2', 'confidence']
        for field in numeric_fields:
            value = result.get(field)
            # Allow None if direction is 'hold' (no trade triggered yet)
            if value is None and result.get('direction') == 'hold':
                continue
            if value is not None:
                if not isinstance(value, (int, float)):
                    raise ValueError(f"Field '{field}' must be numeric or None (for 'hold'), got {type(value).__name__}")
                # Cast to float so callers always receive a consistent type
                result[field] = float(value)
        
        # Validate confidence range (if provided)
        confidence = result.get('confidence')
        if confidence is not None:
            if not 0.0 <= confidence <= 1.0:
                raise ValueError(f"Confidence must be between 0.0 and 1.0, got {confidence}")
    


def execute_generated_code(
    code: str,
    current_candle: Dict[str, Any],
    historical_candles: List[Dict[str, Any]],
    backtest_mode: bool = False,
    asset: str | None = None,
) -> Dict[str, Any]:
    """
    Execute generated trading code with only candle data.
    
    The code is self-contained and calculates all indicators
    from the provided candles. NO external context is available.
    
    Args:
        code: Generated Python trading code
        current_candle: Current 5-minute candle data
        historical_candles: Last 40 historical candles
        backtest_mode: If True, skip time alignment validation (for historical candles)
        asset: Asset symbol for market-gap awareness (e.g. forex weekend handling)
        
    Returns:
        Trading decision or hold signal
    """
    
    executor = TradingCodeExecutor()
    return executor.execute_trading_code(code, current_candle, historical_candles, backtest_mode=backtest_mode, asset=asset)


__all__ = ['TradingCodeExecutor', 'execute_generated_code']
