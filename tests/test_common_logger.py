import json
import json
import time
from typing import Dict

from common import logger


def test_json_logger_emits_required_fields(capfd):
    logger.start_logger()
    ctx: Dict[str, str] = {"unit": "test"}
    logger.info(ctx, "hello world", extra="value")
    logger.shutdown_logger()
    out = capfd.readouterr().out.strip()
    data = json.loads(out)
    assert data["lvl"] == "INFO"
    assert data["msg"] == "hello world"
    assert data["ctx"] == ctx
    assert "ts" in data
    assert "corr_id" in data
    assert data["extra"] == "value"


def test_emit_non_blocking_and_order(monkeypatch, capfd):
    # Ensure no thread is running so we can patch the writer
    logger.shutdown_logger()

    original_write = logger.os.write

    def slow_write(fd, data):
        time.sleep(0.1)
        return original_write(fd, data)

    monkeypatch.setattr(logger.os, "write", slow_write)

    logger.start_logger()
    start = time.time()
    for i in range(3):
        logger.info({}, f"msg{i}")
    duration = time.time() - start
    # If _emit were blocking on writes, duration would be ~0.3s
    assert duration < 0.2

    logger.shutdown_logger()
    out_lines = capfd.readouterr().out.strip().splitlines()
    msgs = [json.loads(line)["msg"] for line in out_lines]
    assert msgs == ["msg0", "msg1", "msg2"]


def test_sink_receives_records():
    logger.shutdown_logger()
    received = []

    def sink(rec):
        received.append(rec)

    logger.add_sink(sink)
    logger.start_logger()
    logger.info({}, "hello")
    logger.shutdown_logger()
    logger.remove_sink(sink)

    assert received and received[0]["msg"] == "hello"


def test_daily_files_follow_event_utc_date_across_midnight(monkeypatch, tmp_path):
    from src.common import logger as audit_log

    monkeypatch.setattr(audit_log, "_LOGS_DIR", tmp_path)
    monkeypatch.setattr(audit_log, "_today_utc", lambda: "2026-11-12")
    audit_log.start_file_logging()
    try:
        audit_log.store_record({"ts": "2026-11-12T00:00:01+00:00", "msg": "today"})
        audit_log.store_record({"ts": "2026-11-12T05:29:59+05:30", "msg": "late yesterday"})
        audit_log.store_record({"ts": "2026-11-12T00:00:02Z", "msg": "today again"})
    finally:
        audit_log.shutdown_logger()

    def messages(day):
        lines = (tmp_path / f"backtest_{day}.log").read_text(encoding="utf-8").splitlines()
        return [json.loads(line)["msg"] for line in lines]

    assert messages("2026-11-11") == ["late yesterday"]
    assert messages("2026-11-12") == ["today", "today again"]


def test_file_write_failure_is_reported_and_writer_recovers(monkeypatch, tmp_path, capsys):
    from src.common import logger as audit_log

    target = tmp_path / "pinned.log"
    write = audit_log._write_record_locked

    def intermittent_write(record):
        if record["msg"].startswith("drop"):
            raise OSError("simulated disk failure")
        write(record)

    monkeypatch.setattr(audit_log, "_write_record_locked", intermittent_write)
    audit_log.start_file_logging(target)
    try:
        for msg in ("drop one", "drop two", "recovered"):
            audit_log.store_record({"ts": "2026-11-11T12:00:00Z", "msg": msg})
    finally:
        audit_log.shutdown_logger()

    assert capsys.readouterr().err.count("Audit log write failed; record lost (OSError).") == 1
    assert json.loads(target.read_text(encoding="utf-8"))["msg"] == "recovered"

