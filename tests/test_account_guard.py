"""Account daily-DD guard: baseline isolation and persistent loss memory.

Regression for the 2026-06-21 bug where a paper-broker $10M day-start baseline
was compared against a live ~$738 equity, yielding a bogus 99.99% drawdown that
blocked every entry for the whole UTC day.
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, Mock

import packages.core.risk.account_guard as ag


# ── In-memory async Mongo stand-ins ──────────────────────────────────────────
class FakeCol:
    def __init__(self):
        self.docs = []

    def _match(self, flt):
        for d in self.docs:
            if all(d.get(k) == v for k, v in flt.items()):
                return d
        return None

    async def find_one(self, flt):
        d = self._match(flt)
        return dict(d) if d else None

    async def update_one(self, flt, update, upsert=False):
        d = self._match(flt)
        if d is None and upsert:
            nd = dict(flt)
            nd.update(update.get("$setOnInsert", {}))
            nd.update(update.get("$set", {}))
            self.docs.append(nd)
        elif d is not None:
            d.update(update.get("$set", {}))  # $setOnInsert is a no-op on existing


class _FakeDB:
    def __init__(self, col): self.col = col
    def __getitem__(self, _): return self.col


class FakeClient:
    def __init__(self, col): self.col = col
    def __getitem__(self, _): return _FakeDB(self.col)


# ── Broker stubs ─────────────────────────────────────────────────────────────
class PaperBrokerStub:
    _credentials = None
    def balances(self): return {"cash": 10_000_000.0}  # fake paper cash, no equity


class MT5Stub:
    def __init__(self, equity):
        self._credentials = {"server": "Example-Demo", "login": "12345678"}
        self._eq = equity
    def balances(self): return {"cash": self._eq, "equity": self._eq}


@pytest.fixture
def col(monkeypatch):
    c = FakeCol()
    monkeypatch.setattr(ag, "get_client", lambda: FakeClient(c))
    return c


def test_paper_seed_does_not_block_live(col):
    """Paper $10M seeds its own baseline; the live account keys separately and
    is NOT halted by the paper number."""
    # Paper cycle seeds first (the original bug source)
    halted, _ = asyncio.run(ag.account_trading_halted(PaperBrokerStub()))
    assert halted is False
    # Live account, fresh — its own baseline = its own equity, DD = 0
    halted, reason = asyncio.run(ag.account_trading_halted(MT5Stub(738.34)))
    assert halted is False, reason
    # Two distinct baselines persisted (paper bucket vs live login)
    accounts = {d["account"] for d in col.docs}
    assert any(a.startswith("mt5:") for a in accounts)
    assert any(not a.startswith("mt5:") for a in accounts)


def test_large_baseline_discrepancy_requires_explicit_reconciliation(col):
    """A large loss cannot establish whether the same-account baseline is bad."""
    live = MT5Stub(738.34)
    bal = live.balances()
    account = ag._account_fingerprint(live, bal)
    col.docs.append({"date": ag._today_key(), "account": account, "start_equity": 10_000_000.0})

    halted, reason = asyncio.run(ag.account_trading_halted(live))
    assert halted is True, reason
    doc = next(d for d in col.docs if d["account"] == account)
    assert doc["start_equity"] == 10_000_000.0
    assert "reseeded_at" not in doc


@pytest.mark.parametrize("equity", [400.0, 0.0, -20.0, float("nan")])
def test_catastrophic_loss_cannot_reset_or_bypass_guard(col, equity):
    account = "mt5:Example-Demo:12345678"
    col.docs.append({"date": ag._today_key(), "account": account, "start_equity": 1000.0})
    live = MT5Stub(equity)
    live.balances = lambda: {"equity": equity, "cash": 1000.0}
    halted, _ = asyncio.run(ag.account_trading_halted(live))
    assert halted is True
    assert col.docs[0]["start_equity"] == 1000.0


def test_real_drawdown_still_halts(col):
    """Genuine DD beyond the limit still halts (guard not neutered)."""
    account = "mt5:Example-Demo:12345678"
    col.docs.append({"date": ag._today_key(), "account": account, "start_equity": 1000.0})
    halted, reason = asyncio.run(ag.account_trading_halted(MT5Stub(850.0)))  # -15%
    assert halted is True
    assert "15.00%" in reason


def test_normal_small_drawdown_allows(col):
    account = "mt5:Example-Demo:12345678"
    col.docs.append({"date": ag._today_key(), "account": account, "start_equity": 1000.0})
    halted, _ = asyncio.run(ag.account_trading_halted(MT5Stub(950.0)))  # -5%
    assert halted is False


def test_paper_identity_does_not_reset_at_equity_digit_boundary(col):
    paper = PaperBrokerStub()
    paper.balances = lambda: {"cash": 1100.}
    assert asyncio.run(ag.account_trading_halted(paper))[0] is False
    paper.balances = lambda: {"cash": 900.}
    assert asyncio.run(ag.account_trading_halted(paper))[0] is True
    assert len(col.docs) == 1
    assert col.docs[0]["start_equity"] == 1100.


@pytest.mark.parametrize("balance", [None, {}, {"equity": "bad"}])
def test_unreadable_balance_defers_entry(col, balance):
    live = MT5Stub(1000.)
    live.balances = lambda: balance
    assert asyncio.run(ag.account_trading_halted(live))[0] is True


@pytest.mark.parametrize("baseline", [None, 0., -1., float("nan"), float("inf"), True])
def test_invalid_persisted_baseline_is_not_replaced(col, baseline):
    col.docs.append({"date": ag._today_key(), "account": "mt5:Example-Demo:12345678", "start_equity": baseline})
    assert asyncio.run(ag.account_trading_halted(MT5Stub(1000.)))[0] is True
    assert len(col.docs) == 1
    assert "reseeded_at" not in col.docs[0]


def test_db_failure_blocks_new_entries_until_successful_read(col, monkeypatch):
    original = col.find_one
    monkeypatch.setattr(col, "find_one", AsyncMock(side_effect=RuntimeError("DB down")))
    assert asyncio.run(ag.account_trading_halted(MT5Stub(1000.)))[0] is True
    monkeypatch.setattr(col, "find_one", original)
    assert asyncio.run(ag.account_trading_halted(MT5Stub(1000.)))[0] is False


def test_disabled_guard_does_not_read_broker(monkeypatch):
    monkeypatch.setattr(ag, "ACCOUNT_DAILY_DD_GUARD_ENABLED", False)
    live = MT5Stub(1000.)
    live.balances = Mock(side_effect=AssertionError("disabled guard read broker"))
    assert asyncio.run(ag.account_trading_halted(live)) == (False, "")


@pytest.mark.asyncio
async def test_concurrent_first_seeds_share_persisted_baseline(col, monkeypatch):
    original = col.find_one
    async def concurrent_read(flt):
        result = await original(flt)
        await asyncio.sleep(0)
        return result
    monkeypatch.setattr(col, "find_one", concurrent_read)
    starts = await asyncio.gather(ag._seed_or_get_start_equity(1000., "one-account"),
                                  ag._seed_or_get_start_equity(900., "one-account"))
    assert starts == [1000., 1000.]
    assert len(col.docs) == 1
    assert col.docs[0]["_id"].startswith("daily-dd:")
