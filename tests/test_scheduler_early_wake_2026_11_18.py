"""An early monotonic wake must not discard the pending hourly slot."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from apps.backend.agents import supervisor as module


@pytest.mark.asyncio
async def test_supervisor_waits_for_actual_scheduled_time_and_runs_slot_once(monkeypatch):
    planned = datetime.now(timezone.utc) + timedelta(milliseconds=40)
    done = asyncio.Event()
    blocked = asyncio.Event()
    seen = []
    schedules = 0

    async def early_schedule(*args):
        nonlocal schedules
        schedules += 1
        if schedules == 1:
            return planned
        await blocked.wait()

    async def runner(asset, count, cfg):
        seen.append((datetime.now(timezone.utc), cfg["scheduled_time"]))
        done.set()

    monkeypatch.setattr(module.scheduler, "await_next_bar_close", early_schedule)
    monkeypatch.setattr(module.session, "is_session_open", lambda *args: True)
    supervisor = module.Supervisor(runner)
    supervisor.start("XAGUSD", {"run_interval": "1h", "N": 1})
    try:
        await asyncio.wait_for(done.wait(), timeout=1)
        assert len(seen) == 1
        assert seen[0][0] >= planned
        assert seen[0][1] == planned
    finally:
        await supervisor.stop("XAGUSD")


@pytest.mark.asyncio
async def test_stop_cancels_early_wake_wait_without_running_slot(monkeypatch):
    pending = asyncio.Event()
    called = []

    async def early_schedule(*args):
        pending.set()
        return datetime.now(timezone.utc) + timedelta(hours=1)

    async def runner(*args):
        called.append(True)

    monkeypatch.setattr(module.scheduler, "await_next_bar_close", early_schedule)
    supervisor = module.Supervisor(runner)
    supervisor.start("XAGUSD", {"run_interval": "1h", "N": 1})
    await pending.wait()
    assert await asyncio.wait_for(supervisor.stop("XAGUSD"), timeout=1)
    assert not called
