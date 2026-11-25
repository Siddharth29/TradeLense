import asyncio
import pytest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import core.scheduler as sched


def test_await_next_bar_close_alignment(monkeypatch):
    tz = "Asia/Kolkata"
    now = datetime(2024, 1, 1, 10, 2, 30, tzinfo=ZoneInfo(tz))

    class DummyDateTime:
        @staticmethod
        def now(tz=None):
            return now

        @staticmethod
        def fromtimestamp(ts, tz=None):
            return datetime.fromtimestamp(ts, tz)

    current = 0

    async def fake_sleep(delay):
        nonlocal current
        current += delay

    def fake_monotonic():
        return current

    monkeypatch.setattr(sched, "datetime", DummyDateTime)
    monkeypatch.setattr(sched.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(sched.time, "monotonic", fake_monotonic)

    planned = asyncio.run(sched.await_next_bar_close("5m", tz))

    assert current == pytest.approx(150)
    assert planned.tzinfo == ZoneInfo(tz)
    assert planned.minute % 5 == 0
    assert planned.second == 0


def test_await_next_bar_close_offset(monkeypatch):
    tz = "Asia/Kolkata"
    now = datetime(2024, 1, 1, 10, 20, tzinfo=ZoneInfo(tz))

    class DummyDateTime:
        @staticmethod
        def now(tz=None):
            return now

        @staticmethod
        def fromtimestamp(ts, tz=None):
            return datetime.fromtimestamp(ts, tz)

    current = 0

    async def fake_sleep(delay):
        nonlocal current
        current += delay

    def fake_monotonic():
        return current

    monkeypatch.setattr(sched, "datetime", DummyDateTime)
    monkeypatch.setattr(sched.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(sched.time, "monotonic", fake_monotonic)

    planned = asyncio.run(sched.await_next_bar_close("1h", tz, offset=1800))

    assert current == pytest.approx(600)
    assert planned.hour == 10 and planned.minute == 30


def test_await_next_bar_close_boundary(monkeypatch):
    tz = "Asia/Kolkata"
    now = datetime(2024, 1, 1, 9, 30, tzinfo=ZoneInfo(tz))

    class DummyDateTime:
        @staticmethod
        def now(tz=None):
            return now

        @staticmethod
        def fromtimestamp(ts, tz=None):
            return datetime.fromtimestamp(ts, tz)

    current = 0

    async def fake_sleep(delay):
        nonlocal current
        current += delay

    def fake_monotonic():
        return current

    monkeypatch.setattr(sched, "datetime", DummyDateTime)
    monkeypatch.setattr(sched.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(sched.time, "monotonic", fake_monotonic)

    planned = asyncio.run(sched.await_next_bar_close("1h", tz, offset=1800))

    assert current == pytest.approx(0)
    assert planned.hour == 9 and planned.minute == 30
