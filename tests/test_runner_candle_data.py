"""Runner conversion contracts; no agent construction, model or broker calls."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from apps.backend.agents import runner as module


@pytest.fixture
def runner(monkeypatch):
    instance = module.AgentRunner.__new__(module.AgentRunner)
    instance._log_ctx = {"asset": "TEST"}
    monkeypatch.setattr(module, "app_logger", SimpleNamespace(error=Mock(), info=Mock(), warning=Mock()))
    return instance


def candle(**changes):
    values = dict(time=datetime(2026, 7, 1, 10, tzinfo=timezone.utc),
                  open='"100.0"', high="101", low=99, close="100.5", volume=None)
    values.update(changes)
    return SimpleNamespace(**values)


def test_object_conversion_preserves_numeric_fallbacks(runner):
    assert runner._candle_to_dict(candle(high="bad", close=None)) == {
        "time": "2026-07-01T10:00:00+00:00", "open": 100.0, "high": 0.0,
        "low": 99.0, "close": 0.0, "volume": 0.0,
    }


def test_missing_object_attributes_preserve_logged_empty_fallback(runner):
    result = runner._candle_to_dict(SimpleNamespace())
    assert result["time"] and result["open"] == result["close"] == 0
    assert module.app_logger.error.call_count == 1
    assert "Failed to convert candle to dict" in module.app_logger.error.call_args.args[0]


def test_object_conversion_does_not_change_logger_failure_behavior(runner):
    module.app_logger.error.side_effect = TypeError("synthetic logger signature error")
    with pytest.raises(TypeError, match="synthetic logger signature error"):
        runner._candle_to_dict(SimpleNamespace())


def test_batch_reuses_dict_lists_and_preserves_dataframe_order(runner):
    bars = [{"time": "2026-07-01T10:05:00", "open": "100", "high": "101", "low": "99", "close": "100.5", "volume": "10"},
            {"time": "2026-07-01T10:00:00", "open": "99", "high": "100", "low": "98", "close": "99.5", "volume": "11"}]
    higher = [dict(bars[0])]
    c5, c15, frame, c5_json, c15_json = runner._convert_candles_batch(bars, higher)
    assert c5 is bars and c15 is higher
    assert list(frame.index) == [pd.Timestamp("2026-07-01T10:05"), pd.Timestamp("2026-07-01T10:00")]
    assert list(frame["close"]) == [100.5, 99.5]
    assert json.loads(c5_json) == bars and json.loads(c15_json) == higher
    assert bars[0]["close"] == "100.5"


def test_batch_object_conversion_and_empty_higher_timeframe(runner):
    result = runner._convert_candles_batch([candle(volume="10")], [])
    assert result[0][0]["open"] == 100.0
    assert result[1] == [] and result[4] == "[]"
    assert result[2].iloc[0]["volume"] == 10.0


def test_invalid_dataframe_conversion_retains_empty_frame_and_logging(runner):
    frame = runner._convert_candles_to_dataframe([{"open": None}])
    assert frame.empty
    assert module.app_logger.error.call_args.args == (runner._log_ctx, "Error converting candles to DataFrame")


def test_batch_preserves_overridden_converters(runner, monkeypatch):
    converted = {"time": "2026-07-01T10:00:00", "close": 1.0}
    object_converter = Mock(return_value=converted)
    frame = pd.DataFrame({"close": [1.0]})
    frame_converter = Mock(return_value=frame)
    monkeypatch.setattr(runner, "_candle_to_dict", object_converter)
    monkeypatch.setattr(runner, "_convert_candles_to_dataframe", frame_converter)
    raw = [object()]
    result = runner._convert_candles_batch(raw, [])
    object_converter.assert_called_once_with(raw[0])
    frame_converter.assert_called_once_with([converted])
    assert result[2] is frame


def test_batch_exception_retains_empty_defaults(runner, monkeypatch):
    monkeypatch.setattr(runner, "_candle_to_dict", Mock(side_effect=ValueError("synthetic")))
    c5, c15, frame, j5, j15 = runner._convert_candles_batch([object()], [])
    assert c5 == c15 == [] and frame.empty and j5 == j15 == "[]"
    assert module.app_logger.error.call_args.args == (runner._log_ctx, "Error in batch candle conversion")


@pytest.mark.parametrize("data,expected", [("already-json", "already-json"), ([], "[]"), ({"close": 1.0}, '[{"close":1.0}]')])
def test_json_conversion_contract(runner, data, expected):
    assert runner._to_json(data) == expected


def test_json_conversion_preserves_overridden_converter(runner, monkeypatch):
    converter = Mock(return_value={"close": 5.0})
    monkeypatch.setattr(runner, "_candle_to_dict", converter)
    assert runner._to_json([object()]) == '[{"close":5.0}]'
    converter.assert_called_once()


def test_effective_execution_mode_still_rejects_run_once(runner):
    with pytest.raises(ValueError, match="Invalid execution mode"):
        runner.set_execution_mode("run_once")
    runner.set_execution_mode("run_now")
    assert runner._execution_mode == "run_now"
