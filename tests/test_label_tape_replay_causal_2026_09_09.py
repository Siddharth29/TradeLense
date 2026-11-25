"""Frozen research must not let future entry/exit candle prices certify an edge."""
from datetime import datetime, timedelta, timezone

from scripts.research import label_tape_replay as replay


def test_entry_tape_uses_only_completed_m5_bars(monkeypatch):
    start = datetime(2026, 7, 13, tzinfo=timezone.utc)
    seen = []

    def capture(bars, label, symbol):
        seen.extend(bars)
        return {"direction": "up"}

    monkeypatch.setattr(replay, "compute_session_tape", capture)
    bars = [{"time": int((start + timedelta(minutes=n)).timestamp()),
             "open": 100, "high": 102, "low": 99, "close": 101}
            for n in (0, 5, 10)]
    tr = {"symbol": "XAUUSD", "entry_time": start + timedelta(minutes=10, seconds=5),
          "entry": 101, "sl": 99, "tp": 105, "side": "buy", "label": "up"}
    replay.reconstruct(tr, {"XAUUSD": bars})
    assert [b["time"] for b in seen] == [b["time"] for b in bars[:2]]


def test_boundary_bar_extremes_do_not_inflate_mfe():
    start = datetime(2026, 7, 13, tzinfo=timezone.utc)
    bars = [{"time": int((start + timedelta(minutes=n)).timestamp()),
             "open": 100, "high": 200, "low": 1, "close": 101}
            for n in (0, 5)]
    tr = {"symbol": "XAUUSD", "entry_time": start + timedelta(seconds=5),
          "exit_time": start + timedelta(minutes=9), "entry": 100,
          "exit_price": 101, "observed_exit_prices": [102, 101],
          "sl": 95, "tp": 110, "side": "buy", "label": "up"}
    replay.reconstruct(tr, {"XAUUSD": bars})
    assert tr["mfe_r"] == .4  # Actual partial fill is observable; neither full bar is.
    assert tr["tar"] == .2
    assert tr["mfe_basis"] == "lower_bound_closed_bars_and_executions"
