"""Tests for log-only analytics persistence (PERSIST_ANALYTICS_TO_MONGO).

Covers the central gate, the per-repo gating + durable event emission, the
in-process open-trades registry that keeps recent_trades_memory working without
Mongo, and the logs->report reconstructor.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from src.common import logger as evlog
import packages.db.persistence_gate as gate
from packages.db.repositories import comprehensive_logging as comp
from packages.db.repositories.multi_expert_cycles import multi_expert_cycles_repo
from packages.core.services import recent_trades_memory as rtm
from packages.db.models import MultiExpertCycleDoc, ExpertInsight, JudgeOutcome


# ── helpers ────────────────────────────────────────────────────────────────

class _EventSink:
    """Synchronously capture structured event records emitted during a test."""

    def __init__(self):
        self.records = []

    def __enter__(self):
        evlog.add_sink(self.records.append)
        return self

    def __exit__(self, *exc):
        evlog.remove_sink(self.records.append)

    def of(self, event: str):
        return [r for r in self.records if r.get("event") == event]


def _no_mongo(monkeypatch):
    """Make any Mongo client acquisition fail loudly so log-only paths can't use it."""
    def _boom(*a, **k):
        raise AssertionError("Mongo client acquired in log-only mode")
    monkeypatch.setattr(comp, "get_client", _boom)


def _utc():
    return datetime.now(timezone.utc)


# ── flag resolution ─────────────────────────────────────────────────────────

def test_flag_env_override(monkeypatch):
    monkeypatch.setenv("PERSIST_ANALYTICS_TO_MONGO", "0")
    assert gate.analytics_persistence_enabled() is False
    monkeypatch.setenv("PERSIST_ANALYTICS_TO_MONGO", "true")
    assert gate.analytics_persistence_enabled() is True


# ── repo gating: log-only mode skips Mongo, still logs the full record ───────

def test_store_comprehensive_trade_logonly(monkeypatch):
    monkeypatch.setenv("PERSIST_ANALYTICS_TO_MONGO", "0")
    _no_mongo(monkeypatch)
    with _EventSink() as sink:
        tid = asyncio.run(comp.store_comprehensive_trade(
            symbol="XAUUSD", asset="XAUUSD", quantity=0.01, price=4000.0,
            entry_price=4000.0, agent_id="a1", prompt_hash="h",
            signal_received_at=_utc(), trade_executed_at=_utc(),
            side="sell", position_ticket="TCK1", trade_status="open",
        ))
    assert tid.startswith("logonly_")
    opens = sink.of("trade_open")
    assert len(opens) == 1 and opens[0]["payload"]["entry_price"] == 4000.0


def test_closed_partial_is_logged_as_an_exit_with_its_ticket(monkeypatch):
    monkeypatch.setenv("PERSIST_ANALYTICS_TO_MONGO", "0")
    _no_mongo(monkeypatch)
    with _EventSink() as sink:
        asyncio.run(comp.store_comprehensive_trade(
            symbol="XAUUSD", asset="XAUUSD", quantity=0.01, price=3990.0,
            entry_price=4000.0, agent_id="a1", prompt_hash="h",
            signal_received_at=_utc(), trade_executed_at=_utc(), trade_closed_at=_utc(),
            side="sell", position_ticket="partial-test", cycle_id="cycle-test",
            trade_status="closed", exit_reason="TP1_PARTIAL", pnl=10.0,
        ))
    comp.pop_open_trade("partial-test")
    assert sink.of("trade_open") == []
    [exit_event] = sink.of("trade_exit")
    assert exit_event["payload"]["exit_reason"] == "TP1_PARTIAL"
    assert exit_event["ctx"]["position_ticket"] == "partial-test"
    assert exit_event["ctx"]["cycle_id"] == "cycle-test"


def test_insert_cycle_logonly(monkeypatch):
    monkeypatch.setenv("PERSIST_ANALYTICS_TO_MONGO", "0")
    # _get_collection would acquire a client; make it explode if reached.
    async def _boom(*a, **k):
        raise AssertionError("Mongo reached in log-only cycle insert")
    monkeypatch.setattr(multi_expert_cycles_repo, "_get_collection", _boom)
    doc = MultiExpertCycleDoc(
        cycle_id="cyc1", mode="multi_expert_code_sniper", agent_id="a1",
        asset="XAUUSD", symbol="XAUUSD", started_at=_utc(),
        experts=[], judge=JudgeOutcome(selected="flat", validation_summary={}),
        status="flat",
    )
    with _EventSink() as sink:
        cid = asyncio.run(multi_expert_cycles_repo.insert_cycle(doc))
    assert cid.startswith("logonly_")
    assert sink.of("cycle") and sink.of("cycle")[0]["payload"]["cycle_id"] == "cyc1"


# ── recent_trades_memory survives without Mongo ─────────────────────────────

def test_exit_logonly_feeds_recent_trades(monkeypatch):
    monkeypatch.setenv("PERSIST_ANALYTICS_TO_MONGO", "0")
    _no_mongo(monkeypatch)
    rtm.reset()

    # Open (registers entry context in the in-process registry).
    asyncio.run(comp.store_comprehensive_trade(
        symbol="XAGUSD", asset="XAGUSD", quantity=0.01, price=60.0,
        entry_price=60.0, agent_id="a1", prompt_hash="h",
        signal_received_at=_utc(), trade_executed_at=_utc(),
        side="sell", position_ticket="TCK9", trade_status="open",
    ))
    assert not rtm.has_recent_trades("XAGUSD")

    # Close by the same ticket -> should feed the ring buffer with entry context.
    with _EventSink() as sink:
        ok = asyncio.run(comp.update_comprehensive_trade_exit(
            symbol="XAGUSD", exit_price=59.0, exit_reason="TP", pnl=5.0,
            position_ticket="TCK9",
        ))
    assert ok is True
    assert rtm.has_recent_trades("XAGUSD")
    views = rtm.get_recent_trades("XAGUSD")
    assert views and views[0]["side"] == "sell"
    assert sink.of("trade_exit")


# ── enabled mode still writes to Mongo ──────────────────────────────────────

def test_enabled_mode_writes_mongo(monkeypatch):
    monkeypatch.setenv("PERSIST_ANALYTICS_TO_MONGO", "1")
    inserts = []

    class _Res:
        inserted_id = "fakeid"

    class _Coll:
        async def insert_one(self, doc):
            inserts.append(doc)
            return _Res()

    class _DB:
        def __getitem__(self, k):
            return _Coll()

    class _Client:
        def __getitem__(self, k):
            return _DB()

    monkeypatch.setattr(comp, "get_client", lambda: _Client())
    tid = asyncio.run(comp.store_comprehensive_trade(
        symbol="XAUUSD", asset="XAUUSD", quantity=0.01, price=4000.0,
        entry_price=4000.0, agent_id="a1", prompt_hash="h",
        signal_received_at=_utc(), trade_executed_at=_utc(), side="sell",
    ))
    assert tid == "fakeid"
    assert len(inserts) == 1


# ── logs -> report reconstruction ───────────────────────────────────────────

def test_logs_to_report(tmp_path):
    from scripts.logs_to_report import build_report

    def line(event, payload):
        return json.dumps({"ts": "t", "lvl": "INFO", "msg": f"event:{event}",
                           "ctx": {}, "event": event, "payload": payload})

    logf = tmp_path / "run.log"
    logf.write_text("\n".join([
        line("cycle", {"cycle_id": "c1"}),
        line("trade_exit", {"asset": "XAUUSD", "side": "sell", "entry_price": 4000,
                            "exit_price": 3990, "pnl": 10.0, "exit_reason": "Rule"}),
        line("trade_exit", {"asset": "XAUUSD", "side": "sell", "entry_price": 4000,
                            "exit_price": 4005, "pnl": -5.0, "exit_reason": "SL"}),
        line("trade_exit", {"asset": "USOIL", "side": "buy", "entry_price": 70,
                            "exit_price": 69, "pnl": -3.0, "exit_reason": "SL"}),
        "garbage-not-json",
    ]) + "\n")

    report = build_report([str(logf)], exclude_partials=False)
    assert "Closed legs | 3" in report
    assert "Net P/L | +2.00" in report           # 10 - 5 - 3
    assert "Win rate | 33.3% (1W / 2L)" in report
    assert "XAUUSD" in report and "USOIL" in report
    assert "| SL | 2 |" in report                # two SL legs grouped


def test_structured_file_handler(tmp_path):
    """stdlib `extra={...}` is captured durably as JSON (the durability gap)."""
    import logging
    from packages.core import logger as corelog

    path = tmp_path / "structured.jsonl"
    # Reset the idempotency guard and detach any existing structured handler so
    # we can point a fresh one at our temp path.
    corelog._structured_enabled = False
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, corelog._StructuredJsonHandler):
            root.removeHandler(h)
    try:
        corelog.enable_structured_file_logging(path)
        lg = logging.getLogger("test.structured.capture")
        lg.setLevel(logging.INFO)
        lg.propagate = True
        lg.info("trail moved", extra={"old_sl": 100.0, "new_sl": 99.0, "asset": "XAUUSD"})

        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert lines
        rec = json.loads(lines[-1])
        assert rec["msg"] == "trail moved"
        assert rec["ctx"]["old_sl"] == 100.0
        assert rec["ctx"]["asset"] == "XAUUSD"
        assert "args" not in rec["ctx"]  # standard LogRecord attrs are excluded
    finally:
        for h in list(root.handlers):
            if isinstance(h, corelog._StructuredJsonHandler):
                root.removeHandler(h)
        corelog._structured_enabled = False


def test_report_reads_jsonl_and_csv(tmp_path):
    from scripts.logs_to_report import collect_rows, _write_csv

    jf = tmp_path / "structured.jsonl"
    jf.write_text(json.dumps({
        "event": "trade_exit",
        "payload": {"asset": "XAUUSD", "side": "sell", "entry_price": 4000,
                    "exit_price": 3990, "pnl": 10.0, "exit_reason": "TP"},
    }) + "\n")

    rows = collect_rows([str(jf)], exclude_partials=False)
    assert len(rows) == 1 and rows[0]["pnl"] == 10.0

    csvp = tmp_path / "rows.csv"
    _write_csv(rows, str(csvp))
    txt = csvp.read_text(encoding="utf-8")
    assert "asset,side,entry_price,exit_price,pnl,reason" in txt
    assert "XAUUSD" in txt
