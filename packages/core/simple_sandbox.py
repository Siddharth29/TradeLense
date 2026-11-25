"""Optimized sandbox execution with improved error handling and performance."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import os
from datetime import datetime
from typing import Any, Dict

from .simple_sandbox_worker import WORKER_SOURCE
from .sandbox_payload import ensure_json_serializable

def run_in_sandbox(source: str, candles_5: Any, now: Any) -> Dict[str, Any]:
    """Execute trading strategy code in a sandboxed environment."""

    # Serialize first so a bad request cannot leave a worker waiting for stdin.
    payload = json.dumps({
        "source": source,
        "candles_5": ensure_json_serializable(candles_5),
        "now": now.isoformat() if isinstance(now, datetime) else now,
    })
    
    # Create worker code with optimized preprocessing
    worker_code = WORKER_SOURCE
    
    # Write worker code to temporary file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8') as f:
        f.write(worker_code)
        worker_file = f.name
    
    try:
        # Execute in subprocess
        proc = subprocess.Popen(
            [sys.executable, worker_file],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        
        try:
            stdout, stderr = proc.communicate(input=payload, timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise TimeoutError("Sandbox execution exceeded 10 second timeout")
        
        # Check for errors
        if proc.returncode != 0:
            # Include full stderr for diagnostics, truncate stdout
            err_full = (stderr or "").strip()
            out_snip = (stdout or "").strip()[:200]
            
            # Log detailed error for debugging
            from .logger import get_logger
            logger = get_logger(__name__)
            logger.error(
                f"Sandbox worker process failed with return code {proc.returncode}",
                extra={
                    "returncode": proc.returncode,
                    "stderr": err_full[:1000],  # Capture up to 1000 chars of stderr
                    "stdout_snippet": out_snip
                }
            )
            
            raise RuntimeError(f"Worker process failed (code {proc.returncode}): {err_full[:500] or out_snip}")
        
        if not stdout:
            raise RuntimeError("No output from worker process")
        
        # Parse response
        try:
            resp = json.loads(stdout.strip())
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Invalid JSON response: {stdout[:200]}")
        
        # Handle errors
        if "error" in resp:
            error_type = resp["error"].get("type", "RuntimeError")
            error_msg = resp["error"].get("message", "Unknown error")
            
            if error_type == "TimeoutError":
                raise TimeoutError(error_msg)
            elif error_type == "MemoryError":
                raise MemoryError(error_msg)
            elif error_type == "PermissionError":
                raise PermissionError(error_msg)
            else:
                raise RuntimeError(error_msg)
        
        # Return result
        if "result" not in resp:
            raise RuntimeError(f"Invalid response format: missing 'result' field")
        
        return resp["result"]
        
    finally:
        # Cleanup temporary file
        try:
            os.unlink(worker_file)
        except OSError:
            pass

__all__ = ["run_in_sandbox"]
