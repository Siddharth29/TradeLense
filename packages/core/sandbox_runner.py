"""Run strategy code in a restricted sandbox."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from typing import Any, Dict

from .models import Decision
from .config import SANDBOX_TIMEOUT, SANDBOX_MEMORY, SANDBOX_MAX_WORKER_RESTARTS

_TIMEOUT = SANDBOX_TIMEOUT
_MEMORY = SANDBOX_MEMORY
_MAX_WORKER_RESTARTS = SANDBOX_MAX_WORKER_RESTARTS

# Code executed in the worker process.  It reads JSON lines from stdin and
# writes JSON lines to stdout.  The strategy code must define a ``run``
# function returning a mapping compatible with :class:`Decision`.
_WORKER_CODE = f"""
import json, sys, builtins, os, socket, random, threading, numpy as np, numpy.random as nprand
from datetime import datetime as _datetime

# Memory limit: only available on Linux/macOS via resource module.
try:
    import resource
    resource.setrlimit(resource.RLIMIT_AS, ({_MEMORY}, {_MEMORY}))
except (ImportError, ValueError, OSError):
    pass  # Windows or unprivileged — skip memory cap

# Helper function for trading code - tfloat is an alias for float
def tfloat(value):
    \"\"\"Trading float conversion - alias for float() for compatibility.\"\"\"
    return float(value)

# Disallow file system access.
def _deny_fs(*a, **k):
    raise PermissionError("file system access denied")
builtins.open = _deny_fs
os.open = _deny_fs
os.listdir = _deny_fs
os.stat = _deny_fs

# Disallow network access.
def _deny_net(*a, **k):
    raise PermissionError("network access denied")
socket.socket = _deny_net
socket.create_connection = _deny_net

# Deterministic random generators.
_rand_seeded = False
_rand_orig_seed = random.seed
_rand_orig_random = random.random

def _rseed(v=None, *a, **k):
    global _rand_seeded
    if v is None:
        raise RuntimeError("random.seed() requires a value")
    _rand_seeded = True
    return _rand_orig_seed(v, *a, **k)

def _rrandom(*a, **k):
    if not _rand_seeded:
        raise RuntimeError("random generator used before seeding")
    return _rand_orig_random(*a, **k)

random.seed = _rseed
random.random = _rrandom

_np_rand = nprand
_np_seeded = False
_np_orig_seed = _np_rand.seed
_np_orig_random = _np_rand.random

def _npseed(v=None, *a, **k):
    global _np_seeded
    if v is None:
        raise RuntimeError("numpy.random.seed() requires a value")
    _np_seeded = True
    return _np_orig_seed(v, *a, **k)

def _nprandom(*a, **k):
    if not _np_seeded:
        raise RuntimeError("numpy.random generator used before seeding")
    return _np_orig_random(*a, **k)

_np_rand.seed = _npseed
_np_rand.random = _nprandom

random.seed(0)
_np_rand.seed(0)

# Cross-platform timeout using a daemon thread + threading.Event.
# signal.SIGALRM is Linux-only and unavailable on Windows.
class _TimeoutError(Exception):
    pass

def _run_with_timeout(fn, timeout_secs):
    \"\"\"Run fn() in the current thread, raising _TimeoutError if it exceeds timeout_secs.\"\"\"
    result_holder = [None]
    exc_holder = [None]
    done_event = threading.Event()

    def _target():
        try:
            result_holder[0] = fn()
        except Exception as e:
            exc_holder[0] = e
        finally:
            done_event.set()

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    finished = done_event.wait(timeout=timeout_secs)
    if not finished:
        raise _TimeoutError("timeout")
    if exc_holder[0] is not None:
        raise exc_holder[0]
    return result_holder[0]

while True:
    line = sys.stdin.readline()
    if not line:
        break
    req = json.loads(line)
    namespace = {{}}
    try:
        def _execute():
            exec(req['source'], namespace)
            _now_raw = req['now']
            namespace['now_str'] = _now_raw
            namespace['tfloat'] = tfloat
            try:
                _iso = _now_raw.replace('Z', '+00:00') if isinstance(_now_raw, str) else str(_now_raw)
                namespace['now_dt'] = _datetime.fromisoformat(_iso)
            except Exception:
                namespace['now_dt'] = _now_raw
            return namespace['run'](req['candles_5'], req['now'])

        result = _run_with_timeout(_execute, {_TIMEOUT})
        print(json.dumps({{'result': result}}))
    except _TimeoutError:
        print(json.dumps({{'error': {{'type': 'TimeoutError', 'message': 'timeout'}}}}))
    except Exception as e:
        print(json.dumps({{'error': {{'type': e.__class__.__name__, 'message': str(e)}}}}))
    sys.stdout.flush()
"""

_worker = None
_worker_restart_count = 0

def _get_worker() -> subprocess.Popen:
    global _worker, _worker_restart_count
    
    if _worker is None or _worker.poll() is not None:
        # Check restart limit
        if _worker_restart_count >= _MAX_WORKER_RESTARTS:
            raise RuntimeError(f"Maximum worker restart limit ({_MAX_WORKER_RESTARTS}) exceeded")
        
        # Clean up any existing dead worker and capture stderr for debugging
        if _worker is not None:
            try:
                _worker.terminate()
                _worker.wait(timeout=1)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    _worker.kill()
                except OSError:
                    pass
            finally:
                pass
        
        cmd = [sys.executable, "-u", "-c", _WORKER_CODE]
        try:
            _worker = subprocess.Popen(
                cmd, 
                stdin=subprocess.PIPE, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.PIPE,
                text=True,
                bufsize=0  # Unbuffered for immediate communication
            )
            _worker_restart_count += 1
        except Exception as e:
            raise RuntimeError(f"Failed to start sandbox worker: {str(e)}")
    
    return _worker


def run_in_sandbox(source: str, candles_5: Any, now: Any, context: Any = None) -> Dict[str, Any]:
    """Execute *source* in a restricted worker and return a dictionary result.

    Parameters
    ----------
    source:
        Strategy Python source code defining ``run(candles_5, now)``.
    candles_5:
        Strategy input data passed to ``run``.
    now:
        Current timestamp supplied to ``run``.
    context:
        Additional context data (not used in sandbox but kept for compatibility).
    """

    # Import checks removed - allow all imports
    global _worker, _worker_restart_count
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            proc = _get_worker()
            # CRITICAL FIX: Ensure all candle data has numeric types before sending to sandbox
            def ensure_numeric_candles(candles_5):
                if not isinstance(candles_5, list):
                    return candles_5
                
                converted_candles = []
                for candle in candles_5:
                    if isinstance(candle, dict):
                        converted_candle = candle.copy()
                        # Convert all numeric fields to float to prevent str-str arithmetic errors
                        numeric_fields = ['open', 'high', 'low', 'close', 'volume']
                        for field in numeric_fields:
                            if field in converted_candle and converted_candle[field] is not None:
                                try:
                                    # Handle string numbers like "122065.0"
                                    if isinstance(converted_candle[field], str):
                                        clean_value = converted_candle[field].strip('"\' ')
                                        converted_candle[field] = float(clean_value)
                                    else:
                                        converted_candle[field] = float(converted_candle[field])
                                except (ValueError, TypeError):
                                    # Set to 0.0 if conversion fails to prevent type errors
                                    converted_candle[field] = 0.0
                        converted_candles.append(converted_candle)
                    else:
                        converted_candles.append(candle)
                return converted_candles
            
            # Apply type conversion before sending to sandbox
            safe_candles_5 = ensure_numeric_candles(candles_5)
            
            # Convert datetime objects to ISO strings for JSON serialization
            def serialize_datetime(obj):
                """Convert datetime objects to ISO format strings."""
                if isinstance(obj, datetime):
                    return obj.isoformat()
                return obj
            
            def ensure_json_serializable(candles_5):
                """Ensure all candle data is JSON serializable."""
                if not isinstance(candles_5, list):
                    return candles_5
                
                serialized_candles = []
                for candle in candles_5:
                    if isinstance(candle, dict):
                        serialized_candle = {}
                        for key, value in candle.items():
                            # Convert datetime to ISO string (handle None)
                            if value is None:
                                serialized_candle[key] = None
                            elif isinstance(value, datetime):
                                serialized_candle[key] = value.isoformat()
                            else:
                                serialized_candle[key] = value
                        serialized_candles.append(serialized_candle)
                    elif isinstance(candle, list):
                        # Handle array format [time, open, high, low, close, volume]
                        serialized_candle = []
                        for item in candle:
                            if item is None:
                                serialized_candle.append(None)
                            elif isinstance(item, datetime):
                                serialized_candle.append(item.isoformat())
                            else:
                                serialized_candle.append(item)
                        serialized_candles.append(serialized_candle)
                    else:
                        serialized_candles.append(candle)
                return serialized_candles
            
            # Ensure datetime objects are serialized
            safe_candles_5 = ensure_json_serializable(safe_candles_5)
            
            # Convert 'now' to string if it's a datetime object
            if isinstance(now, datetime):
                now = now.isoformat()
            
            payload = json.dumps({"source": source, "candles_5": safe_candles_5, "now": now})
            proc.stdin.write(payload + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError("sandbox worker terminated")
            
            resp = json.loads(line)
            if "error" in resp:
                typ = resp["error"].get("type", "RuntimeError")
                msg = resp["error"].get("message", "")
                if typ == "TimeoutError":
                    raise TimeoutError(msg)
                if typ == "MemoryError":
                    raise MemoryError(msg)
                if typ == "PermissionError":
                    raise PermissionError(msg)
                raise RuntimeError(msg)
            result_dict: Dict[str, Any] = resp["result"]
            # Reset restart counter on successful execution
            _worker_restart_count = 0
            return result_dict
            
        except BrokenPipeError as e:
            # Worker process died unexpectedly - reset and retry
            _worker = None
            if attempt < max_retries - 1:
                continue
            raise RuntimeError(f"Sandbox worker process failed after {max_retries} attempts: {str(e)}")
        except (OSError, ConnectionError) as e:
            # Handle other pipe-related errors
            _worker = None
            if attempt < max_retries - 1:
                continue
            raise RuntimeError(f"Sandbox communication failed after {max_retries} attempts: {str(e)}")
        except Exception as e:
            # Catch any other unexpected errors
            _worker = None
            if attempt < max_retries - 1:
                continue
            raise RuntimeError(f"Sandbox execution failed after {max_retries} attempts: {str(e)}")

__all__ = ["run_in_sandbox"]
