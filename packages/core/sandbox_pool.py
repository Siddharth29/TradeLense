"""Robust sandbox execution with worker process pool."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Optional
from concurrent.futures import ThreadPoolExecutor
import queue

from .models import Decision
from .config import SANDBOX_TIMEOUT, SANDBOX_MEMORY, SANDBOX_POOL_SIZE

_TIMEOUT = SANDBOX_TIMEOUT
_MEMORY = SANDBOX_MEMORY
_POOL_SIZE = SANDBOX_POOL_SIZE

# Code executed in the worker process
_WORKER_CODE = f"""
import json, sys, resource, signal, builtins, os, socket, random, numpy as np, numpy.random as nprand

resource.setrlimit(resource.RLIMIT_AS, ({_MEMORY}, {_MEMORY}))

# Disallow file system access
def _deny_fs(*a, **k):
    raise PermissionError("file system access denied")
builtins.open = _deny_fs
os.open = _deny_fs
os.listdir = _deny_fs
os.stat = _deny_fs

# Disallow network access
def _deny_net(*a, **k):
    raise PermissionError("network access denied")
socket.socket = _deny_net
socket.create_connection = _deny_net

# Deterministic random generators
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

# Timeout handling using signals
def _on_timeout(signum, frame):
    raise TimeoutError("timeout")
signal.signal(signal.SIGALRM, _on_timeout)

while True:
    line = sys.stdin.readline()
    if not line:
        break
    req = json.loads(line)
    namespace = {{}}
    try:
        signal.alarm({_TIMEOUT})
        exec(req['source'], namespace)
        result = namespace['run'](req['candles_5'], req['now'])
        print(json.dumps({{'result': result}}))
        sys.stdout.flush()
    except Exception as e:
        print(json.dumps({{'error': {{'type': e.__class__.__name__, 'message': str(e)}}}}))
        sys.stdout.flush()
    finally:
        signal.alarm(0)
"""


class WorkerProcess:
    """Individual worker process wrapper."""
    
    def __init__(self, worker_id: int):
        self.worker_id = worker_id
        self.process: Optional[subprocess.Popen] = None
        self.is_healthy = False
        self.last_used = time.time()
        self._lock = threading.Lock()
    
    def start(self) -> None:
        """Start the worker process."""
        with self._lock:
            if self.process is not None:
                self.cleanup()
            
            cmd = [sys.executable, "-u", "-c", _WORKER_CODE]
            try:
                self.process = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=0
                )
                self.is_healthy = True
                self.last_used = time.time()
            except Exception as e:
                self.is_healthy = False
                raise RuntimeError(f"Failed to start worker {self.worker_id}: {str(e)}")
    
    def cleanup(self) -> None:
        """Clean up the worker process."""
        with self._lock:
            if self.process is not None:
                try:
                    self.process.terminate()
                    self.process.wait(timeout=1)
                except (subprocess.TimeoutExpired, OSError):
                    try:
                        self.process.kill()
                    except OSError:
                        pass
                finally:
                    self.process = None
                    self.is_healthy = False
    
    def is_alive(self) -> bool:
        """Check if the process is alive."""
        return self.process is not None and self.process.poll() is None
    
    def execute(self, source: str, candles_5: Any, now: Any) -> Dict[str, Any]:
        """Execute code in this worker."""
        with self._lock:
            if not self.is_healthy or not self.is_alive():
                raise RuntimeError(f"Worker {self.worker_id} is not healthy")
            
            try:
                payload = json.dumps({"source": source, "candles_5": candles_5, "now": now})
                self.process.stdin.write(payload + "\n")
                self.process.stdin.flush()
                line = self.process.stdout.readline()
                
                if not line:
                    self.is_healthy = False
                    raise RuntimeError(f"Worker {self.worker_id} terminated unexpectedly")
                
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
                
                self.last_used = time.time()
                return resp["result"]
                
            except (BrokenPipeError, OSError, ConnectionError) as e:
                self.is_healthy = False
                # Try to capture stderr for debugging
                try:
                    if self.process and self.process.stderr:
                        stderr_output = self.process.stderr.read()
                        if stderr_output:
                            print(f"DEBUG: Worker {self.worker_id} stderr: {stderr_output}")
                except Exception:
                    pass
                raise RuntimeError(f"Worker {self.worker_id} communication failed: {str(e)}")


class SandboxPool:
    """Pool of worker processes for sandbox execution."""
    
    def __init__(self, pool_size: int = _POOL_SIZE):
        self.pool_size = pool_size
        self.workers: list[WorkerProcess] = []
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=pool_size)
        self._initialize_workers()
    
    def _initialize_workers(self) -> None:
        """Initialize the worker pool."""
        with self._lock:
            self.workers = []
            for i in range(self.pool_size):
                worker = WorkerProcess(i)
                try:
                    worker.start()
                    self.workers.append(worker)
                except Exception as e:
                    print(f"Warning: Failed to start worker {i}: {e}")
    
    def get_healthy_worker(self) -> Optional[WorkerProcess]:
        """Get a healthy worker from the pool."""
        with self._lock:
            # Try to find a healthy worker
            for worker in self.workers:
                if worker.is_healthy and worker.is_alive():
                    return worker
            
            # If no healthy worker found, try to restart one
            for worker in self.workers:
                if not worker.is_healthy or not worker.is_alive():
                    try:
                        worker.start()
                        return worker
                    except Exception as e:
                        print(f"Warning: Failed to restart worker {worker.worker_id}: {e}")
                        continue
            
            return None
    
    def execute(self, source: str, candles_5: Any, now: Any, context: Any = None) -> Dict[str, Any]:
        """Execute code in the sandbox pool."""
        max_retries = 3
        
        for attempt in range(max_retries):
            worker = self.get_healthy_worker()
            if worker is None:
                raise RuntimeError("No healthy workers available in pool")
            
            try:
                return worker.execute(source, candles_5, now)
            except Exception as e:
                if attempt < max_retries - 1:
                    print(f"Worker execution failed, retrying: {e}")
                    continue
                raise RuntimeError(f"Sandbox execution failed after {max_retries} attempts: {str(e)}")
    
    def cleanup(self) -> None:
        """Clean up all workers."""
        with self._lock:
            for worker in self.workers:
                worker.cleanup()
            self._executor.shutdown(wait=True)


# Global pool instance
_pool: Optional[SandboxPool] = None
_pool_lock = threading.Lock()


def run_in_sandbox(source: str, candles_5: Any, now: Any, context: Any = None) -> Dict[str, Any]:
    """Execute code in sandbox using worker pool."""
    global _pool
    
    with _pool_lock:
        if _pool is None:
            _pool = SandboxPool()
    
    return _pool.execute(source, candles_5, now, context)


def cleanup_sandbox_pool() -> None:
    """Clean up the sandbox pool (call this on shutdown)."""
    global _pool
    
    with _pool_lock:
        if _pool is not None:
            _pool.cleanup()
            _pool = None


__all__ = ["run_in_sandbox", "cleanup_sandbox_pool"]
