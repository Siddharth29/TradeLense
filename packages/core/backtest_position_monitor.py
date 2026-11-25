"""Advance one open position across closed bars, in live's exact order.

This is the backtest's counterpart to ``services.trade_monitor.TradeMonitor``'s
per-bar block, and it deliberately performs the same steps in the same sequence
so the two cannot drift:

    1. expiry            — live's ``while now < expiry`` guard, plus the
                           post-loop force close it falls through to
    2. pre-close guard   — ``_is_market_closing_soon``: flatten before the daily
                           maintenance break or the weekend gap
    3. green-TTL         — ``maybe_extend_expiry`` on EVERY fresh bar (live does
                           this per bar; the old backtest only did it once per
                           hourly checkpoint, so a position could time out on a
                           bar where live would have extended it)
    4. exit ladder       — ``PositionSimulator.step`` → ``evaluate_trade``:
                           TP1 half-book, BE+0.5R, the eight-step trail ladder,
                           touch arming, TP2 partial, ATR chandelier
    5. runner expiry     — TP2 booking promotes the expiry to RUNNER_MAX_HOURS

The monitor is handed a list of bars and never fetches its own, so it physically
cannot look past the clock its caller was given. It holds no database, broker or
model dependency, which makes the exit rules unit-testable in isolation — the
thing that matters most, since every dollar of the difference between a correct
thesis and a banked exit is decided here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, Optional

import pandas as pd

from .backtest_account import PositionSimulator
from .backtest_bar_feed import bar_close_time
from .backtest_types import RESULT_TIMEOUT
from .config import RUNNER_MAX_HOURS
from .logger import get_logger
from .trade_monitor import maybe_extend_expiry

logger = get_logger(__name__)


def position_signature(entry: float, stop: float, direction: str) -> tuple:
    """Exact level identity; two-decimal rounding merges distinct FX setups."""
    return (float(entry), float(stop), direction)


@dataclass
class OpenPosition:
    """A filled position and everything needed to carry it across bars.

    ``last_evaluated_bar_time`` is the resume point: the monitor continues from
    the bar AFTER it, so coverage from entry to exit is continuous and no bar is
    evaluated twice. On a fresh fill it is the trigger bar, because the position
    opened at that bar's close and live starts counting at the next one.
    """

    asset: str
    trade_state: Dict[str, Any]
    rules: Dict[str, Any]
    quantity: float
    prediction: Dict[str, Any]
    entry_time: datetime
    entry_iteration: int
    expiry: datetime
    last_evaluated_bar_time: datetime
    simulator: Optional[PositionSimulator] = field(default=None, repr=False)

    def simulator_for_replay(self) -> PositionSimulator:
        """The ladder's state, created once and kept with the position.

        Booked TP1/TP2 legs and the trail step must survive across catch-up
        windows: the monitor is re-entered every checkpoint and a fresh
        simulator would silently un-book realised partials and re-arm the trail.
        """
        if self.simulator is None:
            self.simulator = PositionSimulator(
                self.asset,
                self.trade_state,
                self.rules,
                self.quantity,
                # The first bar this simulator ever sees is already post-entry
                # (the caller resumes after the trigger bar), so no
                # skip_first_bar. Native SL is active from the first tick.
            )
        return self.simulator

    @property
    def signature(self) -> tuple:
        """Entry/stop/direction identity, used to refuse a duplicate re-entry."""
        return position_signature(
            self.trade_state.get("entry") or 0.0,
            self.trade_state.get("initial_stop") or self.trade_state.get("sl") or 0.0,
            self.trade_state.get("direction", ""),
        )


@dataclass
class PositionClosed:
    """The outcome the runner records when a position finishes on a bar.

    Carries the prediction and the iteration that opened the position so the
    exported row still attributes the close to the cycle that caused it, even
    though the close is discovered on a later checkpoint's catch-up.
    """

    outcome: Dict[str, Any]
    bar_time: datetime
    prediction: Dict[str, Any]
    entry_iteration: int

    @property
    def profit(self) -> float:
        return float(self.outcome.get("profit") or 0.0)


class PositionMonitor:
    """Steps an :class:`OpenPosition` over closed bars until it exits.

    ``market_closing_soon`` is injected rather than imported at module scope so
    the exit rules can be tested without pulling in the broker adapter, and so a
    caller can replay a position against a different session calendar without
    touching this class.
    """

    def __init__(self, market_closing_soon) -> None:
        self._market_closing_soon = market_closing_soon

    def advance(self, position: OpenPosition, bars: Iterable[Any]) -> Optional[PositionClosed]:
        """Run ``position`` over ``bars``. Returns the close, or None if still open.

        ``bars`` must already be bounded by the caller's clock — this class never
        reaches for a bar of its own.
        """
        for bar in bars:
            closed = self.step(position, bar)
            if closed is not None:
                return closed
        return None

    def step(self, position: OpenPosition, bar: Dict[str, Any]) -> Optional[PositionClosed]:
        """Advance ``position`` by exactly one closed bar, in live's order.

        The single-bar primitive, so a caller that interleaves position
        monitoring with strategy arming does not have to wrap each bar in a list
        to reuse the ladder's state.
        """
        simulator = position.simulator_for_replay()
        bar_time = _as_utc(bar_close_time(bar))
        close = float(bar["close"])

        # Orders already at the broker act during the bar, before a timeout
        # or a discretionary close at its end. Never overwrite such a fill
        # with the later, potentially favorable candle close.
        simulator.check_native_exit(
            close, float(bar["high"]), float(bar["low"]), bar_time,
            open_price=float(bar["open"]),
        )
        if simulator.result.closed:
            return self._closed(
                position, simulator, simulator.result.result,
                simulator.result.exit_price, bar_time,
            )

        # 1. Expiry. Live's monitor runs `while now < expiry` and force closes
        #    whatever is left when it falls out of that loop.
        if bar_time >= position.expiry:
            simulator.force_close(close, bar_time, reason=RESULT_TIMEOUT)
            held_minutes = (bar_time - position.entry_time).total_seconds() / 60
            logger.info(
                f"[POSITION] {position.asset} monitor expiry at {bar_time.isoformat()} "
                f"after {held_minutes:.0f}min: pnl={simulator.result.pnl:.4f} "
                f"legs={len(simulator.result.legs)}"
            )
            return self._closed(position, simulator, RESULT_TIMEOUT, close, bar_time,
                                timeout_minutes=held_minutes)

        # 2. Pre-close guard: flatten before a maintenance or weekend gap.
        closing, close_reason = self._market_closing_soon(position.asset, now=bar_time)
        if closing:
            simulator.force_close(close, bar_time, reason=RESULT_TIMEOUT)
            logger.info(
                f"[POSITION] {position.asset} market closing ({close_reason}) at "
                f"{bar_time.isoformat()} — flattening to avoid gap risk "
                f"(pnl={simulator.result.pnl:.4f})"
            )
            return self._closed(position, simulator, RESULT_TIMEOUT, close, bar_time,
                                exit_reason=f"PRE_CLOSE_{close_reason.upper()}")

        # 3. Green-TTL extension, per bar, exactly where live evaluates it.
        extended = maybe_extend_expiry(
            position.trade_state, close, position.expiry,
            start_time=position.entry_time, now=bar_time,
        )
        if position.rules.get("hard_max_age_minutes"):
            extended = min(extended, position.entry_time+timedelta(minutes=position.rules["hard_max_age_minutes"]))
        if extended != position.expiry:
            logger.info(
                f"[POSITION] {position.asset} TTL extended (green and risk-free): "
                f"{position.expiry.isoformat()} -> {extended.isoformat()}"
            )
            position.expiry = extended

        # 4. The exit ladder — the same evaluate_trade live runs.
        tp2_was_booked = simulator.result.tp2_hit
        simulator.step(close, float(bar["high"]), float(bar["low"]), bar_time,
                       open_price=float(bar["open"]))
        position.last_evaluated_bar_time = bar_time

        # 5. A booked TP2 buys the runner RUNNER_MAX_HOURS instead of the
        #    ordinary monitor expiry, so the tail the runner exists to capture
        #    is actually observable.
        if simulator.result.tp2_hit and not tp2_was_booked:
            runner_expiry = position.entry_time + timedelta(hours=RUNNER_MAX_HOURS)
            if position.rules.get("hard_max_age_minutes"):
                runner_expiry = min(runner_expiry, position.entry_time+timedelta(minutes=position.rules["hard_max_age_minutes"]))
            if runner_expiry > position.expiry:
                logger.info(
                    f"[POSITION] {position.asset} TP2 booked — runner expiry "
                    f"{position.expiry.isoformat()} -> {runner_expiry.isoformat()}"
                )
                position.expiry = runner_expiry

        if simulator.result.closed:
            logger.info(
                f"[POSITION] {position.asset} closed at {bar_time.isoformat()}: "
                f"result={simulator.result.result} exit={simulator.result.exit_price} "
                f"pnl={simulator.result.pnl:.4f} "
                f"legs={[(leg.reason, round(leg.pnl, 4)) for leg in simulator.result.legs]}"
            )
            return self._closed(
                position, simulator, simulator.result.result,
                simulator.result.exit_price,
                _as_utc(simulator.result.exit_time) or bar_time,
            )

        return None

    # ── outcome shaping ─────────────────────────────────────────────────
    def _closed(
        self,
        position: OpenPosition,
        simulator: PositionSimulator,
        result: str,
        exit_price: float,
        exit_time: datetime,
        *,
        exit_reason: Optional[str] = None,
        timeout_minutes: Optional[float] = None,
    ) -> PositionClosed:
        position.last_evaluated_bar_time = exit_time
        outcome: Dict[str, Any] = {
            "direction": position.trade_state["direction"],
            "entry": position.trade_state["entry"],
            "stop": position.trade_state["sl"],
            "initial_stop": position.trade_state.get("initial_stop"),
            "target": position.rules.get("tp"),
            "take_profit_1": position.rules.get("tp1"),
            "take_profit_2": position.rules.get("tp"),
            "entry_time": position.entry_time,
            "exit_time": exit_time,
            "exit_price": exit_price,
            "profit": simulator.result.pnl,
            "result": result,
            **_cost_evidence(position, simulator, closed=True),
        }
        if exit_reason:
            outcome["exit_reason"] = exit_reason
        if timeout_minutes is not None:
            outcome["timeout_minutes"] = timeout_minutes
        from packages.db.persistence_gate import log_event
        log_event("backtest_trade_exit", outcome, asset=position.asset,
                  cycle_id=(position.prediction.get("ai_cost") or {}).get("cycle_id"))
        return PositionClosed(
            outcome=outcome,
            bar_time=exit_time,
            prediction=position.prediction,
            entry_iteration=position.entry_iteration,
        )


def _cost_evidence(position, simulator, *, closed):
    commission = -sum(leg.pnl for leg in simulator.result.legs if leg.reason == "COMMISSION")
    return {
        "trade_reason": position.prediction.get("trade_reason"),
        "trigger_reason": position.prediction.get("trigger_reason"),
        "ai_cost": position.prediction.get("ai_cost"),
        "broker_cost": {"total": commission, "commission": commission, "currency": "USD",
                        "status": "simulated", "source": "simulation_config", "closed": closed},
    }


def unrealised_snapshot(position: OpenPosition, mark_price: Optional[float]) -> Dict[str, Any]:
    """The still-open row the runner records for a checkpoint that closed nothing.

    ``profit`` stays 0 on an open row — P&L is booked once, at close, where the
    simulator's total covers every leg. Realised partials are surfaced separately
    so a booked TP1 is visible while the runner is still alive.
    """
    simulator = position.simulator_for_replay()
    return {
        "direction": position.trade_state["direction"],
        "entry": position.trade_state["entry"],
        "stop": position.trade_state["sl"],
        "initial_stop": position.trade_state.get("initial_stop"),
        "target": position.rules.get("tp"),
        "take_profit_1": position.rules.get("tp1"),
        "take_profit_2": position.rules.get("tp"),
        "exit_time": None,
        "exit_price": mark_price if mark_price is not None else position.trade_state["entry"],
        "profit": 0.0,
        "realized_pnl": simulator.result.pnl,
        **_cost_evidence(position, simulator, closed=False),
        "result": "Open",
    }


def _as_utc(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    return stamp.to_pydatetime()
