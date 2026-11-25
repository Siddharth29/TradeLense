import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from .config import RESULTS_DIR, LOGS_DIR

# Compute a unique log file per process/run
# Example: results/logs/backtest_20250908_105523_12345.log
_RUN_TS = datetime.now().strftime("%Y%m%d_%H%M%S")
LOGS_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOGS_DIR / f"backtest_{_RUN_TS}_{os.getpid()}.log"

# Durable structured-log file for stdlib records. The plain-text ``LOG_FILE``
# formatter renders only ``%(message)s`` and DROPS the ``extra={...}`` fields
# that hot-path components (trade_monitor, multi_expert) attach — so without this
# those structured fields exist only in volatile memory / the WS stream. This
# JSON-lines file captures message + extras durably so the run is reconstructable
# offline (scripts/logs_to_report.py reads ``*.log`` and ``*.jsonl``).
STRUCTURED_LOG_FILE = LOGS_DIR / f"structured_{_RUN_TS}_{os.getpid()}.jsonl"

# Attributes present on a bare LogRecord — anything else on ``record.__dict__``
# was supplied via ``extra={...}`` and is the structured payload we want.
_STD_LOGRECORD_KEYS = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "level", "pathname",
        "filename", "module", "exc_info", "exc_text", "stack_info", "lineno",
        "funcName", "created", "msecs", "relativeCreated", "thread",
        "threadName", "processName", "process", "taskName",
    }
)

_structured_lock = threading.Lock()
_structured_enabled = False


class _StructuredJsonHandler(logging.Handler):
    """Append each stdlib record as a JSON line (message + ``extra`` fields).

    Never raises out of ``emit`` — logging must not break the trading path.
    """

    def __init__(self, path: Path):
        super().__init__()
        self._path = path
        self._fh = open(path, "a", encoding="utf-8")
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - thin IO
        try:
            ctx = {
                k: v
                for k, v in record.__dict__.items()
                if k not in _STD_LOGRECORD_KEYS and not k.startswith("_")
            }
            rec = {
                "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                "lvl": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
                "ctx": ctx,
            }
            line = json.dumps(rec, default=str, separators=(",", ":")) + "\n"
            with self._lock:
                self._fh.write(line)
                self._fh.flush()
        except Exception:
            pass


def enable_structured_file_logging(path: Path | None = None) -> Path:
    """Attach (once) a structured JSON-lines handler to the root logger.

    Idempotent: subsequent calls are no-ops. Because named loggers from
    :func:`get_logger` propagate to root, a single root handler captures every
    stdlib record — with its ``extra={...}`` fields — exactly once.
    """
    global _structured_enabled
    with _structured_lock:
        if _structured_enabled:
            return STRUCTURED_LOG_FILE
        target = Path(path) if path else STRUCTURED_LOG_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        handler = _StructuredJsonHandler(target)
        handler.setLevel(logging.INFO)
        root = logging.getLogger()
        # Avoid duplicates if a previous instance is somehow already attached.
        if not any(isinstance(h, _StructuredJsonHandler) for h in root.handlers):
            root.addHandler(handler)
        _structured_enabled = True
        return target


def get_logger(name: str = "backtest") -> logging.Logger:
    """Return a logger writing to a unique file under ``results/logs``."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        LOG_FILE.parent.mkdir(exist_ok=True)
        formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

        # File handler for persistent logs
        fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
        fh.setFormatter(formatter)
        logger.addHandler(fh)

        # Ensure root logger outputs to console for live visibility
        if not logging.getLogger().handlers:
            logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    # Durable structured-log capture for stdlib `extra={...}` fields. OPT-IN
    # (STRUCTURED_FILE_LOGGING=1): off by default so production logging behaviour
    # — and hot-path I/O — is byte-for-byte unchanged. Wrapped so a logging-setup
    # failure can never break a get_logger() caller on the trade path.
    if os.getenv("STRUCTURED_FILE_LOGGING", "").strip().lower() in {"1", "true", "yes", "y", "on"}:
        try:
            enable_structured_file_logging()
        except Exception:
            pass
    # Allow propagation so test fixtures and root handlers can observe logs
    logger.propagate = True
    return logger

# Convenience accessor for the current log file path
def get_current_log_file() -> Path:
    return LOG_FILE
