import pandas as pd
import logging
from datetime import datetime
from core.trade_sim import simulate_trade, logger as trade_logger
from core import log_utils


def test_write_row_records_initial_stop(tmp_path, monkeypatch):
    monkeypatch.setattr(log_utils, "RESULTS_DIR", tmp_path)

    base = datetime(2024, 1, 1, 9, 20)
    fut_be = pd.DataFrame(
        [
            {"open": 10, "high": 11.2, "low": 9.5, "close": 10},
            {"open": 10, "high": 10.6, "low": 9.8, "close": 10.2},
        ],
        index=[base, base + pd.Timedelta(minutes=5)],
    )
    pred_be = {"direction": "long", "stop": 8, "target": 12, "level": 10, "condition": "retest"}
    res_be = simulate_trade(pred_be, fut_be)
    res_be["signal_time"] = base
    log_utils.write_row(res_be)

    fut_sl = pd.DataFrame(
        [
            {"open": 9, "high": 10, "low": 8, "close": 9},
            {"open": 7, "high": 8, "low": 6, "close": 7},
        ],
        index=[base, base + pd.Timedelta(minutes=5)],
    )
    pred_sl = {"direction": "long", "stop": 8, "target": 12, "level": 10, "condition": "retest"}
    res_sl = simulate_trade(pred_sl, fut_sl)
    res_sl["signal_time"] = base + pd.Timedelta(minutes=10)
    log_utils.write_row(res_sl)

    df = pd.read_csv(tmp_path / "backtest_results.csv")
    assert list(df.columns) == log_utils.FIELDS
    assert (df["stop"] == df["initial_stop"]).all()
    assert (df["stop"] != df["entry"]).all()


def test_simulate_trade_logs_new_fields(caplog):
    fut = pd.DataFrame(
        [{"open": 90, "high": 95, "low": 85, "close": 92}],
        index=[datetime(2024, 1, 1, 9, 15)],
    )
    pred = {
        "direction": "long",
        "level": 100,
        "stop": 95,
        "target": 110,
        "condition": "breakout",
        "confidence": 0.7,
    }
    with caplog.at_level(logging.INFO, logger=trade_logger.name):
        simulate_trade(pred, fut, symbol="NIFTY", trade_id="T1")
    combined = "\n".join(r.message for r in caplog.records)
    assert "pattern=breakout" in combined
    assert "confidence=0.7" in combined
    assert "triggered=False" in combined
    assert "symbol=NIFTY" in combined
    assert "trade_id=T1" in combined
