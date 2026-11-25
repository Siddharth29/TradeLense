import json
from datetime import datetime
import pytest

from packages.core import replay
from scripts import replay_builder


@pytest.mark.asyncio
async def test_build_replay_merges_and_orders(monkeypatch):
    orders = [
        {"client_order_id": "b", "time": datetime(2024, 1, 1, 10)},
        {"client_order_id": "a", "time": datetime(2024, 1, 1, 9)},
    ]
    trades = [
        {"client_order_id": "b", "time": datetime(2024, 1, 1, 11)}
    ]

    async def fake_get_orders(asset, start=None, end=None):
        return orders

    async def fake_get_trades(asset, start=None, end=None):
        return trades

    monkeypatch.setattr(replay.read, "get_orders", fake_get_orders)
    monkeypatch.setattr(replay.read, "get_trades", fake_get_trades)

    events = await replay.build_replay("XYZ")
    assert [e["client_order_id"] for e in events] == ["a", "b"]
    assert events[0]["order"] == orders[1]
    assert events[1]["trade"] == trades[0]


def test_cli_outputs_jsonl(monkeypatch, capsys):
    events = [
        {"client_order_id": "1", "time": datetime(2024, 1, 1)},
        {"client_order_id": "2", "time": datetime(2024, 1, 2)},
    ]

    async def fake_build_replay(asset, start=None, end=None):
        return events

    monkeypatch.setattr(replay_builder.replay, "build_replay", fake_build_replay)

    replay_builder.main(["--asset", "XYZ"])
    out = capsys.readouterr().out.strip().splitlines()
    assert out == [json.dumps(e, default=str) for e in events]
