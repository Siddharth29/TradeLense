from __future__ import annotations

"""Structured JSON logger utilities.

The logger writes one JSON object per line to ``stdout`` with the fields:
``ts`` (ISO formatted timestamp), ``lvl`` (log level), ``msg`` (message
string), ``ctx`` (context dictionary) and ``corr_id`` (correlation
identifier). Additional keyword arguments are merged into the log record.

Logging is performed asynchronously: log records are enqueued into a
``queue.Queue`` and a background thread drains the queue writing each entry to
``sys.stdout`` using ``os.write`` to minimise blocking.
"""

from datetime import datetime, timezone
import json
import os
import queue
import sys
import threading
import uuid
from typing import Any, Dict, Optional, Callable, Set
from pathlib import Path


_log_queue: "queue.Queue[bytes]" = queue.Queue()
_log_thread: Optional[threading.Thread] = None
_stop_event: Optional[threading.Event] = None
_sinks: Set[Callable[[Dict[str, Any]], None]] = set()

# File logging support
_file_handle: Optional[Any] = None
_file_lock: threading.Lock = threading.Lock()
_current_log_file: Optional[Path] = None
# UTC date (YYYY-MM-DD) the currently-open daily file belongs to. Used to roll
# over to a fresh file when the day changes so logs are split per calendar day.
_current_log_date: Optional[str] = None
# When True the file sink rotates to a new day-stamped file at UTC midnight.
# Set to False when a caller pins an explicit ``path`` (no rotation).
_rotate_daily: bool = False
_ROOT = Path(__file__).resolve().parents[2]
_LOGS_DIR = _ROOT / "results" / "logs"

# Asynchronous file-writer plumbing.
#
# Records are handed to a background thread via ``_file_queue`` and serialized +
# written + flushed there, so the CALLER (including the live-trading thread that
# places orders) is never blocked on disk I/O or JSON serialization — it only
# does an O(1), lock-free ``put_nowait``. This is what lets the live flow be
# durably stored without adding latency to the trading path.
_file_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
_file_thread: Optional[threading.Thread] = None
_file_stop: Optional[threading.Event] = None


def _logger_writer() -> None:
    """Drain ``_log_queue`` and write bytes to ``stdout``."""

    assert _stop_event is not None  # for mypy/typing
    while not _stop_event.is_set() or not _log_queue.empty():
        try:
            data = _log_queue.get(timeout=0.1)
        except queue.Empty:
            continue
        os.write(sys.stdout.buffer.fileno(), data)
        _log_queue.task_done()


def start_logger() -> None:
    """Start the background logger thread if not already running."""

    global _log_thread, _stop_event
    if _log_thread and _log_thread.is_alive():
        return
    _stop_event = threading.Event()
    _log_thread = threading.Thread(target=_logger_writer, name="LoggerWriter", daemon=True)
    _log_thread.start()


def shutdown_logger() -> None:
    """Signal the logger thread to terminate and wait for it to finish."""

    global _log_thread, _stop_event, _log_queue
    if _log_thread is not None:
        assert _stop_event is not None
        _stop_event.set()
        _log_thread.join()
        _log_thread = None
        _stop_event = None
    global _file_handle, _current_log_file, _current_log_date
    global _file_thread, _file_stop
    _log_queue = queue.Queue()
    # Stop the file writer AFTER signalling: its loop keeps draining until the
    # queue is empty, so every already-enqueued record is flushed to disk before
    # we close the handle (graceful shutdown loses nothing).
    remove_sink(_file_sink)
    if _file_thread is not None:
        assert _file_stop is not None
        _file_stop.set()
        _file_thread.join()
        _file_thread = None
        _file_stop = None
    # Close file handle if open
    with _file_lock:
        if _file_handle is not None:
            try:
                _file_handle.flush()
                _file_handle.close()
            except Exception:
                pass
            _file_handle = None
            _current_log_file = None
            _current_log_date = None


def add_sink(fn: Callable[[Dict[str, Any]], None]) -> None:
    """Register a callback to receive each log record."""

    _sinks.add(fn)


def remove_sink(fn: Callable[[Dict[str, Any]], None]) -> None:
    """Unregister a previously added log record callback."""

    _sinks.discard(fn)


def _today_utc() -> str:
    """Return the current UTC date as ``YYYY-MM-DD`` (used for daily rotation)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _daily_log_path(day: str) -> Path:
    """Path of the day-stamped log file, e.g. ``results/logs/backtest_2026-07-06.log``."""
    return _LOGS_DIR / f"backtest_{day}.log"


def _serialize_record(rec: Dict[str, Any]) -> str:
    """Serialize a log record to a single JSON line — never raises.

    Tries the rich serializer first (keeps numbers/nesting intact), then a
    plain ``json.dumps(default=str)`` fallback, then a minimal hand-built line.
    This guarantees every record reaches disk even if a field is exotic — the
    old sink dropped the whole line on any serialization hiccup, which is why
    the file could stay empty while the websocket stream (a separate sink that
    does no serialization) looked healthy.
    """
    try:
        # Best fidelity: recursively coerce datetimes/pandas/etc.
        from packages.core.datetime_utils import serialize_for_json

        return json.dumps(serialize_for_json(rec), separators=(",", ":"))
    except Exception:
        pass
    try:
        # Robust fallback: str() any leaf json can't handle. Never drops the line.
        return json.dumps(rec, default=str, separators=(",", ":"))
    except Exception:
        # Last resort: preserve at least ts/level/msg so the run stays readable.
        try:
            return json.dumps(
                {
                    "ts": rec.get("ts"),
                    "lvl": rec.get("lvl"),
                    "msg": rec.get("msg"),
                    "serialize_error": True,
                },
                default=str,
                separators=(",", ":"),
            )
        except Exception:
            return '{"serialize_error":true}'


def _open_daily_file_locked(day: str) -> None:
    """Open (append) the day-stamped file and mark it active. Caller holds the lock."""
    global _file_handle, _current_log_file, _current_log_date
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    path = _daily_log_path(day)
    _file_handle = open(path, "a", encoding="utf-8")
    _current_log_file = path
    _current_log_date = day


def _maybe_rotate_locked(record: Dict[str, Any]) -> None:
    """Select the record's UTC date, even if the writer drains it after midnight."""
    if not _rotate_daily:
        return
    stamp = record.get("ts")
    try:
        timestamp = datetime.fromisoformat(stamp) if isinstance(stamp, str) else None
    except ValueError:
        timestamp = None
    if timestamp is None:
        day = _today_utc()
    else:
        if timestamp.tzinfo is not None:
            timestamp = timestamp.astimezone(timezone.utc)
        day = timestamp.date().isoformat()
    if _file_handle is not None and day == _current_log_date:
        return
    previous_handle = _file_handle
    _open_daily_file_locked(day)
    if previous_handle is not None:
        try:
            previous_handle.close()
        except Exception:
            pass


def _write_record_locked(rec: Dict[str, Any]) -> None:
    """Serialize + append + flush one record. Runs on the background writer only.

    The flush after every record is what makes the file durable: if the app is
    closed or crashes, everything written up to that moment is already on disk.
    """
    line = _serialize_record(rec) + "\n"
    with _file_lock:
        _maybe_rotate_locked(rec)
        if _file_handle is not None:
            _file_handle.write(line)
            _file_handle.flush()


def _file_writer_loop() -> None:
    """Drain ``_file_queue`` and persist each record off the caller's thread."""
    assert _file_stop is not None
    write_failed = False
    while not _file_stop.is_set() or not _file_queue.empty():
        try:
            rec = _file_queue.get(timeout=0.1)
        except queue.Empty:
            continue
        try:
            _write_record_locked(rec)
            write_failed = False
        except Exception as exc:
            if not write_failed:
                # Bypass the logger so a failed sink cannot recursively log itself.
                try:
                    print(f"Audit log write failed; record lost ({type(exc).__name__}).",
                          file=sys.stderr, flush=True)
                except Exception:
                    pass
            write_failed = True
        finally:
            _file_queue.task_done()


def store_record(rec: Dict[str, Any]) -> None:
    """Hand a log record to the durable daily file — non-blocking.

    Public entry point used both by the internal ``_file_sink`` and by the API's
    stdlib log handler so that *every* log path (including the live-trading
    components that log via ``logging.getLogger``) is captured to disk. The call
    is O(1) and lock-free: all serialization and I/O happen on the background
    writer thread, so the caller — e.g. the order-placing trading thread — is
    never blocked. No-op until :func:`start_file_logging` has been called.
    """
    if _file_thread is None:
        return
    try:
        _file_queue.put_nowait(rec)
    except Exception:
        # Never raise from the logging path.
        pass


def _file_sink(rec: Dict[str, Any]) -> None:
    """Sink registered on ``_emit`` records — forwards to the async file writer."""
    store_record(rec)


def start_file_logging(path: Optional[str | Path] = None) -> Path:
    """Enable file logging by writing JSON lines to a file via a background thread.

    If ``path`` is not provided, logs are written to a per-day file under
    ``results/logs`` named ``backtest_YYYY-MM-DD.log`` (UTC), rolling over to a
    fresh file when the calendar day changes so logs are separated by day.
    Passing an explicit ``path`` pins that single file (no daily rotation).

    Writing is asynchronous: records are queued and a dedicated daemon thread
    serializes, appends and flushes them, so enabling file logging adds no
    disk-I/O latency to any caller (notably the live-trading path). Each record
    is flushed as it is written, so a crash/shutdown still leaves the logs on
    disk. Returns the ``Path`` to the currently-active log file.
    """
    global _file_handle, _current_log_file, _current_log_date, _rotate_daily
    global _file_thread, _file_stop
    if _file_handle is not None:
        return _current_log_file  # type: ignore[return-value]

    _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with _file_lock:
        if path is None:
            _rotate_daily = True
            _open_daily_file_locked(_today_utc())
        else:
            _rotate_daily = False
            path = Path(path)
            if not path.parent.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
            _file_handle = open(path, "a", encoding="utf-8")
            _current_log_file = path
            _current_log_date = None

    # Start the background writer (idempotent — guarded by _file_handle above).
    if _file_thread is None or not _file_thread.is_alive():
        _file_stop = threading.Event()
        _file_thread = threading.Thread(
            target=_file_writer_loop, name="LogFileWriter", daemon=True
        )
        _file_thread.start()

    add_sink(_file_sink)
    return _current_log_file  # type: ignore[return-value]


def get_current_log_file() -> Optional[Path]:
    """Return the path to the active log file if file logging is enabled."""
    return _current_log_file


def _emit(level: str, ctx: Dict[str, Any], msg: str, **fields: Any) -> None:
    """Format a log record and enqueue it for asynchronous emission."""

    
    record: Dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "lvl": level.upper(),
        "msg": msg,
        "ctx": ctx or {},
        "corr_id": fields.pop("corr_id", str(uuid.uuid4())),
    }
    if fields:
        record.update(fields)
    
    # Use safe JSON serialization to handle datetime objects
    try:
        # Import the safe serialization function
        from packages.core.datetime_utils import serialize_for_json
        serialized_record = serialize_for_json(record)
        line = json.dumps(serialized_record, separators=(",", ":")).encode("utf-8") + b"\n"
    except Exception:
        # Fallback to string representation if serialization fails
        try:
            line = json.dumps(str(record), separators=(",", ":")).encode("utf-8") + b"\n"
        except Exception:
            # Final fallback
            line = f"{{'error': 'Failed to serialize log record', 'level': '{level}', 'msg': '{msg}'}}\n".encode("utf-8")
    
    _log_queue.put_nowait(line)
    for sink in list(_sinks):
        try:
            sink(record)
        except Exception:
            pass


def info(ctx: Dict[str, Any], msg: str, **fields: Any) -> None:
    # Remove 'level' from fields to avoid conflict with positional parameter
    fields.pop("level", None)
    _emit("INFO", ctx, msg, **fields)


def error(ctx: Dict[str, Any], msg: str, **fields: Any) -> None:
    # Remove 'level' from fields to avoid conflict with positional parameter
    fields.pop("level", None)
    _emit("ERROR", ctx, msg, **fields)


def debug(ctx: Dict[str, Any], msg: str, **fields: Any) -> None:
    # Remove 'level' from fields to avoid conflict with positional parameter
    fields.pop("level", None)
    _emit("DEBUG", ctx, msg, **fields)


def warning(ctx: Dict[str, Any], msg: str, **fields: Any) -> None:
    # Remove 'level' from fields to avoid conflict with positional parameter
    fields.pop("level", None)
    _emit("WARNING", ctx, msg, **fields)


__all__ = [
    "info",
    "error",
    "debug",
    "warning",
    "start_logger",
    "shutdown_logger",
    "add_sink",
    "remove_sink",
    "start_file_logging",
    "get_current_log_file",
    "store_record",
]

