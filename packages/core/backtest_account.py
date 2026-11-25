"""Account model for the backtest: contract-correct P&L, live sizing, live legs.

Why this module exists
----------------------
Until 2026-08-07 the backtest had no account and no position lifecycle. It

  * multiplied a price move by a fixed ``lot_size`` (default 1.0 standard lot),
    so stop distance, balance and the broker's ``min_lot`` had no effect on
    position size — the exact opposite of live, where every trade is sized by
    ``balance × CS_DEFAULT_RISK_PCT`` and then FLOORED UP to ``min_lot``;
  * converted price to money with ``(diff / pip_size) × pip_value_per_lot``,
    which is right for FX and BTC but reports XAUUSD **100× too small** and
    XAGUSD **5000× too small** (their ``pip_value_per_lot`` is 1.0 while their
    contract sizes are 100 and 5000). Live fixed this in
    ``services/trade_monitor._usd_pnl``; the backtest never got the fix;
  * closed the whole position at TP, while live books 50% at TP1,
    ``TP2_PARTIAL_FRACTION`` of the REMAINDER at TP2, and rides the rest on an
    ATR chandelier.

Each of those makes backtest P&L a different quantity from live P&L, so this
module implements the live definitions once and both simulate paths use it.

Nothing here reads a broker or the clock — it is pure arithmetic over candles,
so it is unit-testable and replay-deterministic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .asset_registry import profile_for
from .backtest_types import (
    RESULT_BE,
    RESULT_OPEN,
    RESULT_SL,
    RESULT_TIMEOUT,
    RESULT_TP,
    RESULT_TRAIL,
)
from .config import (
    COMMISSION_USD_PER_0_01_LOT,
    COMMISSION_USD_PER_0_01_LOT_DEFAULT,
    CS_DEFAULT_RISK_PCT,
    RISK_BUDGET_MAX_OVERSHOOT_MULT,
    RISK_BUDGET_SKIP_ENABLED,
    RISK_BUDGET_WARN_OVERSHOOT_MULT,
    RUNNER_TRAIL_ATR_MULT,
    RUNNER_TRAIL_FALLBACK_RISK_MULT,
    TP2_PARTIAL_FRACTION,
    TP2_RUNNER_ENABLED,
)
from .logger import get_logger
from .trade_monitor import (
    EXIT_SL,
    EXIT_TP,
    MOVE_SL_TO_BE,
    PARTIAL_TP2,
    TRAIL_SL,
    evaluate_trade,
)

logger = get_logger(__name__)

# Fraction of the ORIGINAL position booked when TP1 is reached. Live hardcodes
# 0.5 at the ``partial_close`` call in services/trade_monitor.py; named here so
# the backtest cannot drift from it silently.
TP1_PARTIAL_FRACTION = 0.5


def _contract_specs_for(asset: Optional[str]):
    """``ContractSpecs`` for an asset (lot_step / min_lot / contract_size), or None."""
    from .risk import CONTRACT_SPECS

    profile = profile_for(asset)
    key = profile.symbol if profile else None
    return CONTRACT_SPECS.get(key) if key else None


def contract_size_for(asset: Optional[str]) -> float:
    """Contract size (units per lot) for an asset; 1.0 when unknown.

    Byte-identical resolution to ``services/trade_monitor._usd_pnl`` — same
    ``profile_for`` lookup, same 1.0 fallback — so a P&L computed here and one
    computed live agree by construction rather than by coincidence.
    """
    profile = profile_for(asset)
    return float(getattr(profile, "contract_size", 1.0) or 1.0) if profile else 1.0


def commission_for(asset: Optional[str], qty: float) -> float:
    """Entry commission for a position of size ``qty`` lots — a positive cost.

    Measured per-asset from real MT5 deal history (see
    ``config.COMMISSION_USD_PER_0_01_LOT``): charged once, on entry, scaling
    linearly with lot size. Never charged again on TP1/TP2/SL/TTL legs — every
    one of 405 measured OUT deals carried zero commission, so the ladder's
    partial closes are free once the position is open, exactly as live is.

    An asset that cannot be resolved to a known profile (``None``, or a
    legacy/abstract-unit call with no real instrument behind it — e.g. the
    price-R studies in trade_sim.py's older call sites) is charged nothing:
    guessing a real broker fee for an unspecified instrument would corrupt
    those asset-agnostic tests rather than model anything real.
    """
    profile = profile_for(asset)
    if profile is None:
        return 0.0
    rate = COMMISSION_USD_PER_0_01_LOT.get(profile.symbol, COMMISSION_USD_PER_0_01_LOT_DEFAULT)
    return rate * (float(qty) / 0.01)


def usd_pnl(asset: Optional[str], direction: str, entry: float, exit_price: float,
            qty: float) -> float:
    """Account-currency P&L for a price move, including the contract size.

    The live formula (``services/trade_monitor._usd_pnl``). The backtest's old
    pip formula agreed with it for FX/BTC/USTEC and disagreed by the contract
    size for every metal — see the module docstring.
    """
    sign = 1.0 if direction == "long" else -1.0
    return sign * (float(exit_price) - float(entry)) * float(qty) * contract_size_for(asset)


@dataclass
class SizingResult:
    """Outcome of sizing one trade against the account."""

    lot: float
    intended_risk: float = 0.0
    implied_risk: float = 0.0
    overshoot: float = 0.0
    skipped: bool = False
    reason: str = ""

    @property
    def tradeable(self) -> bool:
        return self.lot > 0 and not self.skipped


def size_position(
    asset: Optional[str],
    balance: float,
    entry: float,
    stop_loss: float,
    risk_pct: float = CS_DEFAULT_RISK_PCT,
) -> SizingResult:
    """Size a trade exactly as ``CodeSniperExecutor._calculate_position_size`` does.

    Same call into ``calc_lot_size_by_risk`` (floor to lot_step, clamp to
    min/max lot), then the same LP-01 risk-budget gate: when the floored lot's
    implied risk exceeds the intended budget by more than
    ``RISK_BUDGET_MAX_OVERSHOOT_MULT`` the trade is SKIPPED rather than taken at
    an unrequested size.

    Deliberately NOT modelled here: the live margin check, which needs broker
    leverage and free margin. On a cash-settled CFD account at these sizes it
    only ever binds far below the risk gate, and inventing a leverage number
    would be a fiction the live path does not have.
    """
    from .risk import CONTRACT_SPECS, calc_lot_size_by_risk

    sl_points = abs(float(entry) - float(stop_loss))
    if sl_points <= 0:
        return SizingResult(lot=0.0, skipped=True, reason="non_positive_stop_distance")
    if balance <= 0:
        return SizingResult(lot=0.0, skipped=True, reason="no_available_funds")

    profile = profile_for(asset)
    specs_key = profile.symbol if profile else None
    if not specs_key or specs_key not in CONTRACT_SPECS:
        # Live raises here rather than guessing a formula; the backtest records
        # it as a skip so the run continues and the gap is visible in the log.
        logger.warning(f"size_position: no contract specs for {asset} — trade skipped")
        return SizingResult(lot=0.0, skipped=True, reason="no_contract_specs")

    specs = CONTRACT_SPECS[specs_key]
    lot = calc_lot_size_by_risk(
        balance=balance,
        risk_pct=risk_pct,
        sl_points=sl_points,
        symbol=specs_key,
        specs=CONTRACT_SPECS,
        entry_price=entry,
    )

    intended_risk = balance * risk_pct
    implied_risk = lot * specs.contract_size * sl_points
    overshoot = (implied_risk / intended_risk) if intended_risk > 0 else 0.0
    result = SizingResult(
        lot=lot,
        intended_risk=intended_risk,
        implied_risk=implied_risk,
        overshoot=overshoot,
    )

    if intended_risk > 0 and overshoot > RISK_BUDGET_WARN_OVERSHOOT_MULT:
        logger.warning(
            "Min-lot RISK OVERSHOOT — trade risks more than risk_pct: "
            f"{asset} lot={lot} intended=${intended_risk:.2f} "
            f"actual=${implied_risk:.2f} ({overshoot:.2f}× = "
            f"{implied_risk / balance * 100:.2f}% of account)"
        )
        if RISK_BUDGET_SKIP_ENABLED and overshoot > RISK_BUDGET_MAX_OVERSHOOT_MULT:
            min_balance = implied_risk / (risk_pct * RISK_BUDGET_MAX_OVERSHOOT_MULT)
            logger.warning(
                "RISK BUDGET — skipping trade: min-lot risk exceeds budget: "
                f"{asset} {overshoot:.2f}× > {RISK_BUDGET_MAX_OVERSHOOT_MULT}× "
                f"(${implied_risk:.2f} vs ${intended_risk:.2f} budget). "
                f"Tradeable at this stop from a balance of ≈${min_balance:.0f}."
            )
            result.lot = 0.0
            result.skipped = True
            result.reason = "risk_budget_overshoot"
    return result


def runner_trail_distance(entry: float, stop_loss: float, atr_1h: Optional[float]) -> Optional[float]:
    """Chandelier distance frozen at entry — the live formula.

    ``RUNNER_TRAIL_ATR_MULT × ATR(1h)`` when the cycle captured an ATR, else
    ``RUNNER_TRAIL_FALLBACK_RISK_MULT × |entry − SL|`` (services/code_sniper_monitor.py).
    """
    risk_dist = abs(float(entry) - float(stop_loss)) if stop_loss else 0.0
    try:
        atr = float(atr_1h) if atr_1h else 0.0
    except (TypeError, ValueError):
        atr = 0.0
    dist = (RUNNER_TRAIL_ATR_MULT * atr) if atr > 0 else (RUNNER_TRAIL_FALLBACK_RISK_MULT * risk_dist)
    return dist or None


@dataclass
class Leg:
    """One booked portion of a position."""

    reason: str          # TP1_PARTIAL | TP2_PARTIAL | SL | BE | TP | TIMEOUT | TRAIL | COMMISSION
    qty: float
    price: float
    pnl: float
    time: Any = None


@dataclass
class PositionResult:
    """Outcome of running a sized position through a candle series."""

    legs: List[Leg] = field(default_factory=list)
    closed: bool = False
    result: str = RESULT_OPEN
    exit_price: Optional[float] = None
    exit_time: Any = None
    tp1_hit: bool = False
    tp2_hit: bool = False
    rejected_stop_updates: int = 0

    @property
    def pnl(self) -> float:
        return sum(leg.pnl for leg in self.legs)

    @property
    def tp1_pnl(self) -> float:
        return sum(leg.pnl for leg in self.legs if leg.reason == "TP1_PARTIAL")


def build_rules(
    tp1: Optional[float],
    tp2: Optional[float],
    *,
    entry: Optional[float] = None,
    stop_loss: Optional[float] = None,
    atr_1h: Optional[float] = None,
    runner_enabled: Optional[bool] = None,
) -> Dict[str, Any]:
    """The ``rules`` dict the live monitor is queued with.

    Live queues ``{tp1: decision.tp1, tp: decision.tp, move_sl_to_be: True,
    runner_enabled: TP2_RUNNER_ENABLED, runner_trail_dist: ...}``. The backtest
    used to invent ``tp1`` as the midpoint of entry→target and drop TP2
    entirely, which made every backtest "TP" a different event from a live one.
    """
    on = TP2_RUNNER_ENABLED if runner_enabled is None else bool(runner_enabled)
    rules: Dict[str, Any] = {
        "tp1": tp1,
        "tp": tp2,
        "move_sl_to_be": True,
        "runner_enabled": on,
    }
    if on and entry is not None and stop_loss is not None:
        rules["runner_trail_dist"] = runner_trail_distance(entry, stop_loss, atr_1h)
    return rules


class PositionSimulator:
    """Replays a sized position over 5m candles with the LIVE exit ladder.

    Ladder (services/trade_monitor.py, config.TP2_*/RUNNER_*):

      TP1  → book ``TP1_PARTIAL_FRACTION`` of the original size, move SL to
             BE+0.5R, keep the remainder.
      TP2  → book ``TP2_PARTIAL_FRACTION`` of what REMAINS, keep the rest as a
             runner trailed by an ATR chandelier (``runner_trail_dist``).
      SL   → close whatever is still open at the (possibly trailed) stop.

    The decision of WHICH action fires on a bar is delegated to the shared
    ``trade_monitor.evaluate_trade``, so touch-arming, the trail ladder and the
    native-SL precedence are literally the same code live runs — this class
    only owns the money and the volume book-keeping.
    """

    def __init__(
        self,
        asset: Optional[str],
        trade_state: Dict[str, Any],
        rules: Dict[str, Any],
        quantity: float,
        *,
        settlement_candles: int = 0,
        skip_first_bar: bool = False,
    ):
        self.asset = asset
        self.trade_state = trade_state
        self.rules = rules
        self.initial_qty = float(quantity)
        self.open_qty = float(quantity)
        # Retained as a constructor compatibility argument only. A native MT5
        # SL is attached on entry, so it cannot be suppressed for post-entry
        # "settlement" bars. Only skip_first_bar excludes pre-entry price action.
        self.settlement_candles = 0
        # When the first bar fed in IS the bar the position entered on, it must
        # not be evaluated at all. Live's monitor starts counting at the first
        # bar AFTER the trade is queued, so the entry bar's price action never
        # reaches evaluate_trade. The backtest feeds the trigger bar as bar 0
        # (the SL-inside-trigger guard needs it), and settlement only suppresses
        # the STOP — so TP/TP1 could fire on a high the bar printed BEFORE the
        # entry close, booking a partial the position was never open for.
        self.skip_first_bar = bool(skip_first_bar)
        self.result = PositionResult()
        self._bars_seen = 0
        # Current bar's extremes, kept so the TP2 handler can seed the
        # chandelier from the same value live does (the bar's high/low).
        self._bar_high: float = 0.0
        self._bar_low: float = 0.0
        trade_state.setdefault("quantity", self.open_qty)
        # Entry commission, charged once — see commission_for(). qty=0 so it
        # never touches open_qty bookkeeping; it is a cost leg, not a close.
        commission = commission_for(asset, self.initial_qty)
        if commission > 0:
            self.result.legs.append(
                Leg(reason="COMMISSION", qty=0.0,
                    price=float(trade_state.get("entry") or 0.0),
                    pnl=-commission, time=None)
            )

    # -- helpers ---------------------------------------------------------
    def _book(self, reason: str, qty: float, price: float, when: Any) -> float:
        """Record a partial/full close and return its P&L."""
        qty = min(qty, self.open_qty)
        if qty <= 0:
            return 0.0
        pnl = usd_pnl(self.asset, self.trade_state.get("direction"),
                      self.trade_state.get("entry"), price, qty)
        self.result.legs.append(Leg(reason=reason, qty=qty, price=price, pnl=pnl, time=when))
        self.open_qty -= qty
        self.trade_state["quantity"] = self.open_qty
        return pnl

    def _close_all(self, reason: str, price: float, when: Any, result_label: str) -> None:
        self._book(reason, self.open_qty, price, when)
        self.result.closed = True
        self.result.result = result_label
        self.result.exit_price = price
        self.result.exit_time = when

    # -- main loop -------------------------------------------------------
    def check_native_exit(
        self, close: float, high: float, low: float, when: Any = None,
        *, open_price: Optional[float] = None,
    ) -> Optional[str]:
        """Resolve orders already active during this post-entry bar.

        Call before a bar-close timeout/pre-close action. A native stop can
        already have closed the position by then. With the runner enabled no
        TP is on the broker: TP2 must wait for the synthetic close-bar action.
        An adverse gap fills at the open, not the stop that was jumped over.
        For two native orders touched without an observable sequence, SL-first
        is a conservative OHLC assumption. An opening gap resolves first.
        """
        if self.result.closed:
            return None
        is_long = self.trade_state.get("direction") == "long"
        sl = self.trade_state.get("sl")
        tp = self.rules.get("tp") if not self.rules.get("runner_enabled", False) else None
        if open_price is not None:
            if sl is not None and (open_price <= sl if is_long else open_price >= sl):
                self._on_sl(close, high, low, when, open_price=open_price)
                return EXIT_SL
            if tp is not None and (open_price >= tp if is_long else open_price <= tp):
                self._on_tp2(EXIT_TP, close, when)
                return EXIT_TP
        if sl is not None and (low <= sl if is_long else high >= sl):
            self._on_sl(close, high, low, when, open_price=open_price)
            return EXIT_SL
        if tp is not None and (high >= tp if is_long else low <= tp):
            self._on_tp2(EXIT_TP, close, when)
            return EXIT_TP
        return None

    def step(
        self, close: float, high: float, low: float, when: Any = None,
        *, open_price: Optional[float] = None,
    ) -> Optional[str]:
        """Advance one candle. Returns the action taken, or None.

        Stops being a no-op once ``result.closed`` is set, so a caller can keep
        feeding bars without special-casing the exit.
        """
        if self.result.closed:
            return None

        self._bars_seen += 1
        self._bar_high, self._bar_low = high, low

        if self.skip_first_bar and self._bars_seen == 1:
            # The entry bar itself: not evaluated, exactly as live (see the
            # skip_first_bar note in __init__).
            return None

        native_action = self.check_native_exit(
            close, high, low, when, open_price=open_price,
        )
        if native_action is not None:
            return native_action

        action = evaluate_trade(
            self.trade_state, close, high, low, self.rules,
            is_entry_candle=False,
        )

        if action == TRAIL_SL:
            self._apply_trail(close)
            return action

        if action == MOVE_SL_TO_BE:
            self._on_tp1(close, when)
            return action

        if action in (PARTIAL_TP2, EXIT_TP):
            self._on_tp2(action, close, when)
            return action

        if action == EXIT_SL:
            self._on_sl(close, high, low, when, open_price=open_price)
            return action

        return None

    def step_quote(self, price: float, when: Any) -> Optional[str]:
        """Replay an actual executable quote; does not advance closed-bar counters.

        Callers must provide bid for a long exit / ask for a short exit. A sampled
        quote replay is still not evidence of fills between observations.
        """
        from .exit_policy import quote_action
        if not math.isfinite(price) or price <= 0 or when is None:
            raise ValueError("Quote replay requires a price and timestamp")
        if self.result.closed:
            return None
        action = self.check_native_exit(price, price, price, when, open_price=price)
        if action:
            return action
        action = quote_action(self.trade_state, self.rules, price)
        self._bar_high = self._bar_low = price
        if action == MOVE_SL_TO_BE:
            self._on_tp1(price, when)
        elif action in (PARTIAL_TP2, EXIT_TP):
            self._on_tp2(action, price, when)
        return action

    def _stop_executable_at_close(self, proposed: float, close: float) -> bool:
        """A new stop must be on the protective side of the current price.

        A bar's earlier high/low is not an executable price at update time.
        Live may clamp to its broker stop distance; without that quote/spec
        snapshot the simulator conservatively rejects an impossible request.
        """
        is_long = self.trade_state.get("direction") == "long"
        executable = proposed < close if is_long else proposed > close
        if not executable:
            self.result.rejected_stop_updates += 1
            logger.warning(
                f"Simulated stop update rejected for {self.asset}: proposed={proposed} "
                f"close={close} direction={self.trade_state.get('direction')} "
                "(stop must be beyond current price on protective side)"
            )
        return executable

    def _apply_trail(self, close: float) -> None:
        """Move the stop, never backwards — the live TRAIL_SL contract."""
        proposed = self.trade_state.pop("_pending_trail_sl", None)
        label = self.trade_state.pop("_trail_label", "trail")
        rollback = self.trade_state.pop("_pending_trail_rollback", None)
        if proposed is None:
            return
        current = self.trade_state.get("sl")
        is_long = self.trade_state.get("direction") == "long"
        if current is not None:
            if is_long and proposed < current:
                return
            if not is_long and proposed > current:
                return
        if not self._stop_executable_at_close(proposed, close):
            if rollback:
                self.trade_state.update(rollback)
            return
        logger.debug(f"trail {label}: {current} -> {proposed}")
        self.trade_state["sl"] = proposed

    def _splittable_volume(self, fraction: float) -> Optional[float]:
        """Volume a broker partial-close would ACTUALLY close, or None.

        Replicates ``MT5Adapter.partial_close`` exactly, because the difference
        is not cosmetic on a small account:

            close_volume = floor(volume x fraction / lot_step) x lot_step
            close_volume = max(close_volume, min_lot)
            if close_volume <= 0 or close_volume >= volume: -> refuse (False)

        On a 0.01-lot position — which is what a $1,000 account gets on gold or
        silver — 0.01 x 0.5 floors to 0, clamps up to min_lot 0.01, and 0.01 is
        not < 0.01, so the broker REFUSES. Live therefore books nothing at TP1
        and closes the position outright at TP2. Booking a notional 0.005 lots,
        as this simulator did, models a fill no broker would ever give.
        """
        volume = self.open_qty
        if volume <= 0 or not 0.0 < fraction < 1.0:
            return None
        specs = _contract_specs_for(self.asset)
        if specs is None:
            return volume * fraction  # unknown instrument: unquantised, as before
        step = float(specs.lot_step or 0.0)
        min_lot = float(specs.min_lot or 0.0)
        close_volume = volume * fraction
        if step > 0:
            close_volume = math.floor(close_volume / step) * step
        close_volume = max(close_volume, min_lot)
        # Float noise: 0.02*0.5 must not land at 0.010000000000000002 > 0.01.
        close_volume = round(close_volume, 8)
        volume = round(volume, 8)
        if close_volume <= 0 or close_volume >= volume:
            return None
        return close_volume

    def _move_stop_to_be(self, close: Optional[float] = None) -> None:
        """SL -> entry +/- 0.5R (the live BE+0.5R move)."""
        entry = self.trade_state.get("entry")
        initial_stop = self.trade_state.get("initial_stop") or self.trade_state.get("sl")
        risk = abs(entry - initial_stop) if entry is not None and initial_stop is not None else 0.0
        current_stop = self.trade_state.get("sl")
        if self.trade_state.get("direction") == "long":
            candidate = entry + 0.5 * risk
            candidate = max(candidate, current_stop) if current_stop is not None else candidate
        else:
            candidate = entry - 0.5 * risk
            candidate = min(candidate, current_stop) if current_stop is not None else candidate
        if close is not None and not self._stop_executable_at_close(candidate, close):
            return
        self.trade_state["sl"] = candidate
        self.trade_state["sl_moved"] = True

    def _on_tp1(self, close: float, when: Any) -> None:
        """Book the TP1 partial (if the lot can be split), then SL -> BE+0.5R.

        Two live details this must honour:

        * the fill price is the BAR CLOSE, not the TP1 level. Live's monitor
          wakes on a closed bar and closes at market, and it records the partial
          at ``float(close)``; pricing the fill at the level would credit the
          backtest with a limit fill it never gets.
        * at min-lot the partial is REFUSED and no volume is booked — but the
          stop still moves to BE+0.5R and TP1 is marked consumed, so the branch
          does not re-fire. That is live's ``min_lot_skip`` path.
        """
        qty = self._splittable_volume(TP1_PARTIAL_FRACTION)
        if qty is None:
            logger.debug(
                f"TP1 partial skipped for {self.asset} — position {self.open_qty} "
                f"cannot be split at min-lot; stop still moves to BE+0.5R"
            )
        else:
            self._book("TP1_PARTIAL", qty, close, when)

        self._move_stop_to_be(close)
        self.trade_state["tp1_hit"] = True
        self.result.tp1_hit = True

    def _on_tp2(self, action: str, close: float, when: Any) -> None:
        """TP2: partial + runner, or a full close when the lot cannot be split."""
        tp2 = self.rules.get("tp")
        level = tp2 if tp2 is not None else close

        if action == EXIT_TP:
            # Runner disabled: a native limit TP exists at the broker.
            self._close_all(RESULT_TP, level, when, RESULT_TP)
            return

        qty = self._splittable_volume(TP2_PARTIAL_FRACTION)
        if qty is None:
            # "TP2 runner unavailable (cannot split) — full close at TP2":
            # Live downgrades to a MARKET close at the monitoring instant.
            # The native TP was stripped on entry because the runner was on;
            # booking the touched TP2 level here invents a limit fill.
            logger.debug(
                f"TP2 runner unavailable for {self.asset} — position {self.open_qty} "
                f"cannot be split; full close at TP2"
            )
            self._close_all(RESULT_TP, close, when, RESULT_TP)
            return

        self._book("TP2_PARTIAL", qty, close, when)
        self.trade_state["tp2_hit"] = True
        self.result.tp2_hit = True
        # Chandelier seeded from THIS bar's favourable extreme, exactly as live
        # does — not from the TP2 level, which would start the trail tighter.
        is_long = self.trade_state.get("direction") == "long"
        self.trade_state["runner_extreme"] = self._bar_high if is_long else self._bar_low
        # If price gapped straight through TP1, the runner would still be
        # carrying its original stop. Live locks BE+0.5R here so a runner is
        # never left able to lose money.
        if not self.result.tp1_hit:
            self._move_stop_to_be(close)
            self.trade_state["tp1_hit"] = True
            self.result.tp1_hit = True

    def _on_sl(
        self, close: float, high: float, low: float, when: Any,
        *, open_price: Optional[float] = None,
    ) -> None:
        sl = self.trade_state.get("sl")
        price = sl if sl is not None else close
        if sl is not None and open_price is not None:
            is_long = self.trade_state.get("direction") == "long"
            price = min(sl, open_price) if is_long else max(sl, open_price)
        if self.result.tp2_hit:
            label = RESULT_TRAIL
        elif self.trade_state.get("sl_moved"):
            label = RESULT_BE
        else:
            label = RESULT_SL
        self._close_all(label, price, when, label)

    def force_close(self, price: float, when: Any, reason: str = RESULT_TIMEOUT) -> None:
        """Close what is left at market — the monitor's expiry / pre-close path."""
        if self.result.closed:
            return
        self._close_all(reason, price, when, reason)
