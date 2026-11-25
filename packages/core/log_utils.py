import csv
import os
from .config import RESULTS_DIR


# Fixed field order for backtest CSV output
FIELDS = [
    "signal_time",
    "entry_time",
    "exit_time",
    "direction",
    "entry",
    "stop",
    "initial_stop",
    "target",
    "tp1_points",
    "exit_price",
    "profit",
    "risk_reward",
    "result",
]


def write_row(row: dict) -> None:
    # If a per-run filename is provided via env, write to that; otherwise use the default
    filename = os.getenv("BACKTEST_CSV_FILENAME")
    file = RESULTS_DIR / (filename if filename else "backtest_results.csv")
    file.parent.mkdir(exist_ok=True)
    write_header = not file.exists() or file.stat().st_size == 0
    with open(file, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in FIELDS})
