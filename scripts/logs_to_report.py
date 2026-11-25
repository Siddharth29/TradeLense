#!/usr/bin/env python3
"""Reconstruct a trading-performance report from the structured JSON event log.

This is the log-only counterpart to the old MongoDB aggregation reads. It reads
the JSON-lines logs emitted by ``src.common.logger`` (one object per line) and
reads current exit events and historical closed legs labeled ``trade_open``.
It reports the app's logged leg P&L. Broker deals are needed for reconciliation.

Usage
-----
    python scripts/logs_to_report.py                       # results/logs/*.log
    python scripts/logs_to_report.py path/to/run.log ...   # explicit files
    python scripts/logs_to_report.py --glob 'results/logs/backtest_2026*.log'
    python scripts/logs_to_report.py --exclude-partials     # drop TP1/TP2 legs

Only the standard library is used so it runs anywhere the logs are shipped.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from math import isfinite
from typing import Any, Dict, Iterable, List, Optional

if __package__:
    from .audit.trade_log_events import trade_stage
else:
    from audit.trade_log_events import trade_stage


def _iter_records(paths: Iterable[str]) -> Iterable[Dict[str, Any]]:
    """Stream structured event records, ignoring diagnostics and malformed rows."""
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or '"event:' not in line and '"event"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(rec, dict):
                        continue
                    payload = rec.get("payload")
                    if isinstance(payload, dict):
                        yield rec
        except FileNotFoundError:
            continue


def _iter_events(paths: Iterable[str], want: str) -> Iterable[Dict[str, Any]]:
    """Yield payloads for the requested event, preserving file and line order."""
    for rec in _iter_records(paths):
        if rec.get("event") == want:
            yield rec["payload"]


def _f(x: Any) -> Optional[float]:
    try:
        value = float(x)
        return value if isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _profit_factor(wins_sum: float, losses_sum: float) -> str:
    if losses_sum == 0:
        return "inf" if wins_sum > 0 else "0.00"
    return f"{wins_sum / abs(losses_sum):.2f}"


def _exit_row(e: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalize one closed leg using the report's existing field conventions."""
    reason = str(e.get("exit_reason") or "").strip().upper()
    pnl = _f(e.get("pnl"))
    if pnl is None:
        return None
    row = {
        "asset": e.get("asset") or e.get("symbol") or "?",
        "side": str(e.get("side") or "?").lower(),
        "entry_price": _f(e.get("entry_price")),
        "exit_price": _f(e.get("exit_price")),
        "pnl": pnl,
        "reason": reason or "?",
    }
    for key in ("trade_reason", "trigger_reason", "cycle_id", "position_ticket"):
        if key in e:
            row[key] = e[key]
    if e.get("broker_cost") is not None:
        cost = e["broker_cost"]
        row.update(broker_charges=cost.get("total"), broker_currency=cost.get("currency"),
                   broker_cost_status=cost.get("status"))
    if e.get("ai_cost") is not None:
        cost = e["ai_cost"]
        row.update(ai_cost_usd=cost.get("total_usd"), ai_cost_status=cost.get("status"))
    return row


@dataclass
class LogSummary:
    """Offline report inputs; counts describe events, rows describe closed legs."""

    source_files: int
    opens: int = 0
    cycles: int = 0
    rows: List[Dict[str, Any]] = field(default_factory=list)


def collect_log_data(paths: List[str], exclude_partials: bool) -> LogSummary:
    """Read each source once for both Markdown and CSV output."""
    summary = LogSummary(source_files=len(paths))
    for rec in _iter_records(paths):
        event = rec.get("event")
        if event == "cycle":
            summary.cycles += 1
            continue
        stage = trade_stage(event, rec["payload"])
        if stage == "fill":
            summary.opens += 1
        elif stage == "exit" or (stage == "partial" and not exclude_partials):
            row = _exit_row(rec["payload"])
            if row is not None:
                summary.rows.append(row)
    return summary


def collect_rows(paths: List[str], exclude_partials: bool) -> List[Dict[str, Any]]:
    """Reconstruct one row per logged closed leg."""
    return collect_log_data(paths, exclude_partials).rows


def build_report(paths: List[str], exclude_partials: bool) -> str:
    return render_report(collect_log_data(paths, exclude_partials), exclude_partials)


def render_report(summary: LogSummary, exclude_partials: bool) -> str:
    """Format collected evidence without reading files or contacting services."""
    rows = summary.rows

    out: List[str] = []
    out.append("# Trade Performance Report (reconstructed from logs)\n")
    out.append(f"- Source files: {summary.source_files}")
    out.append(f"- Cycles logged: {summary.cycles}")
    out.append(f"- Trade-open events: {summary.opens}")
    out.append(f"- Closed legs analysed: {len(rows)}"
               + ("  (partials excluded)" if exclude_partials else "  (partials included)"))

    if not rows:
        out.append("\n_No closed-trade events found in the provided logs._")
        return "\n".join(out)

    out.append("\n_P/L is summed from app log records. Reconcile with broker deals for account results._")

    wins = [r for r in rows if r["pnl"] > 0]
    losses = [r for r in rows if r["pnl"] < 0]
    net = sum(r["pnl"] for r in rows)
    wins_sum = sum(r["pnl"] for r in wins)
    losses_sum = sum(r["pnl"] for r in losses)
    n = len(rows)

    out.append("\n## Headline\n")
    out.append(f"| Metric | Value |")
    out.append(f"|---|---|")
    out.append(f"| Closed legs | {n} |")
    out.append(f"| Net P/L | {net:+.2f} |")
    out.append(f"| Win rate | {100.0 * len(wins) / n:.1f}% ({len(wins)}W / {len(losses)}L) |")
    out.append(f"| Profit factor | {_profit_factor(wins_sum, losses_sum)} |")
    out.append(f"| Avg win | {(wins_sum / len(wins)) if wins else 0:+.2f} |")
    out.append(f"| Avg loss | {(losses_sum / len(losses)) if losses else 0:+.2f} |")
    out.append(f"| Expectancy / leg | {net / n:+.2f} |")
    out.append(f"| Largest win | {max((r['pnl'] for r in rows), default=0):+.2f} |")
    out.append(f"| Largest loss | {min((r['pnl'] for r in rows), default=0):+.2f} |")

    # By symbol
    by_sym: Dict[str, List[float]] = defaultdict(list)
    for r in rows:
        by_sym[r["asset"]].append(r["pnl"])
    out.append("\n## By symbol\n")
    out.append("| Symbol | Legs | Net P/L | Win rate |")
    out.append("|---|---|---|---|")
    for sym in sorted(by_sym, key=lambda s: -sum(by_sym[s])):
        pl = by_sym[sym]
        w = sum(1 for x in pl if x > 0)
        out.append(f"| {sym} | {len(pl)} | {sum(pl):+.2f} | {100.0 * w / len(pl):.1f}% |")

    # By direction
    by_dir: Dict[str, List[float]] = defaultdict(list)
    for r in rows:
        by_dir[r["side"]].append(r["pnl"])
    out.append("\n## By direction\n")
    out.append("| Direction | Legs | Net P/L | Avg/leg |")
    out.append("|---|---|---|---|")
    for d in sorted(by_dir):
        pl = by_dir[d]
        out.append(f"| {d} | {len(pl)} | {sum(pl):+.2f} | {sum(pl) / len(pl):+.2f} |")

    # By exit reason (the SL-vs-rule diagnostic)
    by_reason: Dict[str, List[float]] = defaultdict(list)
    for r in rows:
        by_reason[r["reason"]].append(r["pnl"])
    out.append("\n## By exit type\n")
    out.append("| Exit type | Legs | Net P/L | Avg/leg |")
    out.append("|---|---|---|---|")
    for rsn in sorted(by_reason, key=lambda s: sum(by_reason[s])):
        pl = by_reason[rsn]
        out.append(f"| {rsn} | {len(pl)} | {sum(pl):+.2f} | {sum(pl) / len(pl):+.2f} |")

    return "\n".join(out)


def _resolve_paths(args: argparse.Namespace) -> List[str]:
    if args.files:
        return args.files
    if args.glob:
        return sorted(glob.glob(args.glob))
    base = os.path.join("results", "logs")
    # Read both the app_logger event stream (*.log) and the durable stdlib
    # structured capture (*.jsonl) so nothing is missed.
    return sorted(
        glob.glob(os.path.join(base, "*.log"))
        + glob.glob(os.path.join(base, "*.jsonl"))
    )


def _write_csv(rows: List[Dict[str, Any]], path: str) -> None:
    """Dump reconstructed trade rows to CSV for offline DuckDB/pandas analysis."""
    import csv

    cols = ["asset", "side", "entry_price", "exit_price", "pnl", "reason",
            "trade_reason", "trigger_reason", "cycle_id", "position_ticket",
            "broker_charges", "broker_currency", "broker_cost_status", "ai_cost_usd", "ai_cost_status"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("files", nargs="*", help="Explicit log files (default: results/logs/*.log + *.jsonl)")
    ap.add_argument("--glob", help="Glob pattern for log files")
    ap.add_argument("--exclude-partials", action="store_true",
                    help="Drop TP1_PARTIAL / TP2_PARTIAL legs from the report")
    ap.add_argument("-o", "--output", help="Write the Markdown report to this file instead of stdout")
    ap.add_argument("--csv", help="Also dump reconstructed trade rows to this CSV path")
    args = ap.parse_args()

    paths = _resolve_paths(args)
    summary = collect_log_data(paths, exclude_partials=args.exclude_partials)
    report = render_report(summary, exclude_partials=args.exclude_partials)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(report + "\n")
        print(f"Wrote report to {args.output}")
    else:
        print(report)
    if args.csv:
        _write_csv(summary.rows, args.csv)
        print(f"Wrote trade rows to {args.csv}")


if __name__ == "__main__":
    main()
