from collections.abc import Mapping
from typing import Any, Dict

import pandas as pd
from .trigger_check import entry_trigger_met
from .logger import get_logger
from .config import BACKTEST_INSTRUMENT_CONFIG, SETTLEMENT_CANDLES, TRADE_SIM_MAX_DURATION_HOURS
from .backtest_types import RESULT_NO_ENTRY, RESULT_OPEN, RESULT_TIMEOUT
from .backtest_account import (
    PositionSimulator,
    build_rules,
    contract_size_for,
    usd_pnl,
)
from .prediction import Prediction

logger = get_logger(__name__)


def _norm(pred: Prediction | Mapping[str, Any] | None) -> dict:
    """Normalize prediction to internal format.
    
    Maps canonical field names (entry, stop, target) to internal format (level, stop, target).
    Trade sim uses 'level' internally for compatibility with Prediction model.
    
    Returns an empty dict for non-tradeable signals (missing/invalid direction)
    so the caller's ``if not pred`` guard short-circuits cleanly.
    """
    if isinstance(pred, Prediction):
        if pred.direction is None:
            return {}
        return {
            "direction": pred.direction,
            "level": pred.level,  # Prediction model uses 'level'
            "stop": pred.stop,
            "target": pred.target,
            "condition": pred.condition,
            "buffer": pred.buffer,
            "pattern": pred.condition,
        }
    if isinstance(pred, Mapping):
        d = dict(pred)
        
        # Early exit when direction is absent — avoids wasted simulation work
        direction = d.get("direction")
        if direction is None:
            return {}
        
        # Map canonical field names to internal format
        # Canonical: entry, stop, target
        # Internal: level, stop, target (Prediction model uses 'level')
        if "entry" in d and "level" not in d:
            d["level"] = d["entry"]  # Map canonical 'entry' to internal 'level'
        elif "entry_price" in d and "level" not in d:
            d["level"] = d["entry_price"]  # Legacy variant
        
        # Stop and target are already canonical
        if "stoploss" in d and "stop" not in d:
            d["stop"] = d["stoploss"]  # Legacy variant
        if "stop_loss" in d and "stop" not in d:
            d["stop"] = d["stop_loss"]  # Legacy variant
        if "take_profit_1" in d and "target" not in d:
            d["target"] = d["take_profit_1"]  # Legacy variant

        # TP1/TP2 (2026-08-07 live parity). Live queues the judge's OWN tp1 and
        # tp2; this simulator used to invent tp1 as the midpoint of entry→target
        # and ignore tp2 entirely, so a backtest "TP" was a different event from
        # a live one. Carry both through when the caller supplies them, and fall
        # back to the midpoint only when it does not (legacy call_sniper preds
        # carry a single `target`).
        if "tp1" not in d and d.get("take_profit_1") is not None:
            d["tp1"] = d["take_profit_1"]
        if "tp2" not in d and d.get("take_profit_2") is not None:
            d["tp2"] = d["take_profit_2"]

        d.setdefault("pattern", d.get("condition"))
        return d
    return {}


def simulate_trade(
    pred: Prediction | Mapping[str, Any] | None,
    fut: pd.DataFrame,
    symbol: str | None = None,
    trade_id: str | None = None,
    asset: str | None = None,
    max_trade_duration_hours: int = TRADE_SIM_MAX_DURATION_HOURS,
    lot_size: float | None = None,
    atr_1h: float | None = None,
) -> dict:
    """Simulate trade entry and exit with maximum duration enforcement.

    The simulator implements a two-target strategy:
    1. Half the position is booked at 50% of the reward (TP1).
    2. Stop loss moves to break-even after TP1 is hit.
    3. Remaining position exits on final target (TP2) or break-even.
    4. Maximum trade duration: 4 hours (48 x 5m candles). If neither TP nor SL hit, force exit at market price.

    Parameters
    ----------
    pred : Prediction | Mapping[str, Any] | None
        Trade prediction with entry, stop, target
    fut : pd.DataFrame
        Future candles for trade simulation
    symbol : str, optional
        Symbol for logging
    trade_id : str, optional
        Trade ID for logging
    asset : str, optional
        Asset symbol for trigger validation
    max_trade_duration_hours : int, default=TRADE_SIM_MAX_DURATION_HOURS
        Maximum trade duration in hours. Trade will be forced to exit after this duration.
    lot_size : float, optional
        Lot size in LOTS. When None the instrument's default_lot is used. The
        backtest runner passes the risk-sized lot so P&L matches live sizing.
    atr_1h : float, optional
        Cycle ATR(1h), used to freeze the post-TP2 chandelier distance exactly
        as ``services/code_sniper_monitor`` does when it queues a live trade.
    """

    pred = _norm(pred)
    pattern = pred.get("pattern")
    confidence = pred.get("confidence")

    # Lot resolution. P&L itself is contract-size based (see backtest_account),
    # so BACKTEST_INSTRUMENT_CONFIG is now consulted only for `default_lot`.
    _asset_key = (asset or symbol or "default").upper()
    _instr = BACKTEST_INSTRUMENT_CONFIG.get(_asset_key, BACKTEST_INSTRUMENT_CONFIG["default"])
    _lot = lot_size if lot_size is not None else _instr["default_lot"]
    _pnl_asset = asset or symbol

    if not pred:
        logger.debug("No tradeable signal; skipping trade simulation")
        return {
            "direction": None,
            "entry": None,
            "stop": None,
            "initial_stop": None,
            "target": None,
            "risk_reward": None,
            "entry_time": None,
            "exit_time": None,
            "exit_price": None,
            "profit": 0.0,
            "result": RESULT_NO_ENTRY,
        }

    # For code_sniper pattern, skip entry_trigger_met check
    # Code sniper already validated entry in generated code execution
    skip_trigger_check = (pattern == "code_sniper")
    
    if not skip_trigger_check:
        # Get buffer from prediction or use 0.0 (entry_trigger_met will calculate dynamic buffer)
        buffer = pred.get("buffer", 0.0)
        asset_symbol = asset or symbol or "default"  # Use provided asset, fallback to symbol or default
        
        if not entry_trigger_met(pred, fut, buffer, asset_symbol):
            logger.info(
                "No entry trigger met pattern=%s confidence=%s triggered=%s symbol=%s trade_id=%s",
                pattern,
                confidence,
                False,
                symbol,
                trade_id,
            )
            return {
                "direction": pred.get("direction"),
                "entry": pred.get("level"),
                "stop": pred.get("stop"),
                "initial_stop": pred.get("stop"),
                "target": pred.get("target"),
                "risk_reward": _calc_rr(pred),
                "entry_time": None,
                "exit_time": None,
                "exit_price": None,
                "profit": 0.0,
                "result": RESULT_NO_ENTRY,
            }

    if fut.empty:
        logger.info(
            "No future data available pattern=%s confidence=%s triggered=%s symbol=%s trade_id=%s",
            pattern,
            confidence,
            True,
            symbol,
            trade_id,
        )
        return {
            "direction": pred.get("direction"),
            "entry": pred.get("level"),
            "stop": pred.get("stop"),
            "initial_stop": pred.get("stop"),
            "target": pred.get("target"),
            "risk_reward": _calc_rr(pred),
            "entry_time": None,
            "exit_time": None,
            "exit_price": None,
            "profit": 0.0,
            "result": RESULT_NO_ENTRY,
        }

    direction = pred.get("direction")
    entry = pred.get("level")
    initial_stop = pred.get("stop")
    tp = pred.get("target")

    if direction not in ("long", "short"):
        logger.debug(
            f"Invalid direction '{direction}' — cannot simulate trade. "
            f"Expected 'long' or 'short'."
        )
        return {
            "direction": direction,
            "entry": entry,
            "stop": initial_stop,
            "initial_stop": initial_stop,
            "target": tp,
            "risk_reward": None,
            "entry_time": None,
            "exit_time": None,
            "exit_price": None,
            "profit": 0.0,
            "result": RESULT_NO_ENTRY,
        }

    # Enforce maximum trade duration by limiting future candles.
    # +1 because entry is bar 0: with N bars the series spans (N-1)x5 minutes, so
    # slicing to exactly `max_candles` left the last bar 5 minutes SHORT of the
    # limit and the "force exit at max duration" branch below could never be
    # reached — every long-running trade came back "Open" instead of "Timeout"
    # and its P&L was silently dropped from the run's totals.
    max_candles = (max_trade_duration_hours * 60) // 5 + 1
    if len(fut) > max_candles:
        logger.info(
            f"Limiting future candles to {max_candles} (max duration: {max_trade_duration_hours}h) - had {len(fut)} candles",
            extra={
                "pattern": pattern,
                "symbol": symbol,
                "trade_id": trade_id,
                "max_duration_hours": max_trade_duration_hours
            }
        )
        fut = fut.iloc[:max_candles]
    
    # Early return for None prices (flat signals)
    if entry is None or initial_stop is None or tp is None:
        logger.info("Trade simulation skipped - None prices detected (flat signal)")
        return {
            "direction": direction,
            "entry": entry,
            "stop": initial_stop,
            "initial_stop": initial_stop,
            "target": tp,
            "risk_reward": None,
            "entry_time": None,
            "exit_time": None,
            "exit_price": None,
            "profit": 0.0,
            "result": RESULT_NO_ENTRY,
        }
    
    # ── TP1 / TP2 (live parity) ──────────────────────────────────────────
    # Live queues the judge's own TP1 and TP2 and books a partial at each. When
    # the caller supplies only a single `target` (the legacy call_sniper pred),
    # fall back to the historical midpoint split so those runs are unchanged.
    tp1 = pred.get("tp1")
    if tp1 is None:
        tp1 = entry + (tp - entry) / 2 if direction == "long" else entry - (entry - tp) / 2
    tp2 = pred.get("tp2") if pred.get("tp2") is not None else tp

    rules = build_rules(tp1, tp2, entry=entry, stop_loss=initial_stop, atr_1h=atr_1h)
    trade_state = {
        "direction": direction,
        "entry": entry,
        "sl": initial_stop,
        "initial_stop": initial_stop,
        "sl_moved": False,
        "tp1_hit": False,
        "tp2_hit": False,
        "asset": _pnl_asset,
    }
    rr = _calc_rr(pred)

    entry_time = fut.index[0]
    if isinstance(entry_time, pd.Timestamp) and entry_time.tzinfo is None:
        entry_time = entry_time.tz_localize("UTC")

    # When the pred entered on the first future bar (code-sniper trigger), that
    # bar is the ENTRY bar: skip it entirely and start the settlement window on
    # the next one, which is where live's monitor starts counting.
    entry_row_is_trigger = bool(pred.get("entry_timestamp")) and (pattern == "code_sniper")
    sim = PositionSimulator(
        _pnl_asset, trade_state, rules, _lot,
        settlement_candles=SETTLEMENT_CANDLES if entry_row_is_trigger else 0,
        skip_first_bar=entry_row_is_trigger,
    )

    # Pre-close force close (live parity). Live checks _is_market_closing_soon()
    # on every bar and flattens ahead of the 20:55 daily maintenance and the
    # Friday weekend close. Most backtest trades resolve HERE rather than in the
    # checkpoint monitor, so without this a position entered late in the session
    # rides straight through a gap the live system is never in. Imported lazily:
    # this module is imported by the runner, and the monitor package pulls in the
    # broker adapter.
    _closing_soon = None
    if _pnl_asset:
        try:
            from .services.code_sniper_monitor import _is_market_closing_soon
            _closing_soon = _is_market_closing_soon
        except Exception as e:  # never let a guard import break simulation
            logger.warning(f"pre-close guard unavailable: {e}")

    for t, row in fut.iterrows():
        when = t
        if isinstance(when, pd.Timestamp) and when.tzinfo is None:
            when = when.tz_localize("UTC")

        if _closing_soon is not None:
            closing, close_reason = _closing_soon(_pnl_asset, now=when)
            if closing:
                sim.force_close(row.close, when, reason=RESULT_TIMEOUT)
                logger.info(
                    f"Market closing soon for {_pnl_asset} ({close_reason}) at {when} — "
                    f"force-closing to avoid gap risk (pnl={sim.result.pnl:.4f})"
                )
                return _result_dict(
                    direction, entry, trade_state["sl"], tp2, rr, entry_time,
                    when, row.close, sim.result.pnl,
                    RESULT_TIMEOUT, sim.result.tp1_pnl, initial_stop, tp1,
                )

        sim.step(row.close, row.high, row.low, when, open_price=float(row.open))
        if sim.result.closed:
            logger.info(
                "simulate_trade exit: result=%s exit_price=%s pnl=%.4f pattern=%s symbol=%s trade_id=%s",
                sim.result.result, sim.result.exit_price, sim.result.pnl,
                pattern, symbol, trade_id,
            )
            return _result_dict(
                direction, entry, trade_state["sl"], tp2, rr, entry_time,
                sim.result.exit_time, sim.result.exit_price, sim.result.pnl,
                sim.result.result, sim.result.tp1_pnl, initial_stop, tp1,
            )

    # Trade still open after exhausting future candles
    if fut.empty:
        current_exit_time = None
        current_price = entry
    else:
        current_exit_time = fut.index[-1]
        if isinstance(current_exit_time, pd.Timestamp) and current_exit_time.tzinfo is None:
            current_exit_time = current_exit_time.tz_localize("UTC")
        current_price = fut.iloc[-1].close

    trade_duration_minutes = 0
    if entry_time is not None and current_exit_time is not None:
        trade_duration_minutes = (current_exit_time - entry_time).total_seconds() / 60

    max_duration_minutes = max_trade_duration_hours * 60
    if trade_duration_minutes >= max_duration_minutes:
        # Monitor expiry: close whatever volume is still open at market. Any
        # TP1/TP2 legs already booked stay booked — the old code discarded them
        # and reported only the final leg.
        sim.force_close(current_price, current_exit_time, reason=RESULT_TIMEOUT)
        profit = sim.result.pnl
        # Emit the canonical label. This used to be "Time Exit (Win)" /
        # "Time Exit (Loss)" / "Time Exit (BE)", which no consumer recognised:
        # the first two were binned as "other" and dropped out of every headline
        # metric, and the third was counted as a break-even. Whether a timeout
        # was a win is decided by its P&L, which reporting already does.
        logger.info(
            f"Trade forced to exit after {max_trade_duration_hours}h max duration: "
            f"pnl={profit:.4f} symbol={symbol} trade_id={trade_id}"
        )
        return _result_dict(
            direction, entry, trade_state["sl"], tp2, rr, entry_time,
            current_exit_time, current_price, profit, RESULT_TIMEOUT,
            sim.result.tp1_pnl, initial_stop, tp1,
        )

    # Still open and inside the duration limit — the caller keeps monitoring.
    # Realised legs (a booked TP1/TP2) are reported so partial capture is not
    # invisible while the runner is still alive.
    logger.info(
        "Trade still open after monitoring window pattern=%s confidence=%s symbol=%s trade_id=%s",
        pattern, confidence, symbol, trade_id,
    )
    return _result_dict(
        direction, entry, trade_state["sl"], tp2, rr, entry_time,
        current_exit_time, current_price, 0.0, RESULT_OPEN,
        sim.result.tp1_pnl, initial_stop, tp1,
    )



def _calc_rr(pred: dict) -> float:
    """Risk-to-reward measured entry → **TP1**, falling back to ``target``.

    TP1 is the right denominator and the project already says so: the live
    accept/reject gate is ``CS_MIN_RR_RATIO_TP1`` (RR to the FIRST target with
    the final stop), and "RR on TP2 not TP1" is listed as a known defect in
    PERFORMANCE_LIMITERS. Since ``target`` now carries TP2 (the level the runner
    actually runs to), reading RR off it would silently roughly double every
    reported R and make the go-live scorecard's ">= 2R" check pass on setups the
    live gate measures at half that.
    """
    try:
        entry = pred["level"]
        stop = pred["stop"]
        target = pred.get("tp1") if pred.get("tp1") is not None else pred["target"]
        risk = abs(entry - stop)
        reward = abs(target - entry)
        return reward / risk if risk != 0 else float("inf")
    except Exception:
        # If the prediction lacks any of the required fields we can't compute an
        # RR value.  ``NaN`` makes it explicit that the value is undefined and
        # matches expectations in the golden backtest.
        return float("nan")


def _calc_profit(
    direction: str,
    entry: float,
    exit_price: float,
    lot_size: float = 1.0,
    asset: str | None = None,
) -> float:
    """Return profit in account currency, using the instrument's CONTRACT SIZE.

    2026-08-07: this used to be ``(price_diff / pip_size) × pip_value_per_lot ×
    lot``. That is correct for FX/BTC/USTEC and wrong for every metal, whose
    ``pip_value_per_lot`` is 1.0 while a lot is 100 oz of gold or 5,000 oz of
    silver — so gold P&L came out 100× and silver 5,000× too small. Live has
    used the contract-size formula since the PERFORMANCE_LIMITERS fix; this is
    the same function, so the two can no longer disagree.
    """
    return usd_pnl(asset, direction, entry, exit_price, lot_size)


# ``determine_exit_price`` lived here until 2026-08-07. It resolved an exit
# price from an action + candle, but PositionSimulator now owns exit pricing (it
# has to: with the TP1/TP2/runner ladder an exit is a LEG at a known level, not a
# single price for the whole position). Nothing called it any more, and it had
# silently become a NameError waiting to happen once EXIT_TP/EXIT_SL stopped
# being imported here — so it is gone rather than left as a trap.


def _result_dict(direction, entry, stop, target, rr, entry_time, exit_time, exit_price,
                 profit, result, tp1_points, initial_stop, tp1=None):
    return {
        "direction": direction,
        "entry": entry,
        "stop": stop,
        "initial_stop": initial_stop,
        # `target` is the FINAL target (TP2) — what the runner runs to. TP1 is
        # carried alongside it so reporting can measure RR against the level the
        # live gate uses instead of re-deriving it from the final target.
        "target": target,
        "take_profit_1": tp1,
        "take_profit_2": target,
        "tp1_points": tp1_points,
        "risk_reward": rr,
        "entry_time": entry_time,
        "exit_time": exit_time,
        "exit_price": exit_price,
        "profit": profit,
        "result": result,
    }
