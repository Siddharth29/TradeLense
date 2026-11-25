"""Offline report contracts: mixed logs, leg accounting and single-pass exports."""

import builtins
import csv
import json
import sys

import pytest

from scripts import logs_to_report as report


@pytest.fixture
def mixed_log(tmp_path):
    path = tmp_path / "run.jsonl"
    records = [
        {"event": "cycle", "payload": {"cycle_id": "c1"}},
        {"event": "trade_open", "payload": {}},
        {"event": "trade_exit", "payload": {
            "asset": "XAUUSD", "side": "SELL", "entry_price": "4000",
            "exit_price": 3990, "pnl": "10", "exit_reason": "tp1_partial",
        }},
        {"event": "trade_open", "payload": {
            "asset": "XAUUSD", "side": "sell", "trade_status": "closed",
            "pnl": 3, "exit_reason": "TP2_PARTIAL",
        }},
        {"event": "trade_open", "payload": {"trade_status": "cancelled"}},
        {"event": "trade_exit", "payload": {
            "symbol": "XAUUSD", "side": "sell", "pnl": -5, "exit_reason": "SL",
        }},
        {"event": "trade_exit", "payload": {"pnl": 0}},
        {"event": "trade_exit", "payload": {"pnl": "unavailable"}},
        {"event": "trade_exit", "payload": {"pnl": "inf"}},
        {"event": "trade_exit", "payload": []},
        {"event": "cycle", "payload": None},
        [{"event": "trade_exit", "payload": {"pnl": 999}}],
    ]
    path.write_text("plain text diagnostic\n{broken\n\n" + "\n".join(
        json.dumps(record) for record in records
    ) + '\n"event"\n', encoding="utf-8")
    return path


@pytest.mark.parametrize("exclude_partials,legs,net", [(False, 4, "+8.00"), (True, 2, "-5.00")])
def test_mixed_logs_preserve_leg_accounting(mixed_log, tmp_path, exclude_partials, legs, net):
    paths = [str(mixed_log), str(tmp_path / "missing.log")]
    summary = report.collect_log_data(paths, exclude_partials)
    assert (summary.source_files, summary.cycles, summary.opens) == (2, 1, 1)
    assert len(summary.rows) == legs
    assert summary.rows[-1] == {
        "asset": "?", "side": "?", "entry_price": None,
        "exit_price": None, "pnl": 0.0, "reason": "?",
    }
    markdown = report.build_report(paths, exclude_partials)
    assert f"| Closed legs | {legs} |" in markdown
    assert f"| Net P/L | {net} |" in markdown
    assert "- Source files: 2\n- Cycles logged: 1\n- Trade-open events: 1" in markdown
    assert report.collect_rows(paths, exclude_partials) == summary.rows
    if not exclude_partials:
        assert summary.rows[0]["entry_price"] == 4000.0
        assert summary.rows[0]["reason"] == "TP1_PARTIAL"
        assert summary.rows[1]["reason"] == "TP2_PARTIAL"
        assert "Win rate | 50.0% (2W / 1L)" in markdown


def test_cli_markdown_and_csv_share_one_file_read(mixed_log, tmp_path, monkeypatch):
    markdown_path = tmp_path / "report.md"
    csv_path = tmp_path / "rows.csv"
    expected = report.build_report([str(mixed_log)], False)
    source_reads = []
    real_open = builtins.open

    def tracked_open(path, *args, **kwargs):
        if str(path) == str(mixed_log):
            source_reads.append(path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", tracked_open)
    monkeypatch.setattr(sys, "argv", [
        "logs_to_report.py", str(mixed_log), "-o", str(markdown_path), "--csv", str(csv_path),
    ])
    report.main()
    assert source_reads == [str(mixed_log)]
    assert markdown_path.read_text(encoding="utf-8") == expected + "\n"
    with csv_path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 4
    assert [float(row["pnl"]) for row in rows] == [10, 3, -5, 0]


def test_empty_report_contract():
    assert report.build_report([], False) == (
        "# Trade Performance Report (reconstructed from logs)\n\n"
        "- Source files: 0\n- Cycles logged: 0\n- Trade-open events: 0\n"
        "- Closed legs analysed: 0  (partials included)\n"
        "\n_No closed-trade events found in the provided logs._"
    )
