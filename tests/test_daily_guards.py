import pytest

from packages.core.risk import DailyGuards


@pytest.fixture(autouse=True)
def patch_sniper():
    """Override global autouse fixture that requires external dependencies."""
    pass



def test_normal_progression():
    guards = DailyGuards(max_loss=100, max_trades=5, cooldown=2, loss_threshold=2)

    assert guards.allow_trade(0)
    guards.register_trade(10)
    assert guards.cumulative_pnl == 10
    assert guards.trade_count == 1
    assert guards.consecutive_losses == 0

    assert guards.allow_trade(0)
    guards.register_trade(-5)
    assert guards.cumulative_pnl == 5
    assert guards.trade_count == 2
    assert guards.consecutive_losses == 1

    assert guards.allow_trade(0)
    guards.register_trade(0)
    assert guards.cumulative_pnl == 5
    assert guards.consecutive_losses == 0


def test_limit_breaches():
    guards = DailyGuards(max_loss=50, max_trades=10, cooldown=1, loss_threshold=2)
    guards.register_trade(-30)
    # prospective loss would breach max_loss
    assert not guards.allow_trade(-21)
    # a smaller loss would still be allowed
    assert guards.allow_trade(-10)

    guards.register_trade(-20)  # cumulative now -50
    assert not guards.allow_trade(10)  # already at limit

    trade_limited = DailyGuards(max_loss=100, max_trades=2, cooldown=1, loss_threshold=2)
    assert trade_limited.allow_trade(0)
    trade_limited.register_trade(10)
    assert trade_limited.allow_trade(0)
    trade_limited.register_trade(5)
    assert not trade_limited.allow_trade(0)


def test_cooldown_activation_and_reset():
    guards = DailyGuards(max_loss=100, max_trades=10, cooldown=2, loss_threshold=2)
    guards.register_trade(-10)
    guards.register_trade(-20)

    assert not guards.allow_trade(0)
    assert guards.cooldown_remaining == 1
    assert not guards.allow_trade(0)
    assert guards.cooldown_remaining == 0
    assert guards.allow_trade(0)

    guards.register_trade(5)
    assert guards.cumulative_pnl == -25
    assert guards.consecutive_losses == 0
