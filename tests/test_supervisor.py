import asyncio
from datetime import datetime, timezone
import pytest

import apps.backend.agents.supervisor as sup_mod
from apps.backend.agents import Supervisor


@pytest.mark.asyncio
async def test_run_once_returns_result(monkeypatch):
    async def fake_run(asset, n, cfg):
        return f"{asset}:{n}:{cfg['x']}"

    sup = Supervisor(fake_run)
    cfg = {"N": 2, "x": "y"}

    # avoid real scheduler/session/db interactions
    monkeypatch.setattr(
        sup_mod.scheduler,
        "await_next_bar_close",
        lambda *a, **k: asyncio.sleep(0, result=datetime.now(timezone.utc)),
    )
    monkeypatch.setattr(sup_mod.session, "is_session_open", lambda dt, asset="NSE": True)

    sup.start("AAA", cfg)
    await asyncio.sleep(0)

    res = await sup.run_once("AAA")
    assert res == "AAA:2:y"

    await sup.stop("AAA")


@pytest.mark.asyncio
async def test_start_status_and_stop(monkeypatch):
    event = asyncio.Event()

    async def fake_run(asset, n, cfg):
        await event.wait()

    sup = Supervisor(fake_run)
    cfg = {"N": 1}

    async def _wait(*a, **k):
        await event.wait()
        return datetime.now(timezone.utc)

    monkeypatch.setattr(sup_mod.scheduler, "await_next_bar_close", _wait)
    monkeypatch.setattr(sup_mod.session, "is_session_open", lambda dt, asset="NSE": True)

    sup.start("A", cfg)
    await asyncio.sleep(0)
    assert sup.status("A")["running"] is True

    assert await sup.stop("A") is True
    assert sup.status("A")["running"] is False


@pytest.mark.asyncio
async def test_errors_are_isolated_per_asset(monkeypatch):
    seen = []

    async def fake_run(asset, n, cfg):
        if asset == "bad":
            raise RuntimeError("boom")
        seen.append(asset)

    sup = Supervisor(fake_run)

    monkeypatch.setattr(
        sup_mod.scheduler,
        "await_next_bar_close",
        lambda *a, **k: asyncio.sleep(0, result=datetime.now(timezone.utc)),
    )
    monkeypatch.setattr(sup_mod.session, "is_session_open", lambda dt, asset="NSE": True)

    sup.start("bad", {"N": 1})
    sup.start("good", {"N": 1})
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    error = sup.status("bad")["last_error"]
    assert isinstance(error, RuntimeError)
    assert str(error) == "boom"
    assert sup.status("good")["running"] is True
    assert seen[0] == "good"

    await sup.stop("bad")
    await sup.stop("good")


@pytest.mark.asyncio
async def test_multiple_assets_run_concurrently(monkeypatch):
    events = {"A": asyncio.Event(), "B": asyncio.Event()}

    async def fake_run(asset, n, cfg):
        await asyncio.sleep(cfg["sleep"])
        events[asset].set()

    sup = Supervisor(fake_run)

    monkeypatch.setattr(
        sup_mod.scheduler,
        "await_next_bar_close",
        lambda *a, **k: asyncio.sleep(0, result=datetime.now(timezone.utc)),
    )
    monkeypatch.setattr(sup_mod.session, "is_session_open", lambda dt, asset="NSE": True)

    sup.start("A", {"N": 1, "sleep": 0.2})
    sup.start("B", {"N": 1, "sleep": 0.2})

    start = asyncio.get_event_loop().time()
    await asyncio.gather(events["A"].wait(), events["B"].wait())
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed < 0.4

    await sup.stop("A")
    await sup.stop("B")


@pytest.mark.asyncio
async def test_skip_when_session_closed(monkeypatch):
    called = False

    async def fake_run(asset, n, cfg):
        nonlocal called
        called = True

    sup = Supervisor(fake_run)

    monkeypatch.setattr(
        sup_mod.scheduler,
        "await_next_bar_close",
        lambda *a, **k: asyncio.sleep(0, result=datetime.now(timezone.utc)),
    )
    monkeypatch.setattr(sup_mod.session, "is_session_open", lambda dt, asset="NSE": False)

    sup.start("A", {"N": 1})
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert called is False
    await sup.stop("A")


@pytest.mark.asyncio
async def test_run_once_accepts_cfg_and_stores(monkeypatch):
    async def fake_run(asset, n, cfg):
        return cfg["assistant_id"]

    sup = Supervisor(fake_run)
    monkeypatch.setattr(sup_mod.session, "is_session_open", lambda dt, asset="NSE": True)

    cfg = {
        "N": 1,
        "run_interval": "5m",
        "assistant_id": "aid",
        "broker": "mt5",
        "risk_percent": 2,
    }

    res = await sup.run_once("A", cfg)
    assert res == "aid"
    assert sup._cfg["A"]["run_interval"] == "5m"


@pytest.mark.asyncio
async def test_run_interval_conversion(monkeypatch):
    seen: dict[str, str] = {}

    async def fake_run(asset, n, cfg):
        pass

    async def fake_sched(interval, tz, offset=0):
        seen["interval"] = interval
        await asyncio.sleep(0)
        return datetime.now(timezone.utc)

    sup = Supervisor(fake_run)
    monkeypatch.setattr(sup_mod.scheduler, "await_next_bar_close", fake_sched)
    monkeypatch.setattr(sup_mod.session, "is_session_open", lambda dt, asset="NSE": True)

    sup.start("A", {"N": 1, "run_interval": "hourly"})
    await asyncio.sleep(0)
    assert seen["interval"] == "1h"
    await sup.stop("A")
