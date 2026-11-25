"""Tests for the exit-match retry in update_comprehensive_trade_exit (2026-06-20).

The open-trade doc is persisted fire-and-forget at entry, so a very fast exit
can arrive before that write lands. When a position_ticket was supplied and no
doc matches on the first pass, the updater retries once — closing the race that
otherwise left a phantom permanently-open doc.
"""

from __future__ import annotations

import pytest

from packages.db.repositories import comprehensive_logging as cl


class _FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, field, direction):
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    async def to_list(self, length=None):
        return list(self._docs)


class _FakeColl:
    """Reveals the open doc only after ``reveal_after`` find calls — simulating
    the in-flight entry write landing during the retry delay."""

    def __init__(self, doc, reveal_after):
        self.doc = doc
        self.reveal_after = reveal_after
        self.find_calls = 0
        self.updated = []

    def find(self, filt, projection=None):
        self.find_calls += 1
        if self.find_calls <= self.reveal_after:
            return _FakeCursor([])
        if self.doc.get("trade_status") != "open":
            return _FakeCursor([])
        if filt.get("position_ticket") == self.doc.get("position_ticket"):
            return _FakeCursor([self.doc])
        if filt.get("symbol") == self.doc.get("symbol"):
            return _FakeCursor([self.doc])
        return _FakeCursor([])

    async def update_one(self, filt, update):
        self.updated.append((filt, update))
        return None


def _install(monkeypatch, coll):
    client = {cl.MONGO_DB_NAME: {cl._COLLECTION_COMPREHENSIVE_TRADES: coll}}
    monkeypatch.setattr(cl, "get_client", lambda: client)
    monkeypatch.setattr(cl, "_EXIT_MATCH_RETRY_SECONDS", 0.0)  # no real delay in tests


@pytest.mark.asyncio
async def test_retry_finds_doc_written_after_exit(monkeypatch):
    doc = {"_id": "abc", "symbol": "BTCUSDm", "position_ticket": "T1",
           "entry_price": 100.0, "trade_status": "open"}
    coll = _FakeColl(doc, reveal_after=2)  # both attempt-0 finds miss; retry hits
    _install(monkeypatch, coll)

    ok = await cl.update_comprehensive_trade_exit(
        symbol="BTCUSDm", exit_price=101.0, exit_reason="TP2", pnl=1.0,
        position_ticket="T1",
    )
    assert ok is True
    assert len(coll.updated) == 1
    assert coll.updated[0][1]["$set"]["trade_status"] == "closed"
    assert coll.find_calls >= 3  # proved a retry happened


@pytest.mark.asyncio
async def test_immediate_match_no_retry(monkeypatch):
    doc = {"_id": "abc", "symbol": "BTCUSDm", "position_ticket": "T1",
           "entry_price": 100.0, "trade_status": "open"}
    coll = _FakeColl(doc, reveal_after=0)  # visible on first find
    _install(monkeypatch, coll)

    ok = await cl.update_comprehensive_trade_exit(
        symbol="BTCUSDm", exit_price=101.0, exit_reason="SL", pnl=-1.0,
        position_ticket="T1",
    )
    assert ok is True
    assert coll.find_calls == 1  # matched on the very first pass


@pytest.mark.asyncio
async def test_no_ticket_does_not_retry(monkeypatch):
    doc = {"_id": "abc", "symbol": "BTCUSDm", "entry_price": 100.0, "trade_status": "open"}
    coll = _FakeColl(doc, reveal_after=99)  # never revealed
    _install(monkeypatch, coll)

    ok = await cl.update_comprehensive_trade_exit(
        symbol="BTCUSDm", exit_price=101.0, exit_reason="SL", pnl=-1.0,
        position_ticket=None,
    )
    assert ok is False
    assert coll.updated == []
    assert coll.find_calls == 1  # single symbol-filter pass, no retry
