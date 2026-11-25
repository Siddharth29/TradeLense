"""Sequential dev-startup agent launcher (used by start_dev.bat).

Flow:
1. Wait for the backend to answer GET /health.
2. Wait for MT5 to report connected via GET /brokers/mt5/status
   (the backend auto-connects from the local cred cache at startup).
3. Start each agent in AGENTS order with POST /agents/<id>/run-now?background=true,
   which acknowledges "started" immediately while the first cycle runs
   server-side, then confirm GET /agents/<id>/status reports running=true
   before moving on to the next agent.

Every HTTP call carries its own timeout, so a wedged backend can never hang
this launcher: worst case an agent is reported FAILED and the rest still
get their start attempts.

Environment:
  API_URL                 backend base URL (default http://127.0.0.1:8000)
  AGENTS                  space/comma-separated agent ids, started in order.
                          If unset, ALL agents from GET /agents are started
                          (careful: that would also start benched agents).
  BACKEND_HEALTH_TIMEOUT  seconds to wait for /health (default 180)
  MT5_CONNECT_TIMEOUT     seconds to wait for MT5 connected (default 180)

Stdlib only - no repo imports, safe to run from any cwd.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

API_URL = os.environ.get("API_URL", "http://127.0.0.1:8000").rstrip("/")
BACKEND_HEALTH_TIMEOUT = float(os.environ.get("BACKEND_HEALTH_TIMEOUT", "180"))
MT5_CONNECT_TIMEOUT = float(os.environ.get("MT5_CONNECT_TIMEOUT", "180"))
MT5_MODE = os.environ.get("TRADELENS_MT5_MODE", "").strip().lower()
START_ACK_TIMEOUT = 90.0  # run-now?background=true acks after cfg/services prep
CONFIRM_RUNNING_TIMEOUT = 30.0
POLL_INTERVAL = 2.0


def _request(method: str, path: str, timeout: float):
    """One HTTP round-trip. Returns (status_code, parsed_json_or_None)."""
    req = urllib.request.Request(API_URL + path, method=method)
    if method == "POST":
        req.add_header("Content-Length", "0")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        try:
            return resp.status, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return resp.status, None


def _wait_until(desc: str, probe, timeout: float) -> bool:
    """Poll ``probe`` (returns bool, may raise) until True or timeout."""
    deadline = time.monotonic() + timeout
    last_error = "no attempt made"
    while time.monotonic() < deadline:
        try:
            if probe():
                return True
            last_error = "probe returned False"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        remaining = max(0, int(deadline - time.monotonic()))
        print(f"  waiting for {desc}... ({remaining}s left)")
        time.sleep(POLL_INTERVAL)
    print(f"[FAIL] timed out after {int(timeout)}s waiting for {desc} (last: {last_error})")
    return False


def _backend_healthy() -> bool:
    status, _ = _request("GET", "/health", timeout=5)
    return status == 200


def _mt5_connected() -> bool:
    status, body = _request("GET", "/brokers/mt5/status", timeout=10)
    return status == 200 and bool(
        body and body.get("connected") and (not MT5_MODE or body.get("mode") == MT5_MODE)
    )


def _resolve_roster() -> list[str]:
    raw = os.environ.get("AGENTS", "").replace(",", " ").split()
    if raw:
        return raw
    print("AGENTS not set - falling back to ALL agents from GET /agents")
    print("(note: this also starts benched agents; set AGENTS to control the roster)")
    _, body = _request("GET", "/agents", timeout=15)
    return [a["id"] for a in (body or {}).get("agents", [])]


def _start_agent(agent: str) -> bool:
    """Start one agent and confirm the backend acknowledged it."""
    print(f"-> starting {agent} (run-now, background mode)")
    try:
        status, body = _request(
            "POST", f"/agents/{agent}/run-now?background=true", timeout=START_ACK_TIMEOUT
        )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        print(f"[FAIL] {agent}: run-now HTTP {exc.code}: {detail}")
        return False
    except Exception as exc:
        print(f"[FAIL] {agent}: run-now request failed: {type(exc).__name__}: {exc}")
        return False
    if status != 200 or not body or body.get("status") != "started":
        print(f"[FAIL] {agent}: unexpected run-now response (HTTP {status}): {body}")
        return False

    def _running() -> bool:
        st, sb = _request("GET", f"/agents/{agent}/status", timeout=10)
        return st == 200 and bool(sb and sb.get("running"))

    if not _wait_until(f"{agent} status=running", _running, CONFIRM_RUNNING_TIMEOUT):
        return False
    print(f"[OK] {agent} started and confirmed running")
    return True


def main() -> int:
    print(f"Backend: {API_URL}")

    print("Step 1/3: waiting for backend health...")
    if not _wait_until("backend /health", _backend_healthy, BACKEND_HEALTH_TIMEOUT):
        return 1
    print("[OK] backend is up")

    print("Step 2/3: waiting for MT5 auto-connect...")
    if not _wait_until("MT5 connected", _mt5_connected, MT5_CONNECT_TIMEOUT):
        print("MT5 did not connect - NOT starting agents (they need the broker).")
        return 1
    print("[OK] MT5 connected")

    try:
        roster = _resolve_roster()
    except Exception as exc:
        print(f"[FAIL] could not resolve agent roster: {type(exc).__name__}: {exc}")
        return 1
    if not roster:
        print("[FAIL] agent roster is empty - nothing to start")
        return 1

    print(f"Step 3/3: starting {len(roster)} agents in sequence: {' '.join(roster)}")
    results: dict[str, bool] = {}
    for agent in roster:
        results[agent] = _start_agent(agent)

    print("")
    print("=== Agent startup summary ===")
    for agent, ok in results.items():
        print(f"  {agent}: {'OK' if ok else 'FAILED'}")
    failed = [a for a, ok in results.items() if not ok]
    if failed:
        print("")
        print(f"{len(failed)} agent(s) FAILED to start: {' '.join(failed)}")
        print("Check the Backend window / results\\logs for details.")
        return 1
    print("")
    print("All agents started.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
