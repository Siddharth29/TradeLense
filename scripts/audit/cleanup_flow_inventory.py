"""Read-only flow census and source structure audit, using only the standard library.

Run from the repository root. Log stages indicate observed telemetry, not confirmed
broker actions. Fixtures contain whitelisted structural fields, never raw messages,
prompts, account IDs, prices or generated code. Historical source versions remain
unknown unless independently reconciled.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

if __package__:
    from .trade_log_events import trade_stage
else:
    from trade_log_events import trade_stage


STAGES = ("startup", "decision", "armed", "fill", "partial", "exit", "expiry", "recovery", "connection_failure")
SOURCE_FILES = (
    "apps/backend/api.py", "apps/backend/agents/runner.py",
    "packages/core/backtest_runner.py", "packages/core/multi_expert.py",
    "packages/core/services/code_sniper_monitor.py",
    "packages/core/services/trade_monitor.py", "packages/core/code_sniper_executor.py",
    "packages/core/operational_ledger.py", "packages/core/gpt_client.py",
    "packages/core/logger.py", "src/common/logger.py",
    "scripts/analyze_trades_from_logs.py",
)


@dataclass(frozen=True)
class EvidenceLocation:
    file: str
    line: int
    line_sha256: str


def classify_record(record: dict[str, Any]) -> str | None:
    """Use explicit events first; narrowly matched diagnostics are secondary."""
    event = record.get("event")
    payload = record.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    if event in ("cycle", "gpt_response"):
        return "decision"
    if event in ("trade_open", "trade_exit"):
        return trade_stage(event, payload)
    if event == "strategy_expiry":
        return "expiry"
    msg = record.get("msg")
    if not isinstance(msg, str):
        return None
    if msg == "startup":
        return "startup"
    if msg == "Strategy added":
        return "armed"
    if msg in ("Trade executed", "Code sniper trade executed"):
        return "fill"
    if msg.startswith(("MT5 LIVE: TP1 partial exit recorded", "TP2 partial booked")):
        return "partial"
    if msg.startswith("Updated comprehensive trade exit"):
        return "exit"
    if msg in ("Strategy expired", "Code sniper strategy expired"):
        return "expiry"
    if msg.lower().startswith("recovery:"):
        return "recovery"
    if msg.startswith(("MT5 auto-connect failed", "CRITICAL: MT5 reconnection failed")):
        return "connection_failure"
    return None


def sanitized_fixture(record: dict[str, Any], stage: str, location: EvidenceLocation) -> dict[str, Any]:
    """Preserve event shape and linkage availability, with no free-form values."""
    payload = record.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    ctx = record.get("ctx")
    ctx = ctx if isinstance(ctx, dict) else {}
    known_events = {"cycle", "gpt_response", "trade_open", "trade_exit", "strategy_expiry"}
    identifiers = ("corr_id", "cycle_id", "strategy_id", "position_ticket", "trade_id", "order_id")
    level = record.get("lvl", record.get("level"))
    return {
        "stage": stage,
        "event": record.get("event") if isinstance(record.get("event"), str) and record.get("event") in known_events else None,
        "level": level if level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL") else None,
        "timestamp_present": bool(record.get("ts") or record.get("time")),
        "payload_is_object": isinstance(record.get("payload"), dict),
        "linkage_present": {key: any(bool(mapping.get(key)) for mapping in (record, ctx, payload)) for key in identifiers},
        "exit_fields_present": {key: key in payload for key in ("pnl", "exit_reason", "entry_price", "exit_price")},
        "source": asdict(location),
        "historical_source_revision": None,
    }


def inspect_source(path: Path, display_path: str) -> dict[str, Any]:
    """Report method spans and shadowed definitions; no module imports or execution."""
    raw = path.read_bytes()
    tree = ast.parse(raw.decode("utf-8-sig"), filename=display_path)
    functions = []
    duplicates = []
    routes: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                    and isinstance(decorator.func.value, ast.Name) and decorator.func.value.id == "app"
                    and decorator.func.attr in ("get", "post", "put", "patch", "delete")
                    and decorator.args and isinstance(decorator.args[0], ast.Constant)
                    and isinstance(decorator.args[0].value, str)):
                routes[(decorator.func.attr, decorator.args[0].value)].append({"name": node.name, "line": node.lineno})

    def walk_scope(node: ast.AST, scope: str) -> None:
        definitions: dict[str, list[int]] = defaultdict(list)
        for child in getattr(node, "body", []):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                definitions[child.name].append(child.lineno)
                qualified = f"{scope}.{child.name}" if scope else child.name
                if not isinstance(child, ast.ClassDef):
                    functions.append({"name": qualified, "line": child.lineno,
                                      "span_lines": child.end_lineno - child.lineno + 1,
                                      "async": isinstance(child, ast.AsyncFunctionDef)})
                walk_scope(child, qualified)
        for name, lines in definitions.items():
            if len(lines) > 1:
                duplicates.append({"scope": scope or "module", "name": name, "lines": lines})

    walk_scope(tree, "")
    return {"file": display_path, "sha256": hashlib.sha256(raw).hexdigest(),
            "lines": len(raw.splitlines()), "function_count": len(functions),
            "largest_functions": sorted(functions, key=lambda item: -item["span_lines"])[:5],
            "duplicate_definitions": duplicates,
            "duplicate_routes": [{"method": method, "path": route, "handlers": handlers}
                                 for (method, route), handlers in routes.items() if len(handlers) > 1]}


def inventory(root: Path, paths: list[Path], samples_per_stage: int = 3) -> tuple[dict[str, Any], dict[str, Any]]:
    """Single streaming pass, keeping bounded examples and per-file counts."""
    samples: dict[str, list[dict[str, Any]]] = {stage: [] for stage in STAGES}
    files = []
    totals: Counter[str] = Counter()
    for path in paths:
        counts: Counter[str] = Counter()
        stages: Counter[str] = Counter()
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for number, raw in enumerate(fh, 1):
                digest.update(raw)
                counts["lines"] += 1
                try:
                    record = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    counts["non_json_lines"] += 1
                    continue
                if not isinstance(record, dict):
                    counts["non_object_json"] += 1
                    continue
                counts["json_objects"] += 1
                stage = classify_record(record)
                if stage is None:
                    counts["unclassified_objects"] += 1
                    continue
                stages[stage] += 1
                if len(samples[stage]) < samples_per_stage:
                    location = EvidenceLocation(path.relative_to(root).as_posix(), number, hashlib.sha256(raw).hexdigest())
                    samples[stage].append(sanitized_fixture(record, stage, location))
        totals.update(stages)
        files.append({"file": path.relative_to(root).as_posix(), "sha256": digest.hexdigest(),
                      "counts": dict(counts), "stages": dict(stages),
                      "historical_source_revision": None})
    report = {
        "schema_version": 1,
        "interpretation": "Telemetry census only. Stages may overlap the same action across files; no deduplication or position reconciliation.",
        "files": files, "stage_totals": {stage: totals[stage] for stage in STAGES},
        "missing_stages": [stage for stage in STAGES if not totals[stage]],
        "sources": [inspect_source(root / name, name) for name in SOURCE_FILES],
    }
    fixtures = {"schema_version": 1, "purpose": "Sanitized historical event shapes; independent samples, not a joined trade lifecycle.",
                "samples": [sample for stage in STAGES for sample in samples[stage]]}
    return report, fixtures


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New output JSON path")
    parser.add_argument("--fixtures", type=Path, required=True, help="New sanitized fixtures JSON path")
    args = parser.parse_args()
    if args.output.resolve() == args.fixtures.resolve():
        parser.error("Output and fixture paths must differ")
    if args.output.exists() or args.fixtures.exists():
        parser.error("Outputs must be new paths to preserve prior evidence")
    root = Path(__file__).resolve().parents[2]
    paths = sorted(path for path in (root / "results/logs").iterdir() if path.suffix in (".log", ".jsonl") and path.is_file())
    report, fixtures = inventory(root, paths)
    for path, data in ((args.output, report), (args.fixtures, fixtures)):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
    print(f"Inventoried {len(paths)} files; saved {len(fixtures['samples'])} sanitized samples.")


if __name__ == "__main__":
    main()
