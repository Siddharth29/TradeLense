"""Run the real backtest engine over the freeze window, from the CLI.

This is the exact same call sequence the browser's POST /backtest/run makes
(backtest_repo.create_backtest -> BacktestRunner -> execute_backtest) — no
changes to backtest_runner.py itself, just a headless driver so a long
multi-asset run can be launched, monitored, and resumed from a terminal
instead of the browser.

Sequential by asset (not concurrent): one bad checkpoint stays isolated to a
single asset, already-completed assets keep their Mongo/log records intact,
and a retry after a fix only needs to re-run the assets that didn't finish.

Usage:
    python scripts/audit/run_freeze_backtest.py --smoke
        # tiny 1-day/1-asset slice to validate the pipeline before spending
    python scripts/audit/run_freeze_backtest.py
        # full 2026-07-13 -> 2026-07-29 run, all 6 freeze-era assets
    python scripts/audit/run_freeze_backtest.py --assets XAGUSD,USTEC
        # re-run only the assets that failed / didn't finish last time
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Load .env before any package that reads os.environ at import time
# (mirrors apps/backend/api.py's own bootstrap — ANTHROPIC_API_KEY etc. live there).
from dotenv import load_dotenv  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env", override=False)
sys.path.insert(0, str(REPO_ROOT))

from packages.core.backtest_runner import BacktestRunner  # noqa: E402
from packages.core.config import CS_DEFAULT_RISK_PCT  # noqa: E402
from packages.core.logger import get_logger  # noqa: E402
from packages.db.repositories import backtest as backtest_repo  # noqa: E402

logger = get_logger(__name__)

DEFAULT_ASSETS = ["XAUUSD", "XAGUSD", "USTEC", "BTCUSD", "EURUSD", "GBPUSD"]
DEFAULT_FROM = "2026-07-13T00:00:00+00:00"
DEFAULT_TO = "2026-07-29T00:00:00+00:00"
# The model live actually ran on for these dates (confirmed from real day
# logs: "model":"claude-sonnet-4-6" on every expert/judge call 07-06..08-07).
# Pinned explicitly rather than left to the DB "prod" model-config default,
# which may have moved on since the freeze window and would silently make
# this a test of a DIFFERENT model than the one being audited.
LIVE_MODEL = "claude-sonnet-4-6"

RESULTS_DIR = REPO_ROOT / "results" / "freeze_backtest_2026-07-13_2026-07-29"


def _utc(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def run_one_asset(
    asset: str,
    start: datetime,
    end: datetime,
    *,
    initial_capital: float,
    interval_hours: int,
    model: str,
) -> dict:
    test_name = f"freeze_audit_{start.date()}_{end.date()}_{asset}"
    config = {
        "candle_count_5m": 100,
        "candle_count_15m": 50,
        "use_code_sniper": True,
        "interval_hours": interval_hours,
        "lot_size": 1.0,
        "initial_capital": initial_capital,
        "risk_pct": CS_DEFAULT_RISK_PCT,
        "use_risk_sizing": True,
        "gpt_config": {"model": model},
    }
    backtest_id = await backtest_repo.create_backtest(
        test_name=test_name, config=config, asset=asset, start_date=start, end_date=end
    )
    print(f"\n{'='*70}\n{asset}  backtest_id={backtest_id}\n{'='*70}", flush=True)
    logger.info(f"[RUN] {asset} starting", extra={"backtest_id": backtest_id, "test_name": test_name})

    t0 = datetime.now(timezone.utc)
    outcome = {"asset": asset, "backtest_id": backtest_id, "test_name": test_name, "status": "unknown"}
    try:
        runner = BacktestRunner(
            asset=asset,
            start_date=start,
            end_date=end,
            candle_count_5m=100,
            candle_count_15m=50,
            use_code_sniper=True,
            gpt_config={"model": model},
            initial_capital=initial_capital,
            risk_pct=CS_DEFAULT_RISK_PCT,
            use_risk_sizing=True,
        )
        await runner.execute_backtest(test_name=test_name, interval_hours=interval_hours, backtest_id=backtest_id)
        outcome["status"] = "completed"
    except Exception as exc:  # noqa: BLE001 — isolate one asset's failure from the rest
        logger.error(f"[RUN] {asset} FAILED: {exc}", extra={"backtest_id": backtest_id}, exc_info=True)
        outcome["status"] = "error"
        outcome["error"] = f"{type(exc).__name__}: {exc}"
        try:
            await backtest_repo.update_backtest_status(backtest_id, "failed", error_message=str(exc))
        except Exception:
            pass

    elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
    doc = await backtest_repo.get_backtest(backtest_id)
    if doc:
        outcome.update({
            "iterations": doc.get("iterations", 0),
            "total_signals": doc.get("total_signals", 0),
            "wins": doc.get("wins", 0),
            "losses": doc.get("losses", 0),
            "total_pnl": doc.get("total_pnl", 0.0),
            "max_drawdown": doc.get("max_drawdown", 0.0),
            "win_rate": doc.get("win_rate", 0.0),
            "avg_rr": doc.get("avg_rr", 0.0),
            "mongo_status": doc.get("status"),
        })
    outcome["elapsed_sec"] = round(elapsed, 1)

    print(
        f"{asset} done in {elapsed/60:.1f}min  status={outcome['status']}  "
        f"iterations={outcome.get('iterations')}  signals={outcome.get('total_signals')}  "
        f"W/L={outcome.get('wins')}/{outcome.get('losses')}  pnl=${outcome.get('total_pnl', 0):.2f}",
        flush=True,
    )
    return outcome


async def main_async(args: argparse.Namespace) -> int:
    assets = [a.strip().upper() for a in args.assets.split(",") if a.strip()]
    start, end = _utc(args.start), _utc(args.end)

    if args.smoke:
        assets = assets[:1]
        end = start.replace(hour=0, minute=0) + timedelta(days=1)
        print(f"SMOKE TEST: {assets[0]} only, {start} -> {end}")

    print(f"Assets: {assets}\nWindow: {start} -> {end}\nModel: {args.model}\nInitial capital: ${args.initial_capital}")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for asset in assets:
        outcome = await run_one_asset(
            asset, start, end,
            initial_capital=args.initial_capital,
            interval_hours=args.interval_hours,
            model=args.model,
        )
        results.append(outcome)
        # Persist incrementally so a later asset's crash doesn't lose earlier results.
        summary_path = RESULTS_DIR / ("smoke_summary.json" if args.smoke else "summary.json")
        summary_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")

    n_ok = sum(1 for r in results if r["status"] == "completed")
    print(f"\n{'='*70}\n{n_ok}/{len(results)} assets completed.  Summary: {summary_path}\n{'='*70}")
    return 0 if n_ok == len(results) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--assets", default=",".join(DEFAULT_ASSETS))
    ap.add_argument("--start", default=DEFAULT_FROM)
    ap.add_argument("--end", default=DEFAULT_TO)
    ap.add_argument("--initial-capital", type=float, default=2000.0)
    ap.add_argument("--interval-hours", type=int, default=1)
    ap.add_argument("--model", default=LIVE_MODEL)
    ap.add_argument("--smoke", action="store_true", help="tiny 1-day/1-asset slice to validate the pipeline")
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
