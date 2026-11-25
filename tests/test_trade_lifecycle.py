import pytest

from core.trade_lifecycle import (
    TradeLifecycle,
    InMemoryTradeRepository,
    TradeState,
    OptimisticLockError,
)


@pytest.mark.asyncio
async def test_full_lifecycle():
    repo = InMemoryTradeRepository()
    lifecycle = TradeLifecycle(repo)
    trade_id = "t1"

    trade = await lifecycle.open(trade_id)
    assert trade.state is TradeState.OPEN
    assert trade.version == 1

    trade = await lifecycle.move_to_break_even(trade_id)
    assert trade.state is TradeState.BREAK_EVEN
    assert trade.version == 2

    trade = await lifecycle.close(trade_id)
    assert trade.state is TradeState.CLOSED
    assert trade.version == 3


@pytest.mark.asyncio
async def test_version_conflict():
    repo = InMemoryTradeRepository()
    lifecycle = TradeLifecycle(repo)
    trade_id = "t2"

    await lifecycle.open(trade_id)
    stale = await repo.get(trade_id)

    await lifecycle.move_to_break_even(trade_id)

    stale.state = TradeState.CLOSED
    with pytest.raises(OptimisticLockError):
        await repo.save(stale, expected_version=stale.version)
