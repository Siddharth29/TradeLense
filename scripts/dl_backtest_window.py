"""Pull + validate the candle window the exit-config sweep needs.

Run this on the Windows box with the MT5 terminal logged in. Read-only: it
opens no orders and touches no account state.

Replaces ``dl_full_window.py``, which pulled only BTCUSD / XAUUSD / GBPUSD /
USOIL and validated nothing. Three assets that actually traded the freeze
window were missing from it — XAGUSD, EURUSD, USTEC — and XAGUSD alone carries
75% of the freeze-era loss, so a sweep on that data would have skipped the
asset that matters most.

Every symbol/interval goes through the same validator the sweep uses, and a
file is only written when every rule passes. A missing CSV stops the sweep
loudly; a silently-bad CSV produces plausible numbers that are wrong.

    python scripts/dl_backtest_window.py
    python scripts/dl_backtest_window.py --from 2026-07-13 --to 2026-07-29
    python scripts/dl_backtest_window.py --symbols XAGUSD,XAUUSD --write-on-fail
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from packages.core.candle_repository import (  # noqa: E402
    CandleRepository, CandleSpec, Mt5CandleSource,
)

# The six assets that actually traded 2026-07-13 -> 07-27, per the freeze-era
# census in EQUITY_LEDGER.md. USOIL was benched 07-14 and is not swept.
SYMBOLS = ["XAUUSD", "XAGUSD", "USTEC", "BTCUSD", "EURUSD", "GBPUSD"]
INTERVALS = ["5m", "15m", "1h"]

# Some brokers append `m` to symbols. The source already tries `<SYM>m` and
# `<SYM>`; these are the extra aliases that do not follow that rule.
SYMBOL_ALIASES = {
    "USTEC": ("USTECm", "USTEC", "NAS100m", "NAS100"),
    "BTCUSD": ("BTCUSDm", "BTCUSD"),
}

# Default window: freeze start through 07-29. The tail past the last entry is
# deliberate — post-exit continuation and the 240-minute TTL both need bars
# *after* the final trade closes, and that is precisely the measurement the
# sweep exists to make.
DEFAULT_FROM = "2026-07-13"
DEFAULT_TO = "2026-07-29"


def _utc(day: str) -> datetime:
    return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from", dest="start", default=DEFAULT_FROM)
    ap.add_argument("--to", dest="end", default=DEFAULT_TO)
    ap.add_argument("--symbols", default=",".join(SYMBOLS))
    ap.add_argument("--intervals", default=",".join(INTERVALS))
    ap.add_argument("--out", default="data/processed")
    ap.add_argument(
        "--write-on-fail",
        action="store_true",
        help="write the CSV even when validation fails (inspection only — never sweep on it)",
    )
    args = ap.parse_args()

    repo = CandleRepository(Mt5CandleSource(SYMBOL_ALIASES), root=args.out)
    start, end = _utc(args.start), _utc(args.end)

    written = failed = 0
    print(f"window {args.start} -> {args.end}   out={args.out}\n")

    for symbol in [s.strip().upper() for s in args.symbols.split(",") if s.strip()]:
        for interval in [i.strip() for i in args.intervals.split(",") if i.strip()]:
            spec = CandleSpec(symbol, interval, start, end)
            report = repo.sync(spec, write_on_fail=args.write_on_fail)
            if report.ok:
                written += 1
                print(f"  OK    {symbol:8} {interval:4} {report.summary()}")
            else:
                failed += 1
                print(f"  FAIL  {symbol:8} {interval:4} {report.summary()}")

    print(f"\n{written} written, {failed} failed")
    if failed:
        print("Sweep needs every file to pass. Re-pull the failures before running it.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
