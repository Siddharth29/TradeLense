import datetime as dt

import mongomock
import numpy as np
import pytest

from core.mongo_utils import (
    save_backtest_result,
    query_backtests,
    save_backtest_report,
)


def test_save_and_query_by_name():
    client = mongomock.MongoClient()
    coll = client.db.results

    summary = {"risk_to_reward": 1.5, "total_api_calls": 3}
    save_backtest_result("run1", summary, collection=coll)

    docs = query_backtests(test_name="run1", collection=coll)
    assert len(docs) == 1
    assert docs[0]["test_name"] == "run1"


def test_query_by_date():
    client = mongomock.MongoClient()
    coll = client.db.results

    now = dt.datetime.now(dt.timezone.utc)
    summary = {"risk_to_reward": 1.0, "total_api_calls": 2}
    save_backtest_result("run2", summary, collection=coll)

    docs = query_backtests(date=now.strftime("%Y-%m-%d"), collection=coll)
    assert len(docs) == 1
    assert docs[0]["test_name"] == "run2"


def test_save_backtest_report():
    client = mongomock.MongoClient()
    coll = client.db.reports
    trades = [
        {
            "entry_time": dt.datetime(2024, 1, 1, 9, 15),
            "exit_time": dt.datetime(2024, 1, 1, 10, 15),
            "entry_price": 100.0,
            "exit_price": 110.0,
            "stop_loss": 95.0,
            "target": 110.0,
            "result": "win",
            "points": 10.0,
            "risk_to_reward": 2.0,
            "api_call_id": "abc123",
        }
    ]
    with pytest.warns(DeprecationWarning):
        save_backtest_report("run3", trades, collection=coll)
    doc = coll.find_one({"test_name": "run3"})
    assert doc is not None
    assert doc["trades"][0]["entry_price"] == 100.0
    assert "created_at" in doc
    metrics = doc.get("metrics")
    assert metrics is not None
    assert metrics["total_trades"] == 1
    assert metrics["stop_loss_hits"] == 0
    assert metrics["win_rate"] == 1
    assert metrics["risk_to_reward"] == 2.0


def test_save_report_with_numpy_numbers():
    client = mongomock.MongoClient()
    coll = client.db.reports
    trades = [
        {
            "entry_time": dt.datetime(2024, 1, 1, 9, 15),
            "exit_time": dt.datetime(2024, 1, 1, 10, 15),
            "entry_price": np.int64(100),
            "exit_price": np.int64(110),
            "stop_loss": np.int64(95),
            "target": np.int64(110),
            "result": "win",
            "points": np.int64(10),
            "risk_to_reward": np.float64(2.0),
            "api_call_id": "abc123",
        }
    ]
    with pytest.warns(DeprecationWarning):
        save_backtest_report("run_np", trades, collection=coll)
    doc = coll.find_one({"test_name": "run_np"})
    assert doc is not None
    trade = doc["trades"][0]
    assert trade["entry_price"] == 100
    assert isinstance(trade["entry_price"], int)
