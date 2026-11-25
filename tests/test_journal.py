import json
from datetime import datetime

import pytest
from datetime import datetime, timezone

from packages.core.journal import AssetJournal


def test_journal_writes_required_fields(tmp_path):
    journal = AssetJournal(tmp_path, "NIFTY", "test_agent", buffer_size=1)
    journal.log_decision({"action": "buy"}, "ph", "mode", 1.23)
    journal.flush()

    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    path = tmp_path / f"NIFTY_{today}.jsonl"
    assert path.exists()
    data = json.loads(path.read_text().strip())
    assert data["asset_id"] == "NIFTY"
    assert data["agent_name"] == "test_agent"
    assert data["prompt_hash"] == "ph"
    assert data["gpt_mode"] == "mode"
    assert data["latency_ms"] == pytest.approx(1.23)


def test_journal_rotates_files(tmp_path, monkeypatch):
    from packages.core import journal as journal_module

    class D1(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2023, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    class D2(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2023, 1, 2, 0, 0, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(journal_module, "datetime", D1)
    j = AssetJournal(tmp_path, "ASSET", "agent", buffer_size=1)
    j.log_decision({}, "h1", "m", 0.0)
    j.flush()
    file1 = tmp_path / "ASSET_20230101.jsonl"
    assert file1.exists()

    monkeypatch.setattr(journal_module, "datetime", D2)
    j.log_decision({}, "h2", "m", 0.0)
    j.flush()
    file2 = tmp_path / "ASSET_20230102.jsonl"
    assert file2.exists()

