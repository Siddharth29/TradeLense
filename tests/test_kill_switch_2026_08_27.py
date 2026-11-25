"""C2 — the kill switch must actually kill, and the per-cycle cleanup must not.

Before 2026-08-27 all four teardown paths resolved the monitor with a SYNC registry
getter that returns None whenever an event loop is running — which is every call
site — so `if monitor is not None:` skipped the body and Stop was a no-op. The same
no-op was the only reason live trading worked, because `cleanup_resources()` runs
in a `finally` after every cycle and would otherwise have destroyed the monitor
seconds after the cycle that armed it.

These tests fail if either half regresses.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import packages.core.services.code_sniper_monitor as csm
from packages.core.services.code_sniper_monitor import (
    CodeSniperMonitor,
    get_code_sniper_monitor_async,
    set_code_sniper_monitor_async,
    shutdown_code_sniper_monitor,
)


class _StubMonitor:
    """Enough of CodeSniperMonitor for the registry to drive it."""

    def __init__(self):
        self.stopped = False
        self.active_count = 3

    async def stop_monitoring(self):
        self.stopped = True


@pytest.fixture(autouse=True)
async def _clean_registry():
    yield
    for agent_id in list((await csm._registry.get_all_monitors()).keys()):
        await csm._registry.remove_monitor(agent_id)


class TestTheLyingGetterIsGone:
    def test_sync_registry_wrappers_no_longer_exist(self):
        """They returned None inside an event loop — the whole bug class."""
        assert not hasattr(csm, "get_code_sniper_monitor"), (
            "the sync getter is back; it returns None inside a running loop and "
            "silently disables every teardown that uses it"
        )
        assert not hasattr(csm, "set_code_sniper_monitor")

    def test_public_api_only_exposes_the_async_accessors(self):
        assert "get_code_sniper_monitor" not in csm.__all__
        assert "set_code_sniper_monitor" not in csm.__all__
        assert "shutdown_code_sniper_monitor" in csm.__all__


class TestShutdownActuallyStops:
    @pytest.mark.asyncio
    async def test_shutdown_stops_and_deregisters(self):
        stub = _StubMonitor()
        await set_code_sniper_monitor_async("XAUUSD", stub)
        assert await get_code_sniper_monitor_async("XAUUSD") is stub

        assert await shutdown_code_sniper_monitor("XAUUSD") is True
        assert stub.stopped is True, "the monitor's own loop was never stopped"
        assert await get_code_sniper_monitor_async("XAUUSD") is None, (
            "monitor still registered — a later cycle would find and reuse it"
        )

    @pytest.mark.asyncio
    async def test_shutdown_is_safe_when_nothing_is_running(self):
        assert await shutdown_code_sniper_monitor("NOTHING") is False

    @pytest.mark.asyncio
    async def test_shutdown_works_from_inside_a_running_loop(self):
        """The exact context the old sync getter silently failed in."""
        assert asyncio.get_running_loop().is_running()
        stub = _StubMonitor()
        await set_code_sniper_monitor_async("BTCUSD", stub)
        assert await shutdown_code_sniper_monitor("BTCUSD") is True
        assert stub.stopped is True


class TestStopPathsAreAwaitable:
    """A coroutine that is never awaited does nothing. All stop paths must be async."""

    def test_supervisor_stop_is_a_coroutine_function(self):
        from apps.backend.agents.supervisor import Supervisor
        assert inspect.iscoroutinefunction(Supervisor.stop)
        assert inspect.iscoroutinefunction(Supervisor.stop_all)

    def test_runner_stop_paths_are_coroutine_functions(self):
        from apps.backend.agents.runner import AgentRunner
        assert inspect.iscoroutinefunction(AgentRunner.stop_continuous_monitoring)
        assert inspect.iscoroutinefunction(AgentRunner.emergency_stop)

    def test_api_awaits_supervisor_stop(self):
        """A bare `supervisor.stop(symbol)` would build a coroutine and drop it."""
        src = Path("apps/backend/api.py").read_text(encoding="utf-8")
        assert "await supervisor.stop(symbol)" in src
        assert "\n        supervisor.stop(symbol)" not in src

    @pytest.mark.asyncio
    async def test_stop_cancels_cycle_and_stops_monitor_registered_under_agent_id(self):
        from apps.backend.agents.supervisor import Supervisor

        started = asyncio.Event()
        cleaned = asyncio.Event()
        executions = []

        async def runner(asset, count, cfg):
            executions.append(asset)
            started.set()
            try:
                await asyncio.Future()
            finally:
                cleaned.set()

        supervisor = Supervisor(runner)
        cfg = {"agent_id": "btcusd_saved_id", "symbol": "BTCUSD", "N": 120}
        monitor = _StubMonitor()
        await set_code_sniper_monitor_async(cfg["agent_id"], monitor)
        cycle = asyncio.create_task(supervisor.run_once("BTCUSD", cfg))
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            await supervisor.run_once("BTCUSD")
            assert executions == ["BTCUSD"]
            assert await supervisor.stop("BTCUSD")
            assert cycle.cancelled()
            assert cleaned.is_set()
            assert monitor.stopped
            assert await get_code_sniper_monitor_async(cfg["agent_id"]) is None
        finally:
            cycle.cancel()
            await asyncio.gather(cycle, return_exceptions=True)


class TestPerCycleCleanupDoesNotOwnTheMonitor:
    """cleanup_resources runs after EVERY cycle. It must not stop the monitor."""

    def test_cleanup_resources_does_not_tear_down_the_monitor(self):
        src = Path("apps/backend/agents/runner.py").read_text(encoding="utf-8")
        start = src.index("    async def cleanup_resources(self) -> None:")
        end = src.index("    def _check_budgets", start)
        # Strip comments — a comment explaining WHY it must not tear down is fine;
        # only an actual call is a regression.
        body = "\n".join(
            line for line in src[start:end].splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "shutdown_code_sniper_monitor" not in body, (
            "cleanup_resources is stopping the strategy monitor again — it runs in "
            "a `finally` after every cycle and would destroy the strategy that "
            "cycle just armed"
        )
        assert "remove_code_sniper_monitor" not in body

    def test_cleanup_resources_is_defined_exactly_once(self):
        """It was defined twice; Python kept the last and the first silently died."""
        src = Path("apps/backend/agents/runner.py").read_text(encoding="utf-8")
        assert src.count("    async def cleanup_resources(self) -> None:") == 1

    def test_dead_strategy_clearing_block_is_gone(self):
        """M1: it read an attribute never assigned, and would have killed the TTL guard."""
        src = Path("apps/backend/agents/runner.py").read_text(encoding="utf-8")
        assert "self, '_code_sniper_monitor'" not in src
        assert 'self, "_code_sniper_monitor"' not in src
