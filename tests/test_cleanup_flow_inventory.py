"""Read-only evidence tooling and source-isolated behavior characterization."""

import ast
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.audit import cleanup_flow_inventory as audit

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("record,stage", [
    ({"msg": "startup"}, "startup"),
    ({"event": "cycle"}, "decision"),
    ({"msg": "Strategy added"}, "armed"),
    ({"event": "trade_open"}, "fill"),
    ({"event": "trade_open", "payload": {"trade_status": "closed", "exit_reason": "TP2_PARTIAL"}}, "partial"),
    ({"event": "trade_open", "payload": {"trade_status": "cancelled"}}, None),
    ({"event": "trade_exit", "payload": {"exit_reason": "tp1_partial"}}, "partial"),
    ({"event": "trade_exit", "payload": {"exit_reason": "SL"}}, "exit"),
    ({"event": "strategy_expiry"}, "expiry"),
    ({"msg": "recovery: broker not ready for XAUUSD"}, "recovery"),
    ({"msg": "MT5 auto-connect failed (keepalive will retry)"}, "connection_failure"),
    ({"msg": "JUDGE PROMPT: recover after partial exit with armed strategy"}, None),
    ({"msg": {"invalid": "shape"}, "event": []}, None),
])
def test_stage_classification_does_not_interpret_prompt_text(record, stage):
    assert audit.classify_record(record) == stage


def test_sanitizer_keeps_shape_and_removes_sensitive_values():
    secret = "private-account-prompt-code"
    rec = {"msg": secret, "event": "trade_exit", "ts": secret, "lvl": "INFO",
           "ctx": {"login": secret, "cycle_id": secret},
           "payload": {"pnl": 9001, "entry_price": 123456, "position_ticket": secret,
                       "code": secret, "exit_reason": secret}, "corr_id": secret}
    location = audit.EvidenceLocation("results/logs/sample.log", 4, "abc")
    fixture = audit.sanitized_fixture(rec, "exit", location)
    text = json.dumps(fixture)
    assert secret not in text and "9001" not in text and "123456" not in text
    assert fixture["linkage_present"]["position_ticket"] is True
    assert fixture["linkage_present"]["cycle_id"] is True
    assert fixture["exit_fields_present"]["pnl"] is True
    assert fixture["historical_source_revision"] is None
    rec["event"] = []
    assert audit.sanitized_fixture(rec, "exit", location)["event"] is None


def test_inventory_is_read_only_bounded_and_marks_missing_coverage(tmp_path, monkeypatch):
    path = tmp_path / "run.log"
    raw = ('plain text\n[]\n{bad\n' + '\n'.join(json.dumps({"event": "cycle", "payload": {}}) for _ in range(5)) + '\n').encode()
    path.write_bytes(raw)
    monkeypatch.setattr(audit, "SOURCE_FILES", ())
    summary, fixtures = audit.inventory(tmp_path, [path], samples_per_stage=2)
    assert path.read_bytes() == raw
    assert summary["files"][0]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert summary["files"][0]["counts"]["non_object_json"] == 1
    assert summary["files"][0]["counts"]["non_json_lines"] == 2
    assert summary["stage_totals"]["decision"] == 5
    assert "fill" in summary["missing_stages"]
    assert len(fixtures["samples"]) == 2
    assert fixtures["samples"][0]["source"]["line"] == 4


def test_source_audit_distinguishes_separate_scopes(tmp_path):
    path = tmp_path / "sample.py"
    path.write_text("class A:\n def run(self): pass\n def run(self): pass\nclass B:\n def run(self): pass\n")
    result = audit.inspect_source(path, "sample.py")
    assert result["duplicate_definitions"] == [{"scope": "A", "name": "run", "lines": [2, 3]}]
    assert result["function_count"] == 3


def source_functions(path, names, class_name=None):
    """Compile only reviewed source functions; never import the engine or log code."""
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8-sig"))
    body = tree.body if class_name is None else next(n.body for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    return [n for n in body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]


def test_runner_has_one_execution_mode_method_with_preserved_behavior():
    functions = source_functions("apps/backend/agents/runner.py", {"set_execution_mode"}, "AgentRunner")
    assert len(functions) == 1
    namespace = {"datetime": __import__("datetime").datetime,
                 "app_logger": SimpleNamespace(info=lambda *a, **k: None)}
    exec(compile(ast.Module(body=functions, type_ignores=[]), "runner-mode", "exec"), namespace)
    runner = SimpleNamespace(_log_ctx={})
    with pytest.raises(ValueError, match="Invalid execution mode"):
        namespace["set_execution_mode"](runner, "run_once")
    namespace["set_execution_mode"](runner, "run_now")
    assert runner._execution_mode == "run_now"


def test_current_provider_default_is_process_global():
    functions = source_functions("packages/core/gpt_client.py", {"set_provider", "get_provider"})
    namespace = {"_active_provider": "openai"}
    exec(compile(ast.Module(body=functions, type_ignores=[]), "provider-default", "exec"), namespace)
    # Two callers share one namespace. Explicit model-name routing can override
    # this default; this is evidence of shared state, not proof of misrouting.
    agent_a_read_default = namespace["get_provider"]
    namespace["set_provider"]("gemini")
    assert agent_a_read_default() == "gemini"


def test_current_monitor_and_ledger_symbol_keys_disagree_on_raw_suffix():
    from typing import Any
    from packages.core.operational_ledger import asset_key
    functions = source_functions("packages/core/services/trade_monitor.py", {"_asset_key"})
    namespace = {"Any": Any}
    exec(compile(ast.Module(body=functions, type_ignores=[]), "monitor-symbol-key", "exec"), namespace)
    assert namespace["_asset_key"]("XAUUSDm") == asset_key("XAUUSDm") == "XAUUSD"
    assert namespace["_asset_key"]("XAUUSD.raw") == "XAUUSD"
    assert asset_key("XAUUSD.raw") == "XAUUSD.RAW"


def test_export_route_has_one_handler_with_preserved_response(monkeypatch):
    import sys
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    service = SimpleNamespace(list_exports=lambda: ["sample.csv"], get_export_dir=lambda: "/synthetic/exports")
    monkeypatch.setitem(sys.modules, "packages.core.services.export_service",
                        SimpleNamespace(get_export_service=lambda: service))
    functions = source_functions("apps/backend/api.py", {"list_exports"})
    app = FastAPI()
    namespace = {"app": app, "HTTPException": HTTPException}
    exec(compile(ast.Module(body=functions, type_ignores=[]), "export-routes", "exec"), namespace)
    assert len([r for r in app.routes if r.path == "/api/export/list"]) == 1
    with TestClient(app) as client:
        response = client.get("/api/export/list")
    assert response.status_code == 200
    assert response.json() == {"exports": ["sample.csv"], "count": 1}
    # The Python binding and registered endpoint now describe the same contract.
    import asyncio
    assert asyncio.run(namespace["list_exports"]()) == response.json()


def test_source_audit_confirms_no_duplicate_route_registrations():
    result = audit.inspect_source(ROOT / "apps/backend/api.py", "apps/backend/api.py")
    assert result["duplicate_routes"] == []


def test_saved_fixture_schema_has_no_raw_log_content():
    path = ROOT / "tests/fixtures/cleanup_flow_shapes_2026_10_09.json"
    fixture = json.loads(path.read_text(encoding="utf-8"))
    assert fixture["schema_version"] == 1
    assert {sample["stage"] for sample in fixture["samples"]} == set(audit.STAGES)
    for sample in fixture["samples"]:
        assert set(sample) == {"stage", "event", "level", "timestamp_present", "payload_is_object",
                               "linkage_present", "exit_fields_present", "source", "historical_source_revision"}
        assert sample["historical_source_revision"] is None
        assert len(sample["source"]["line_sha256"]) == 64
