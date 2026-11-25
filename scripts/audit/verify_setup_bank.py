"""Compare frozen typed/legacy setup decisions without calling a model or broker."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from packages.core.typed_setup import compile_setup, evaluate_setup, VERSION
from packages.core.schemas_trade import MultiExpertSignal, CodeSniperSignal
from packages.core.code_sniper_executor import CodeSniperExecutor
from packages.core.operational_ledger import utc
from packages.core.config import CS_EXPECTED_HISTORICAL_CANDLES


def verify(path):
    raw = Path(path).read_bytes()
    bank = json.loads(raw)
    outputs = []
    executor = CodeSniperExecutor()
    for case in bank["snapshots"]:
        started = time.perf_counter()
        proposal = MultiExpertSignal.model_validate(case["proposal"])
        compiled = compile_setup(proposal, case["c5"], case["c15"], case["config"])
        legacy = CodeSniperSignal(code=case["legacy_code"], metadata={}, ttl_minutes=proposal.ttl_minutes, confidence=proposal.confidence)
        row = {"id": case["id"], "snapshot_id": compiled.metadata["snapshot_id"], "compiler": VERSION,
               "model_versions": case.get("model_versions"), "prompt_version": case.get("prompt_version"),
               "compile_ms": (time.perf_counter()-started)*1000, "typed": [], "legacy": [],
               "typed_first_trigger": None, "legacy_first_trigger": None}
        history = list(case["c5"])
        typed_done = legacy_done = False
        for current in case["future_bars"]:
            stamp = utc(current["time"])+timedelta(minutes=5)
            if not typed_done:
                t = time.perf_counter()
                result = evaluate_setup(compiled.metadata, history[-71:], current)
                row["typed"].append({"at": stamp.isoformat(), "status": result["typed_status"],
                                     "direction": result["direction"], "execution_ms": (time.perf_counter()-t)*1000})
                typed_done = result["typed_status"] in ("triggered", "invalidated", "expired", "missed_trigger")
                if result["typed_status"] == "triggered":
                    row["typed_first_trigger"] = stamp.isoformat()
            if not legacy_done:
                if stamp >= utc(compiled.metadata["expires_at"]):
                    row["legacy"].append({"at": stamp.isoformat(), "status": "expired"})
                    legacy_done = True
                else:
                    t = time.perf_counter()
                    side = executor.dry_run_direction(legacy, current, history[-CS_EXPECTED_HISTORICAL_CANDLES:])
                    row["legacy"].append({"at": stamp.isoformat(), "direction": side,
                                          "execution_ms": (time.perf_counter()-t)*1000})
                    if side in ("buy", "sell", "long", "short"):
                        legacy_done = True
                        row["legacy_first_trigger"] = stamp.isoformat()
            history.append(current)
        row["expected_contract_passed"] = (row["typed_first_trigger"] == case["expected_typed_first_trigger"] and
                                           row["typed"][-1]["status"] == case["expected_typed_terminal_status"])
        outputs.append(row)
    return {"bank_sha256": hashlib.sha256(raw).hexdigest(), "provenance": bank["provenance"],
            "cases": outputs, "passed": all(x["expected_contract_passed"] for x in outputs),
            "scope": "Paired trigger/invalidation contracts and local execution latency. No model quality or profitability inference."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bank")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = verify(args.bank)
    Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"{len(result['cases'])} paired snapshots; passed={result['passed']}")
    raise SystemExit(0 if result["passed"] else 1)
