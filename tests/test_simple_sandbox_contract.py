"""Valid-time sandbox behavior and transport; synthetic strategies only."""

import json
from datetime import datetime, timezone

import pytest

from packages.core.simple_sandbox import run_in_sandbox


def test_worker_preserves_hybrid_time_and_numeric_candle_inputs():
    source = """
def run(candles_5, now):
    return {'hour': now.hour, 'minute': now.minute, 'text': now.split('T')[0],
            'iso': now.isoformat(), 'close': candles_5[-1]['close'],
            'empty_higher': len(candles_15m)}
"""
    result = run_in_sandbox(source, [{"time": datetime(2026, 7, 1, tzinfo=timezone.utc), "close": "100.5"}], datetime(2026, 7, 1, 10, 5, tzinfo=timezone.utc))
    assert result == {"hour": 10, "minute": 5, "text": "2026-07-01", "iso": "2026-07-01T10:05:00+00:00", "close": 100.5, "empty_higher": 0}


@pytest.mark.parametrize("source,now,message", [
    ("x = 1", "2026-07-01T10:00:00", "No 'run' function"),
    ("def run(candles_5, now): return {}", "invalid", "Invalid datetime string"),
    ("def run(candles_5, now): raise ValueError('synthetic')", "2026-07-01T10:00:00", "synthetic"),
])
def test_worker_error_mapping(source, now, message):
    with pytest.raises(RuntimeError, match=message):
        run_in_sandbox(source, [], now)


def test_transport_keeps_payload_timeout_and_temp_file_cleanup(monkeypatch):
    from pathlib import Path
    from packages.core import simple_sandbox as module
    captured = {}

    class FakeProcess:
        returncode = 0

        def communicate(self, input, timeout):
            captured.update(payload=json.loads(input), timeout=timeout)
            return '{"result":{"direction":"hold"}}', ""

    def start(args, **kwargs):
        captured["worker_file"] = Path(args[1])
        captured["worker_source"] = captured["worker_file"].read_text(encoding="utf-8")
        return FakeProcess()

    monkeypatch.setattr(module.subprocess, "Popen", start)
    source = "def run(candles_5, now): return {}"
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert run_in_sandbox(source, [{"time": now, "close": "1"}], now) == {"direction": "hold"}
    assert captured["payload"] == {"source": source, "candles_5": [{"time": now.isoformat(), "close": "1"}], "now": now.isoformat()}
    assert captured["timeout"] == 10
    assert not captured["worker_file"].exists()


def test_worker_source_matches_pre_refactor_hash():
    import hashlib
    from pathlib import Path
    from packages.core.simple_sandbox_worker import WORKER_SOURCE
    fixture = Path(__file__).resolve().parent / "fixtures/core_refactor_baseline_2026_10_09.json"
    baseline = json.loads(fixture.read_text(encoding="utf-8"))
    assert hashlib.sha256(WORKER_SOURCE.encode()).hexdigest() == baseline["worker_source_sha256"]


def test_infinite_strategy_times_out_with_valid_timestamp():
    # An invalid 'now' would fail before run(), hiding a broken timeout.
    source = "def run(candles_5, now):\n    while True:\n        pass\n"
    with pytest.raises(TimeoutError):
        run_in_sandbox(source, [], "2026-07-01T10:00:00+00:00")
