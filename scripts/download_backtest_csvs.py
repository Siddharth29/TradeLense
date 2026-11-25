"""Refresh the backtest candle CSVs from the LIVE Real8 MT5 account.

The backtest reads ``data/processed/<asset>_<5m|15m>_2years.csv`` through
``HistoricalCandleService``. Those files are gitignored — they are the ONLY copy
of that data — and they had drifted months out of date (BTC 2025-11, EUR/GBP
2026-04, XAU 2026-05) with XAGUSD and USTEC missing entirely, so half the traded
roster could not be backtested at all.

REAL ACCOUNT ONLY. Read-only: this calls ``account_info`` and
``copy_rates_range`` and nothing else — it never places, modifies or closes an
order. It refuses to run against the committed demo config, because a CSV built
from Trial-server prices is indistinguishable from real data once written and
would silently poison every backtest that reads it.

Usage
-----
    python scripts/download_backtest_csvs.py                      # all assets, 2y
    python scripts/download_backtest_csvs.py --assets XAGUSD USTEC
    python scripts/download_backtest_csvs.py --years 3
    python scripts/download_backtest_csvs.py --check             # verify only, no write

Existing CSVs are backed up to ``data/processed/_backup_<UTC date>/`` before
being replaced, and each new file is written to a temp path and moved into
place only after it passes the quality checks — a failed or partial download
can never leave a corrupt CSV behind.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from packages.core.asset_registry import ASSET_PROFILES  # noqa: E402
from packages.core.config import DATA_DIR  # noqa: E402

# MT5 timeframe constants are resolved lazily so --help works without the package.
INTERVALS = ("5m", "15m")
# Chunk the range: copy_rates_range is bounded by the terminal's max-bars
# setting, and 30 days of 5m is ~8,640 bars — comfortably inside any default.
CHUNK_DAYS = 30
SUFFIX_CANDIDATES = ("", "m", ".raw")


# ─────────────────────────────────────────────────────────────────────────
# Credentials — real account only
# ─────────────────────────────────────────────────────────────────────────

def load_real_creds() -> tuple[str, int, str]:
    """LIVE creds from the gitignored local config. Fails loudly otherwise.

    Same contract as ``scripts/audit/pull_live_mt5.py`` — one rule, one place.
    """
    path = REPO / "config" / "mt5_config.local.json"
    if not path.exists():
        sys.exit(
            f"LIVE creds not found at {path}.\n"
            f"Refusing to fall back to the committed demo config: backtest candles "
            f"must come from the real account."
        )
    cfg = json.load(open(path, encoding="utf-8"))["mt5"]
    server = str(cfg.get("server", ""))
    if "real" not in server.lower():
        sys.exit(f"server '{server}' is not a real account — refusing.")
    return server, int(cfg["login"]), cfg["password"]


def connect_verified(server: str, login: int, password: str):
    """Connect and HARD-VERIFY we are on the expected real account.

    Initialising with explicit credentials rather than trusting the terminal's
    ambient session is deliberate (2026-07-18): ambient state proved unstable
    mid-day and attached to a session that later failed authorization.
    """
    import MetaTrader5 as mt5

    if not mt5.initialize(login=login, password=password, server=server):
        err = mt5.last_error()
        # Surface the terminal's own reason — an "Invalid account" here is a
        # broker-side rejection, not something a retry can fix.
        if not mt5.initialize():
            sys.exit(
                f"mt5.initialize failed: {err}\n"
                f"The MetaTrader 5 terminal could not authorize {login}@{server}.\n"
                f"Check the terminal's log under "
                f"%APPDATA%\\MetaQuotes\\Terminal\\<id>\\logs\\ for the broker's reason."
            )
        if not mt5.login(login=login, password=password, server=server):
            sys.exit(f"mt5.login failed: {mt5.last_error()}")

    ai = mt5.account_info()
    if ai is None or ai.login != login or "real" not in str(ai.server).lower():
        mt5.shutdown()
        sys.exit(
            f"ACCOUNT VERIFICATION FAILED: connected to "
            f"{getattr(ai, 'login', None)}@{getattr(ai, 'server', None)}, "
            f"expected {login}@{server}"
        )
    print(f"ACCOUNT: {ai.login} {ai.server} balance={ai.balance} equity={ai.equity} {ai.currency}")
    return mt5


# ─────────────────────────────────────────────────────────────────────────
# Symbol resolution + fetch
# ─────────────────────────────────────────────────────────────────────────

def resolve_symbol(mt5, asset: str) -> str | None:
    """Broker symbol for an asset: plain, then 'm', then '.raw'.

    A broker may expose the same instrument under different suffixes per account
    type; the registry stores the clean name, so the broker name has to be
    discovered. A symbol that selects but returns no bars is not usable.
    """
    for suffix in SUFFIX_CANDIDATES:
        candidate = asset + suffix
        if not mt5.symbol_select(candidate, True):
            continue
        probe = mt5.copy_rates_from_pos(candidate, mt5.TIMEFRAME_M5, 0, 2)
        if probe is not None and len(probe):
            return candidate
    return None


def fetch_range(mt5, symbol: str, interval: str, start: datetime, end: datetime) -> pd.DataFrame:
    """Chunked copy_rates_range -> de-duplicated, time-sorted DataFrame."""
    timeframe = {"5m": mt5.TIMEFRAME_M5, "15m": mt5.TIMEFRAME_M15}[interval]
    frames: list[pd.DataFrame] = []
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + timedelta(days=CHUNK_DAYS), end)
        raw = mt5.copy_rates_range(symbol, timeframe, cursor, chunk_end)
        if raw is not None and len(raw):
            frames.append(pd.DataFrame(raw))
        cursor = chunk_end

    if not frames:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    df = pd.concat(frames, ignore_index=True)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    volume_col = "tick_volume" if "tick_volume" in df.columns else "real_volume"
    df = df[["time", "open", "high", "low", "close", volume_col]].rename(
        columns={volume_col: "volume"}
    )
    df = df.drop_duplicates(subset="time", keep="first").sort_values("time")
    return df.set_index("time")


# ─────────────────────────────────────────────────────────────────────────
# Quality reporting
# ─────────────────────────────────────────────────────────────────────────

def quality_report(df: pd.DataFrame, interval: str, asset: str) -> dict:
    """Coverage + gap stats. A CSV that looks full can still be full of holes.

    The XAUUSD file this replaces jumped 2024-08-27 -> 2024-09-04 in its first
    two rows: eight days missing, invisible unless somebody counts.
    """
    step = timedelta(minutes=5 if interval == "5m" else 15)
    if df.empty:
        return {"bars": 0, "ok": False, "note": "no bars returned"}

    deltas = df.index.to_series().diff().dropna()

    # Gap taxonomy. Getting these bounds wrong makes healthy data look broken:
    # an FX/metals weekend is Fri 20:55 -> Sun 22:05 = ~49h, so a naive
    # "anything under 50h is a weekday gap" rule files EVERY weekend as missing
    # data and reports ~40,000 phantom missing bars on a complete series.
    #   intraday (<24h)  : the 20:55-22:05 daily maintenance break (~14 bars on
    #                      metals/indices/oil) plus genuine micro-holes.
    #   weekend (24-75h) : expected market closure, including long weekends.
    #   hole    (>75h)   : a real hole — no market closure runs this long.
    intraday = deltas[(deltas > step) & (deltas < timedelta(hours=24))]
    weekend = deltas[(deltas >= timedelta(hours=24)) & (deltas <= timedelta(hours=75))]
    holes = deltas[deltas > timedelta(hours=75)]

    def _bars(series) -> int:
        return int(((series - step) / step).sum()) if len(series) else 0

    return {
        "bars": len(df),
        "first": df.index[0],
        "last": df.index[-1],
        "span_days": (df.index[-1] - df.index[0]).days,
        "intraday_gaps": len(intraday),
        "intraday_missing": _bars(intraday),
        "weekend_gaps": len(weekend),
        "holes": len(holes),
        "hole_bars": _bars(holes),
        "hole_spans": [(t - g, t) for g, t in zip(holes, holes.index)],
        "largest_gap": deltas.max() if len(deltas) else timedelta(0),
        "ok": len(df) > 0,
    }


def print_report(asset: str, interval: str, rep: dict) -> None:
    if not rep.get("bars"):
        print(f"  {asset:8} {interval:3}  NO DATA — {rep.get('note', '')}")
        return
    flag = "  !! HOLES" if rep["holes"] else ""
    print(
        f"  {asset:8} {interval:3}  bars={rep['bars']:>7,}  "
        f"{rep['first']:%Y-%m-%d} -> {rep['last']:%Y-%m-%d} ({rep['span_days']}d)  "
        f"weekends={rep['weekend_gaps']:>3}  intraday-gaps={rep['intraday_gaps']:>4} "
        f"(~{rep['intraday_missing']:,} bars)  holes={rep['holes']}{flag}"
    )
    for span, end in rep["hole_spans"]:
        print(f"       HOLE {end - span:%Y-%m-%d} -> {end:%Y-%m-%d}  ({span})")


# ─────────────────────────────────────────────────────────────────────────
# Atomic write
# ─────────────────────────────────────────────────────────────────────────

def write_csv(df: pd.DataFrame, asset: str, interval: str, backup_dir: Path) -> Path:
    """Write via a temp file; back up any existing CSV before replacing it.

    These files are gitignored, so an overwritten CSV is unrecoverable except by
    another download — and a download that dies halfway must not be able to
    leave a truncated file that still loads.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    target = DATA_DIR / f"{asset.lower()}_{interval}_2years.csv"
    tmp = target.with_suffix(".csv.tmp")

    df.to_csv(tmp)
    if target.exists():
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, backup_dir / target.name)
    os.replace(tmp, target)
    return target


# ─────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--assets", nargs="+", default=sorted(ASSET_PROFILES),
                    help="Assets to refresh (default: every asset in the registry)")
    ap.add_argument("--years", type=float, default=2.0, help="Years of history (default 2)")
    ap.add_argument("--check", action="store_true",
                    help="Connect, verify the account and resolve symbols; download nothing")
    args = ap.parse_args()

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.years * 365)
    backup_dir = DATA_DIR / f"_backup_{end:%Y%m%d}"

    print("=" * 78)
    print(f"Backtest CSV refresh — REAL account only, read-only")
    print(f"range : {start:%Y-%m-%d} -> {end:%Y-%m-%d}  ({args.years}y)")
    print(f"assets: {' '.join(args.assets)}")
    print(f"out   : {DATA_DIR}")
    print("=" * 78)

    server, login, password = load_real_creds()
    mt5 = connect_verified(server, login, password)

    try:
        print("\nResolving broker symbols...")
        resolved: dict[str, str] = {}
        for asset in args.assets:
            symbol = resolve_symbol(mt5, asset)
            print(f"  {asset:8} -> {symbol or 'NOT AVAILABLE ON THIS ACCOUNT'}")
            if symbol:
                resolved[asset] = symbol

        missing = [a for a in args.assets if a not in resolved]
        if missing:
            print(f"\n  WARNING: no broker symbol for {', '.join(missing)} — skipped.")

        if args.check:
            print("\n--check: verified only, nothing downloaded.")
            return 0
        if not resolved:
            print("\nNothing to download.")
            return 1

        print(f"\nDownloading (backups -> {backup_dir})...")
        failures: list[str] = []
        for asset, symbol in resolved.items():
            for interval in INTERVALS:
                df = fetch_range(mt5, symbol, interval, start, end)
                rep = quality_report(df, interval, asset)
                print_report(asset, interval, rep)
                if not rep["ok"]:
                    failures.append(f"{asset} {interval}")
                    continue
                write_csv(df, asset, interval, backup_dir)

        print("\n" + "=" * 78)
        if failures:
            print(f"Completed WITH FAILURES — no data for: {', '.join(failures)}")
            print("(existing CSVs for those were left untouched)")
        else:
            print("Completed: every requested asset/interval refreshed.")
        print("=" * 78)
        return 1 if failures else 0

    finally:
        mt5.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
