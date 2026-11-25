"""Daily risk management guards.

This module defines :class:`DailyGuards` which tracks cumulative profit and
loss (PnL), trade counts, and consecutive losing trades.  It can be used to
prevent further trades when daily limits are breached or after a cooldown
period triggered by consecutive losses.
"""

from dataclasses import dataclass


@dataclass
class DailyGuards:
    """Enforce daily trading limits.

    Parameters
    ----------
    max_loss: float
        Maximum allowable cumulative loss for the day. Trading stops once
        this loss is reached or exceeded.
    max_trades: int
        Maximum number of trades allowed per day.
    cooldown: int
        Number of trade attempts to block after ``loss_threshold`` consecutive
        losing trades.
    loss_threshold: int
        Number of consecutive losing trades required to trigger a cooldown.
    """

    max_loss: float
    max_trades: int
    cooldown: int
    loss_threshold: int
    cumulative_pnl: float = 0.0
    trade_count: int = 0
    consecutive_losses: int = 0
    cooldown_remaining: int = 0

    def allow_trade(self, pnl: float = 0.0) -> bool:
        """Return ``True`` if a new trade is allowed.

        ``pnl`` is the prospective PnL for the next trade. It is used to
        determine whether taking the trade would push cumulative losses beyond
        ``max_loss``.
        """

        if self.cooldown_remaining > 0:
            self.cooldown_remaining -= 1
            return False
        if self.trade_count >= self.max_trades:
            return False
        if (
            self.cumulative_pnl <= -self.max_loss
            or self.cumulative_pnl + pnl <= -self.max_loss
        ):
            return False
        return True

    def register_trade(self, pnl: float) -> None:
        """Update internal counters with the result of a completed trade."""

        self.cumulative_pnl += pnl
        self.trade_count += 1

        if pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.loss_threshold:
                self.cooldown_remaining = self.cooldown
                self.consecutive_losses = 0
        else:
            self.consecutive_losses = 0
