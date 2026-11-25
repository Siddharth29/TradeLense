"""Worker program for simple_sandbox. Transport and policy are separate concerns.

This source is preserved verbatim during extraction; it is process isolation,
not an OS-enforced security boundary.
"""

WORKER_SOURCE = '''# -*- coding: utf-8 -*-
import json
import sys
import signal
import re

class HybridNow(str):
    __slots__ = ('_dt',)

    def __new__(cls, value, dt):
        if dt is None:
            raise ValueError("Datetime value required for 'now'")
        if value is None:
            value = dt.isoformat()
        obj = str.__new__(cls, value)
        obj._dt = dt
        return obj

    @property
    def hour(self):
        return self._dt.hour

    @property
    def minute(self):
        return self._dt.minute

    def isoformat(self):
        return self._dt.isoformat()

    def to_datetime(self):
        return self._dt

def timeout_handler(signum, frame):
    raise TimeoutError("Execution timeout")

# Set up timeout (Unix-like systems only)
if hasattr(signal, 'SIGALRM'):
    signal.signal(signal.SIGALRM, timeout_handler)

def convert_candle_fields(candles):
    """Convert candle data to proper numeric types."""
    if not isinstance(candles, list):
        return candles
    
    converted = []
    numeric_fields = ['open', 'high', 'low', 'close', 'volume']
    
    for candle in candles:
        if isinstance(candle, dict):
            conv_candle = {}
            for key, value in candle.items():
                if key in numeric_fields and value is not None:
                    try:
                        # Handle string numbers like "122065.0"
                        conv_candle[key] = float(str(value).strip('"\\\''))
                    except (ValueError, TypeError):
                        conv_candle[key] = 0.0
                else:
                    conv_candle[key] = value
            converted.append(conv_candle)
        else:
            converted.append(candle)
    
    return converted

def preprocess_source(source_code):
    """Optimize source code by adding automatic type conversions.

    Only wraps candle-specific accesses with float() — variables that
    are clearly candle references (candles_5[...]['field']).
    Does NOT touch arbitrary dict accesses or standalone assignments.
    """
    # Only match candles_5[...]['open'|'high'|'low'|'close'|'volume']
    # This avoids wrapping non-candle dicts like config['close']
    pattern = r"(?<!float\\()\\b(candles_5\\[[^\\]]+\\])(\\['(?:open|high|low|close|volume)'\\])"

    def wrap_with_float(match):
        obj, field = match.groups()
        return f"float({obj}{field})"

    processed = re.sub(pattern, wrap_with_float, source_code)

    # Do NOT replace standalone candle assignments (var = candles_5[i])
    # as the code may legitimately want the entire candle dict.

    return processed

try:
    # Read input
    line = sys.stdin.readline().strip()
    if not line:
        sys.exit(1)
    
    req = json.loads(line)
    
    # Set timeout (5 seconds)
    if hasattr(signal, 'alarm'):
        signal.alarm(5)
    
    # Preprocess and execute code
    processed_source = preprocess_source(req['source'])
    
    namespace = {
        'tfloat': float,  # Compatibility alias
        '__builtins__': __builtins__,
        # Fallback candle variables — GPT code sometimes references these at
        # module level (before run() is called).  Providing empty lists prevents
        # NameError when the caller only supplies candles_5.
        'candles_15m': [],
        'candles_5m': [],
        'candles_1m': [],
    }

    exec(processed_source, namespace)
    
    if 'run' not in namespace:
        raise RuntimeError("No 'run' function found in source code")
    
    # Convert candle data and execute
    converted_candles = convert_candle_fields(req['candles_5'])
    
    # Provide 'now' as BOTH string and datetime to support all generated code patterns
    # Generated code may use: now.split() (string) OR now.hour/.minute (datetime)
    from datetime import datetime
    now_param = req['now']
    
    # Parse to datetime if needed
    now_dt = None
    now_str = None
    
    if isinstance(now_param, datetime):
        now_dt = now_param
        now_str = now_param.isoformat()
    elif isinstance(now_param, str):
        now_str = now_param
        try:
            # Try to parse ISO format
            now_dt = datetime.fromisoformat(now_param.replace('Z', '+00:00'))
        except Exception as parse_error:
            raise ValueError(f"Invalid datetime string for 'now': {now_param}") from parse_error
    else:
        raise TypeError(f"Unsupported type for 'now': {type(now_param).__name__}")
    
    hybrid_now = HybridNow(now_str, now_dt)
    
    # Add both formats to namespace for generated code
    namespace['now'] = hybrid_now  # Hybrid object supports string ops and datetime access
    namespace['now_str'] = now_str
    namespace['now_dt'] = now_dt  # Datetime format for .hour, .minute access
    
    # Also add helper function for backward compatibility
    def parse_datetime(iso_string):
        """Helper to parse ISO datetime string if needed."""
        try:
            return datetime.fromisoformat(iso_string.replace('Z', '+00:00'))
        except:
            return None
    
    namespace['parse_datetime'] = parse_datetime
    
    # Pass hybrid object to preserve both string and datetime behavior
    result = namespace['run'](converted_candles, hybrid_now)
    
    print(json.dumps({'result': result}))
    
except TimeoutError as e:
    print(json.dumps({'error': {'type': 'TimeoutError', 'message': str(e)}}))
except MemoryError as e:
    print(json.dumps({'error': {'type': 'MemoryError', 'message': str(e)}}))
except Exception as e:
    error_msg = str(e)
    if isinstance(e, TypeError) and 'unsupported operand' in error_msg and 'str' in error_msg:
        error_msg = f"Type error: {error_msg}. Use candles[i]['close'] to access numeric values."
    print(json.dumps({'error': {'type': e.__class__.__name__, 'message': error_msg}}))
finally:
    if hasattr(signal, 'alarm'):
        signal.alarm(0)
'''
