"""Backtest runner for executing backtests with date ranges and interval-based iterations."""

from __future__ import annotations

import asyncio
import bisect
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

import pandas as pd

from .utils import _first_not_none
from .ai_accounting import trade_reason
from .multi_expert import multi_expert_decision_with_details
from .backtest_account import build_rules, size_position
from .models import Side
from .trade_geometry import cap_structural_targets, first_target_rr
from .backtest_armed_strategy import MAX_CONSECUTIVE_CODE_FAILURES, ArmedStrategy
from .backtest_bar_feed import ClosedBarFeed, BAR_DURATION, bar_close_time
from .backtest_position_monitor import (
    position_signature,
    OpenPosition,
    PositionClosed,
    PositionMonitor,
    unrealised_snapshot,
)
from .historical_candle_service import HistoricalCandleService
from .backtest_executor import BacktestExecutor
from .code_executor import execute_generated_code
from .logger import get_logger
from .candles import filter_consecutive_candles, is_expected_market_gap
from .asset_registry import CRYPTO_SYMBOLS
from .candle_type_validation import (
    validate_candle_list_types,
    normalize_candle_list_types,
    normalize_candle_types
)
from .backtest_normalizer import normalize_iteration_data
from .backtest_types import (
    IterationDict,
    PredictionDict,
    OutcomeDict,
)
from packages.db.repositories import backtest as backtest_repo
from .config import (
    BACKTEST_MIN_RR,
    BACKTEST_MAX_TRADES_PER_DAY,
    BACKTEST_COOLDOWN_AFTER_SL,
    BACKTEST_COOLDOWN_SL_THRESHOLD,
    BACKTEST_INSTRUMENT_CONFIG,
    BT_INTERVAL_5M_SECONDS,
    BT_INTERVAL_15M_SECONDS,
    BT_HISTORICAL_WINDOW,
    BT_TTL_DEFAULT_MINUTES,
    BT_TRADE_TIMEOUT_MINUTES,
    BT_ALLOWED_COUNTS,
    BT_INITIAL_CAPITAL,
    BT_RISK_PCT,
    BT_RISK_SIZING_ENABLED,
    # ── live behaviour the backtest must reproduce ────────────────────────
    ACCOUNT_DAILY_DD_GUARD_ENABLED,
    ACCOUNT_MAX_DAILY_DD_PCT,
    ALIGNED_TOUCH_FILL_MODE,
    ATR_STOP_REFERENCE_TF,
    CS_MAX_ENTRY_DEVIATION_RISK_MULT,
    CS_MAX_RR_RATIO,
    CS_MIN_RR_RATIO_TP1,
    CS_MIN_DISTANCE_TO_SL_PCT,
    CS_MIN_DISTANCE_TO_SL_PCT_BY_ASSET,
    CS_MIN_DISTANCE_TO_TP_PCT,
    CS_MIN_DISTANCE_TO_TP_PCT_BY_ASSET,
    HTF_CANDLE_COUNTS,
    HTF_CANDLE_COUNTS_BY_ASSET,
    HTF_CONTEXT_ENABLED,
    SL_ANCHOR_MODE,
    SL_ANCHOR_PAD_ATR_FRAC,
    TREND_AGREEMENT_ENABLED,
    TTL_MINUTES_ALLOWED,
    aligned_touch_fill_mode_for,
    coerce_ttl,
    resolve_for_asset,
    session_tape_mode_for,
    sl_anchor_mode_for,
    trend_agreement_from,
)
from .market_structure import (
    recommended_stop_distance,
    atr_from_candles,
    candle_time_utc,
    compute_htf_context,
    compute_session_tape,
)
from .risk.daily_guards import DailyGuards
from .risk.news_blackout import news_blackout_active
from .services.code_sniper_monitor import _is_market_closing_soon

logger = get_logger(__name__)


@dataclass
class BarAdvance:
    """What the bar clock did between two checkpoints.

    Returned rather than mutated in place so the checkpoint loop stays a
    readable sequence of `advance -> record -> decide`, and so the bar clock
    can be exercised in a test without a runner-sized fixture.
    """

    open_position: Optional[OpenPosition]
    armed_strategy: Optional[ArmedStrategy]
    last_processed_bar_time: Optional[datetime]
    refuse_signature: Optional[tuple]
    closes: List[PositionClosed] = field(default_factory=list)
    opened: List[OpenPosition] = field(default_factory=list)
    rejections: List[str] = field(default_factory=list)


def _as_utc_datetime(value: Any) -> datetime:
    """Any timestamp-like value as a timezone-aware UTC datetime."""
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    return stamp.to_pydatetime()


_INTERVAL_5M_SECONDS   = BT_INTERVAL_5M_SECONDS
_INTERVAL_15M_SECONDS  = BT_INTERVAL_15M_SECONDS
_HISTORICAL_WINDOW     = BT_HISTORICAL_WINDOW
_TTL_DEFAULT_MINUTES   = BT_TTL_DEFAULT_MINUTES
_TRADE_TIMEOUT_MINUTES = BT_TRADE_TIMEOUT_MINUTES

# pandas resample rules for the HTF series the live runner fetches from the
# broker. Backtests have no broker, so the same series is built from the 5m
# cache — see BacktestRunner._build_htf_candles.
_HTF_RESAMPLE_RULES: Dict[str, str] = {"1h": "1h", "4h": "4h", "1d": "1D"}

ALLOWED_COUNTS = BT_ALLOWED_COUNTS


class BacktestRunner:
    """Runner for executing backtests with date ranges and interval-based iterations.
    
    Parameters
    ----------
    asset : str
        Asset symbol (e.g., 'NIFTY')
    start_date : datetime
        Start date for backtest (timezone-aware)
    end_date : datetime
        End date for backtest (timezone-aware)
    candle_count_5m : int
        Number of 5-minute candles to use (default: 100)
    candle_count_15m : int
        Number of 15-minute candles to use (default: 70)
    use_code_sniper : bool
        Must be True. Kept as a parameter because callers pass it explicitly;
        False is rejected in __init__ (there is no live call_sniper path).
    gpt_config : Dict[str, Any]
        GPT configuration dictionary (default: {})
    initial_capital : float
        Starting account balance. Sizing is ``balance × risk_pct`` floored to
        the broker min_lot — the live formula — so this is what determines
        position size and therefore P&L. Compounds across the run.
    risk_pct : float
        Fraction of balance risked per trade (default: the live
        ``CS_DEFAULT_RISK_PCT``).
    lot_size : float
        Fixed lot used ONLY when ``use_risk_sizing`` is False (price-R studies).
    use_risk_sizing : bool
        When True (default) every trade is sized against the account exactly as
        live does, including the LP-01 risk-budget skip.
    """

    def __init__(
        self,
        asset: str,
        start_date: datetime,
        end_date: datetime,
        candle_count_5m: int = 100,
        candle_count_15m: int = 50,
        use_code_sniper: bool = True,
        gpt_config: Dict[str, Any] | None = None,
        lot_size: float = 1.0,
        initial_capital: float = BT_INITIAL_CAPITAL,
        risk_pct: float = BT_RISK_PCT,
        use_risk_sizing: bool = BT_RISK_SIZING_ENABLED,
    ):
        if candle_count_5m not in ALLOWED_COUNTS:
            raise ValueError(f"Invalid 5m candle count: {candle_count_5m}. Must be one of {ALLOWED_COUNTS}")
        if candle_count_15m not in ALLOWED_COUNTS:
            raise ValueError(f"Invalid 15m candle count: {candle_count_15m}. Must be one of {ALLOWED_COUNTS}")
        if start_date >= end_date:
            raise ValueError("start_date must be before end_date")
        if initial_capital <= 0:
            raise ValueError(f"initial_capital must be positive, got {initial_capital}")
        if not 0 < risk_pct < 1:
            raise ValueError(f"risk_pct must be a fraction in (0, 1), got {risk_pct}")
        if not use_code_sniper:
            # Live runs one decision pipeline: multi-expert -> judge -> generated
            # strategy -> per-bar trigger. The legacy single-shot `call_sniper`
            # path has no armed window and no trigger bar, so a backtest on it
            # could not mimic live even in principle. Refusing is the honest
            # answer; running it would report a strategy nobody trades.
            raise ValueError(
                "use_code_sniper=False is not supported: the backtest mirrors the "
                "live code-sniper pipeline, and no live path produces call_sniper "
                "signals."
            )

        self.asset = asset
        self.start_date = start_date
        self.end_date = end_date
        self.candle_count_5m = candle_count_5m
        self.candle_count_15m = candle_count_15m
        self.use_code_sniper = use_code_sniper
        self.gpt_config = gpt_config or {}
        self.lot_size = lot_size

        # ── Account ──────────────────────────────────────────────────────
        # `balance` is mutated as trades close so sizing compounds the way the
        # live balance does: a drawdown shrinks the next position, and the
        # risk-budget gate starts refusing instruments the account can no
        # longer afford at min_lot. `initial_capital` is kept for reporting.
        self.initial_capital = float(initial_capital)
        self.balance = float(initial_capital)
        self.risk_pct = float(risk_pct)
        self.use_risk_sizing = bool(use_risk_sizing)

        # Initialize historical candle service and executor
        self.candle_service = HistoricalCandleService()
        self.executor = BacktestExecutor()

        # Cache for full date range candles (fetched once at start)
        self._cached_5m_candles: List[Dict[str, Any]] | None = None
        self._cached_15m_candles: List[Dict[str, Any]] | None = None

        # O(1) lookup structures for candle indexing
        self._candle_5m_index_map: Dict[pd.Timestamp, int] | None = None
        self._candle_15m_index_map: Dict[pd.Timestamp, int] | None = None
        # Pre-sorted timestamp lists for O(log n) bisect lookups
        self._sorted_5m_times: List[pd.Timestamp] | None = None
        self._sorted_15m_times: List[pd.Timestamp] | None = None
        # 5m cache as a DataFrame, resampled on demand into the 1h/4h/1d series
        # the live runner fetches from the broker (built once in execute_backtest).
        self._htf_source_df: pd.DataFrame | None = None
        # Per-cycle execution context (atr_1h / sl_floor / trend_agreement),
        # rebuilt at each checkpoint — the backtest twin of the live runner's
        # ``_cycle_risk_meta``.
        self._cycle_context: Dict[str, Any] = {}
        # The bar clock and the position lifecycle, both built in
        # execute_backtest once the candle cache exists.
        self._bar_feed: ClosedBarFeed | None = None
        self._position_monitor: PositionMonitor | None = None
        from .operational_ledger import OperationalLedger
        self._operational_ledger = OperationalLedger(":memory:")
        self._ledger_account = "replay"

    # Crypto assets — the only ones that should get %-based SL widening
    # NOTE: XAUUSD/XAGUSD are commodities with much lower prices than crypto;
    # %-based SL widening (0.15% of ~$5300 = $7.95) distorts gold trades.
    # Gold uses absolute BACKTEST_MIN_SL_DISTANCE ($2) instead.
    # Derived from the single asset registry (incl. legacy USDT aliases).
    _CRYPTO_ASSETS = CRYPTO_SYMBOLS

    def _is_crypto_asset(self) -> bool:
        return self.asset.upper() in self._CRYPTO_ASSETS

    def _build_htf_source(self) -> None:
        """Index the 5m cache once as a DataFrame for HTF resampling."""
        if not self._cached_5m_candles:
            self._htf_source_df = None
            return
        df = pd.DataFrame(self._cached_5m_candles)
        if "time" not in df.columns:
            self._htf_source_df = None
            return
        df["time"] = pd.to_datetime(df["time"], utc=True)
        df = df.set_index("time").sort_index()
        keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
        self._htf_source_df = df[keep]

    def _build_htf_candles(self, as_of: pd.Timestamp) -> Dict[str, List[Dict[str, Any]]]:
        """1h/4h/1d candles as of a checkpoint, in the live fetch's shape.

        Only bars that had CLOSED by ``as_of`` are returned — the partially
        formed bar the checkpoint sits inside is dropped, because live only
        ever sees closed broker bars. Counts come from the same
        ``HTF_CANDLE_COUNTS`` the live fetch uses, so the judge sees a series of
        the same depth.

        Returns ``{}`` when HTF context is disabled or there is not enough
        history, which leaves every downstream consumer inert exactly as the
        live fetch's ``{}`` does on a broker failure.
        """
        if not HTF_CONTEXT_ENABLED or self._htf_source_df is None or self._htf_source_df.empty:
            return {}
        as_of = pd.Timestamp(as_of)
        if as_of.tzinfo is None:
            as_of = as_of.tz_localize("UTC")
        counts = resolve_for_asset(HTF_CANDLE_COUNTS_BY_ASSET, self.asset, None) or HTF_CANDLE_COUNTS

        src = self._htf_source_df.loc[:as_of - BAR_DURATION]
        if src.empty:
            return {}

        out: Dict[str, List[Dict[str, Any]]] = {}
        for interval, limit in counts.items():
            rule = _HTF_RESAMPLE_RULES.get(interval)
            if not rule:
                continue
            try:
                agg = src.resample(rule, label="left", closed="left").agg(
                    {"open": "first", "high": "max", "low": "min",
                     "close": "last", "volume": "sum"}
                ).dropna(subset=["close"])
            except Exception as e:
                logger.warning(f"[BACKTEST-HTF] resample to {interval} failed: {e}")
                continue
            if agg.empty:
                continue
            # Drop the bar the checkpoint is still inside: its right edge is in
            # the future, so including it would leak information live cannot have.
            period = pd.Timedelta(rule)
            if agg.index[-1] + period > as_of:
                agg = agg.iloc[:-1]
            if agg.empty:
                continue
            agg = agg.tail(int(limit))
            out[interval] = [
                {
                    "time": idx.to_pydatetime(),
                    "open": float(row["open"]), "high": float(row["high"]),
                    "low": float(row["low"]), "close": float(row["close"]),
                    "volume": float(row.get("volume", 0.0) or 0.0),
                }
                for idx, row in agg.iterrows()
            ]
        return out

    def _compute_cycle_context(
        self,
        htf_candles: Dict[str, List[Dict[str, Any]]],
        c5_candles: List[Dict[str, Any]],
        as_of: datetime,
    ) -> Dict[str, Any]:
        """The backtest twin of ``AgentRunner._compute_cycle_risk_meta``.

        Produces the dict the executor reads at entry time: ``atr_1h`` (runner
        chandelier + sl_anchor pad), ``sl_floor`` (volatility auto-widen),
        ``htf_trend``/``regime_hint`` and — when TREND_AGREEMENT_ENABLED — the
        unanimous ``trend_agreement`` the aligned touch-fill keys on.
        """
        ctx: Dict[str, Any] = {"risk_pct": self.risk_pct}
        try:
            ref = (htf_candles or {}).get(ATR_STOP_REFERENCE_TF) or []
            atr_ref = atr_from_candles(ref) if ref else None
            if atr_ref is None:
                # Same fallback the prompt's stop hint uses when the HTF fetch
                # came back empty, so floor and context never disagree.
                atr_ref = atr_from_candles(c5_candles)
            if atr_ref is not None:
                ctx["atr_1h"] = atr_ref
            price = None
            if c5_candles:
                try:
                    price = float(c5_candles[-1].get("close"))
                except (TypeError, ValueError):
                    price = None
            floor = recommended_stop_distance(atr_ref, price, self.asset, as_of)
            if floor:
                ctx["sl_floor"] = floor

            htf_ctx = compute_htf_context(htf_candles or {}, self.asset, as_of) or {}
            if htf_ctx.get("er_1h") is not None:
                ctx["er_1h"] = htf_ctx.get("er_1h")
            if htf_ctx.get("htf_trend"):
                ctx["htf_trend"] = htf_ctx["htf_trend"]
            if htf_ctx.get("regime_hint"):
                ctx["regime_hint"] = htf_ctx["regime_hint"]
            if TREND_AGREEMENT_ENABLED:
                ctx["trend_agreement"] = trend_agreement_from(htf_ctx)
                ctx["trends_htf"] = [htf_ctx.get("trend_1h"), htf_ctx.get("trend_4h"),
                                     htf_ctx.get("trend_1d")]
        except Exception as e:
            logger.warning(f"[BACKTEST-CONTEXT] cycle context failed (inert): {e}")
        return ctx

    def _observe_session_tape(
        self,
        c5_candles: List[Dict[str, Any]],
        htf_trend: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """LP-08 session tape; returns it only in "context" mode, like live.

        Mirrors ``AgentRunner._observe_session_tape``: computed for every asset
        whose mode is not "off", logged when it flags a conflict, but only
        surfaced to the prompt when the asset is on "context".
        """
        try:
            mode = session_tape_mode_for(self.asset)
            if mode == "off":
                return None
            tape = compute_session_tape(c5_candles, htf_trend, self.asset)
            if tape is None:
                return None
            if tape.get("conflict"):
                logger.info(
                    f"label_tape_conflict {self.asset}: htf_trend={tape.get('htf_trend')} vs "
                    f"session={tape.get('session')} dir={tape.get('direction')} "
                    f"move={tape.get('move_pct')}% er_session={tape.get('er_session')} "
                    f"er_2h={tape.get('er_2h')} bars={tape.get('bars')} "
                    f"(LP-08 observation, mode={mode})"
                )
            return tape if mode == "context" else None
        except Exception as e:
            logger.warning(f"[BACKTEST-CONTEXT] session tape failed (inert): {e}")
            return None

    def _min_target_distances(self) -> tuple[float, float]:
        """Per-asset (min_tp_pct, min_sl_pct) — executor's ``_min_target_distances``."""
        key = (self.asset or "").upper()
        if key not in CS_MIN_DISTANCE_TO_TP_PCT_BY_ASSET and key.endswith("M"):
            key = key[:-1]
        return (
            CS_MIN_DISTANCE_TO_TP_PCT_BY_ASSET.get(key, CS_MIN_DISTANCE_TO_TP_PCT),
            CS_MIN_DISTANCE_TO_SL_PCT_BY_ASSET.get(key, CS_MIN_DISTANCE_TO_SL_PCT),
        )

    def _aligned_touch_fill(
        self,
        entry: Optional[float],
        stop_loss: Optional[float],
        take_profit_1: Optional[float],
        trigger_candle: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        """Promote a 'hold' when an ALIGNED setup's entry is touched (2026-07-29).

        Same predicate as ``CodeSniperExecutor._aligned_touch_fill``: unanimous
        1h/4h/1d agreement in this cycle's context, the judge's own levels
        trading WITH it, and the bar's range containing the judged entry.
        Returns 'long'/'short' (the backtest's vocabulary) or None.
        """
        mode = aligned_touch_fill_mode_for(self.asset, ALIGNED_TOUCH_FILL_MODE)
        if mode == "off":
            return None
        if not entry or not stop_loss or not take_profit_1 or entry == stop_loss:
            return None
        agreement = (self._cycle_context or {}).get("trend_agreement")
        if agreement not in ("up", "down"):
            return None
        declared = "long" if stop_loss < entry else "short"
        if (declared == "long") != (agreement == "up"):
            return None
        try:
            high = float((trigger_candle or {}).get("high"))
            low = float((trigger_candle or {}).get("low"))
        except (TypeError, ValueError):
            return None
        if not (low <= entry <= high):
            return None
        logger.warning(
            f"Aligned touch-fill {'FILLING' if mode == 'fill' else 'would fill (mode=log)'}: "
            f"{self.asset} {declared} at judged entry {entry} touched by bar "
            f"[{low}, {high}] with 1h/4h/1d all {agreement}; trigger code said hold"
        )
        return declared if mode == "fill" else None

    def _apply_execution_gates(
        self,
        direction: str,
        entry: Optional[float],
        stop_loss: Optional[float],
        take_profit_1: Optional[float],
        take_profit_2: Optional[float],
        trigger_price: Optional[float],
        sl_anchor: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Run the live gate chain. Returns the executed levels or a rejection.

        Result: ``{"ok": bool, "reason": str, "entry"/"stop"/"tp1"/"tp2": float}``.
        """
        ctx = self._cycle_context or {}
        is_long = direction == "long"

        if entry is None or stop_loss is None or take_profit_1 is None:
            return {"ok": False, "reason": "missing_levels"}
        if take_profit_2 is None:
            # The code executor auto-derives TP2 = TP1 when the judge omits it.
            take_profit_2 = take_profit_1

        # ── 1. Fill-fidelity gate + real-entry substitution (audit F2) ────
        if trigger_price and entry and stop_loss:
            risk = abs(entry - stop_loss)
            deviation = abs(trigger_price - entry)
            if risk > 0 and deviation > CS_MAX_ENTRY_DEVIATION_RISK_MULT * risk:
                return {
                    "ok": False,
                    "reason": (
                        f"Entry rejected: trigger close {trigger_price} deviates "
                        f"{deviation / risk:.2f}x stop distance from judged entry {entry} "
                        f"(max {CS_MAX_ENTRY_DEVIATION_RISK_MULT}x)"
                    ),
                }
            # The order is MARKET: the real fill is the triggering bar's close,
            # so every gate below must judge THAT price, not the judge's constant.
            entry = trigger_price

        if (1.0 if is_long else -1.0) * (entry - stop_loss) <= 0:
            return {"ok": False, "reason": "structural stop is not protective"}

        # ── 2. Volatility SL floor — widen only a protective stop ────────
        try:
            sl_floor = float(ctx.get("sl_floor") or 0.0) or None
        except (TypeError, ValueError):
            sl_floor = None
        if sl_floor and entry and stop_loss:
            risk = abs(entry - stop_loss)
            if 0 < risk < sl_floor:
                old_sl = stop_loss
                stop_loss = entry - sl_floor if is_long else entry + sl_floor
                logger.info(
                    f"SL widened to volatility floor for {self.asset}: {old_sl} -> {stop_loss} "
                    f"(floor {sl_floor:.6g}, old risk {risk:.6g})"
                )

        # ── 3. sl_anchor contract (per-asset widen/log/off) ───────────────
        anchor_mode = sl_anchor_mode_for(self.asset, SL_ANCHOR_MODE)
        if anchor_mode != "off" and entry and stop_loss:
            if not isinstance(sl_anchor, dict) or not sl_anchor.get("price"):
                logger.info(f"sl_anchor missing from judge metadata for {self.asset}")
            else:
                try:
                    anchor_price = float(sl_anchor["price"])
                except (TypeError, ValueError):
                    anchor_price = None
                try:
                    atr_1h = float(ctx.get("atr_1h") or 0.0)
                except (TypeError, ValueError):
                    atr_1h = 0.0
                pad = SL_ANCHOR_PAD_ATR_FRAC * atr_1h if atr_1h > 0 else 0.0005 * entry
                anchor_ok = anchor_price is not None and (
                    anchor_price < entry if is_long else anchor_price > entry
                )
                if not anchor_ok:
                    logger.warning(
                        f"sl_anchor invalid (wrong side of entry or unparseable) - ignored: "
                        f"{self.asset} anchor={sl_anchor} entry={entry} dir={direction}"
                    )
                else:
                    required_sl = (anchor_price - pad) if is_long else (anchor_price + pad)
                    inside = stop_loss > required_sl if is_long else stop_loss < required_sl
                    if inside:
                        old_sl = stop_loss
                        if anchor_mode == "widen":
                            stop_loss = required_sl
                        logger.warning(
                            f"SL inside declared anchor for {self.asset}: stop {old_sl} vs "
                            f"anchor {anchor_price} ({sl_anchor.get('type', '?')}) + pad {round(pad, 6)}"
                            + (f" -> widened to {stop_loss}" if anchor_mode == "widen"
                               else f" (log-only, mode={anchor_mode})")
                        )

        # ── 4. Level validation: distances, RR-to-TP1, TP2 clamp ──────────
        ok, take_profit_1, take_profit_2, reason = self._validate_trade_levels(
            entry, stop_loss, take_profit_1, take_profit_2, is_long
        )
        if not ok:
            return {"ok": False, "reason": reason}

        return {
            "ok": True,
            "reason": "",
            "entry": entry,
            "stop": stop_loss,
            "tp1": take_profit_1,
            "tp2": take_profit_2,
        }

    def _validate_trade_levels(
        self,
        entry: float,
        stop_loss: float,
        take_profit_1: float,
        take_profit_2: float,
        is_long: bool,
    ) -> tuple[bool, float, float, str]:
        """Port of ``CodeSniperExecutor._validate_trade_levels``.

        Shared geometry caps TP2 without changing structural TP1. Distance
        and RR gates then check the final executable prices, just like live.
        """
        if not entry or entry <= 0:
            return False, take_profit_1, take_profit_2, "Entry price is zero or negative"

        try:
            take_profit_1, take_profit_2 = cap_structural_targets(
                entry, stop_loss, take_profit_1, take_profit_2,
                Side.BUY if is_long else Side.SELL, CS_MAX_RR_RATIO,
            )
        except ValueError as exc:
            return False, take_profit_1, take_profit_2, str(exc)

        d_tp1 = abs(entry - take_profit_1) / entry * 100
        d_tp2 = abs(entry - take_profit_2) / entry * 100
        d_sl = abs(entry - stop_loss) / entry * 100
        min_tp_pct, min_sl_pct = self._min_target_distances()
        if d_tp1 < min_tp_pct or d_tp2 < min_tp_pct or d_sl < min_sl_pct:
            return False, take_profit_1, take_profit_2, (
                f"Entry too close to targets — TP1: {d_tp1:.2f}%, TP2: {d_tp2:.2f}%, "
                f"SL: {d_sl:.2f}% (min {min_tp_pct}% TP / {min_sl_pct}% SL)"
            )

        rr_tp1 = first_target_rr(entry, stop_loss, take_profit_1)
        if rr_tp1 < CS_MIN_RR_RATIO_TP1:
            return False, take_profit_1, take_profit_2, (
                f"RR to TP1 {rr_tp1:.2f} below minimum {CS_MIN_RR_RATIO_TP1}"
            )

        return True, take_profit_1, take_profit_2, ""

    def _price_at(self, when: datetime, fallback: Optional[float] = None) -> Optional[float]:
        """Close of the newest M5 candle fully available at ``when``."""
        if not self._cached_5m_candles:
            return fallback
        ts = pd.Timestamp(when) - BAR_DURATION
        if self._sorted_5m_times and self._candle_5m_index_map:
            pos = bisect.bisect_right(self._sorted_5m_times, ts) - 1
            if pos >= 0:
                candle = self._cached_5m_candles[
                    self._candle_5m_index_map[self._sorted_5m_times[pos]]
                ]
                return candle.get("close", fallback)
            return fallback
        for c in reversed(self._cached_5m_candles):
            if pd.Timestamp(c.get("time")) <= ts:
                return c.get("close", fallback)
        return fallback

    def _settle_closed_trade(self, profit: float) -> None:
        """Apply a closed trade's P&L to the account balance.

        This is what makes sizing compound: after a drawdown the next
        ``balance × risk_pct`` budget is smaller, so the LP-01 risk-budget gate
        starts refusing instruments the account can no longer carry at min_lot —
        the same account-size-adaptive behaviour the live gate has.
        """
        if not self.use_risk_sizing:
            return
        self.balance = max(0.0, self.balance + float(profit or 0.0))

    def _execute_triggered_setup(
        self,
        direction: Optional[str],
        execution_result: Dict[str, Any],
        signal_dict: Dict[str, Any],
        trigger_candle: Dict[str, Any],
        timestamp_dt: datetime,
    ) -> Dict[str, Any]:
        """Turn a triggered strategy into an executable position, or reject it.

        The backtest twin of ``CodeSniperExecutor._convert_to_decision`` plus
        the entry brakes ``CodeSniperMonitor`` applies before it: the
        scheduled-release blackout (LP-04) runs first, because live never even
        builds an order inside one.

        Returns ``{"ok": False, "reason": ...}`` or
        ``{"ok": True, "entry", "stop", "tp1", "tp2", "quantity"}``.
        """
        if direction not in ("long", "short"):
            return {"ok": False, "reason": f"invalid direction {direction!r}"}

        # ── Scheduled-release blackout (LP-04) ───────────────────────────
        # New entries only; an open position is never touched. `timestamp_dt`
        # is the trigger bar, so this reads the historical clock, not today's.
        from .entry_policy import evaluate_entry_policy
        setup = signal_dict.get("metadata", {}).get("typed_setup", {})
        policy = evaluate_entry_policy(self.asset, timestamp_dt,
            horizon_minutes=setup.get("expected_horizon_minutes", signal_dict.get("metadata", {}).get("expected_horizon_minutes", 0)), static_policy=news_blackout_active)
        blackout, blackout_reason = policy["blocked"], policy["reason"]
        if blackout:
            return {"ok": False, "reason": f"Scheduled-release blackout — {blackout_reason}"}
        from .config import BACKTEST_MAX_TRADES_PER_DAY, BACKTEST_COOLDOWN_SL_THRESHOLD, TRADE_COOLDOWN_MINUTES, TRADE_THROTTLE_ENABLED
        blocked, reason = self._operational_ledger.throttle(self._ledger_account, self.asset, timestamp_dt,
            cap=BACKTEST_MAX_TRADES_PER_DAY, loss_threshold=BACKTEST_COOLDOWN_SL_THRESHOLD,
            cooldown_minutes=TRADE_COOLDOWN_MINUTES)
        if blocked and TRADE_THROTTLE_ENABLED:
            return {"ok": False, "reason": reason}

        entry = execution_result.get("entry")
        stop_loss = _first_not_none(execution_result.get("stop_loss"), execution_result.get("stop"))
        tp1 = _first_not_none(execution_result.get("take_profit_1"), execution_result.get("target"))
        tp2 = execution_result.get("take_profit_2")
        try:
            entry = float(entry) if entry is not None else None
            stop_loss = float(stop_loss) if stop_loss is not None else None
            tp1 = float(tp1) if tp1 is not None else None
            tp2 = float(tp2) if tp2 is not None else None
        except (TypeError, ValueError):
            return {"ok": False, "reason": "unparseable levels from generated code"}

        # The real market fill. Live reads the trigger bar's close; the backtest
        # additionally clamps into [low, high] so a bar-close outside its own
        # range (malformed data) can never become a fill price.
        try:
            bar_low = float(trigger_candle.get("low"))
            bar_high = float(trigger_candle.get("high"))
            bar_close = float(trigger_candle.get("close"))
        except (TypeError, ValueError):
            return {"ok": False, "reason": "trigger candle missing OHLC"}
        trigger_price = min(bar_high, max(bar_low, bar_close))

        gated = self._apply_execution_gates(
            direction, entry, stop_loss, tp1, tp2, trigger_price,
            sl_anchor=(signal_dict.get("metadata") or {}).get("sl_anchor"),
        )
        if not gated["ok"]:
            return gated

        quantity, size_reason = self._size_trade(gated["entry"], gated["stop"])
        if size_reason:
            return {"ok": False, "reason": size_reason}

        gated["quantity"] = quantity
        return gated

    def _size_trade(self, entry: float, stop_loss: float) -> tuple[float, Optional[str]]:
        """Size against the account, or return the live skip reason.

        With ``use_risk_sizing`` off this returns the fixed ``lot_size`` and the
        LP-01 gate never fires — a price-R study, not a P&L study.
        """
        if not self.use_risk_sizing:
            return self.lot_size, None
        sizing = size_position(self.asset, self.balance, entry, stop_loss, self.risk_pct)
        if not sizing.tradeable:
            return 0.0, (
                f"Position sizing skipped ({sizing.reason}): implied risk "
                f"${sizing.implied_risk:.2f} vs budget ${sizing.intended_risk:.2f} "
                f"({sizing.overshoot:.2f}x) at balance ${self.balance:.2f}"
            )
        return sizing.lot, None

    def _get_hourly_checkpoints(self, start: pd.Timestamp, end: pd.Timestamp, interval_hours: int) -> List[pd.Timestamp]:
        """Get hourly checkpoints within the date range at specified intervals.

        Generates checkpoints every ``interval_hours`` from ``start`` to ``end``.
        Weekend / maintenance gaps are handled by the candle fetch + session
        layer, not here, so this is a simple linear walk for all MT5 assets.
        """
        checkpoints = []
        current = start.replace(second=0, microsecond=0)
        while current <= end:
            checkpoints.append(current)
            current += pd.Timedelta(hours=interval_hours)
        return checkpoints
    
    def _filter_consecutive_5m_candles(
        self,
        candles: List[Dict[str, Any]],
        reference_time: datetime | pd.Timestamp | None = None
    ) -> List[Dict[str, Any]]:
        """Filter candles to only include those with strict consecutive 5-minute intervals.
        
        This ensures that monitoring candles have exactly 5-minute gaps, skipping any
        candles with irregular intervals that would fail validation.
        
        Parameters
        ----------
        candles : List[Dict[str, Any]]
            List of candle dictionaries with 'time' field
        reference_time : datetime | pd.Timestamp | None
            Optional reference time to start filtering from. If None, uses first candle.
            
        Returns
        -------
        List[Dict[str, Any]]
            Filtered list of candles with strict consecutive 5-minute intervals
        """
        if not candles:
            return []
        
        # Use the helper function from candles module
        filtered = filter_consecutive_candles(candles, interval="5m", start_from_first=True, asset=self.asset)
        logger.info(
            "[TIMESTAMP-VALIDATION] Filtered consecutive 5m candles",
            extra={
                "total_candles_before": len(candles),
                "total_candles_after": len(filtered),
                "first_candle_time": candles[0].get('time') if candles else None,
                "last_candle_time": candles[-1].get('time') if candles else None,
                "validation_context": "filter_consecutive_5m"
            }
        )
        
        # If reference_time provided, filter to only include candles after reference
        if reference_time is not None:
            ref_ts = pd.Timestamp(reference_time)
            filtered = [c for c in filtered if pd.Timestamp(c.get('time')) > ref_ts]
            logger.info(
                "[TIMESTAMP-VALIDATION] Applied reference time filter to 5m candles",
                extra={
                    "reference_time": ref_ts.isoformat(),
                    "remaining_candles": len(filtered),
                    "validation_context": "filter_consecutive_5m_reference"
                }
            )
        
        return filtered
    
    async def _execute_iteration(
        self,
        timestamp: pd.Timestamp,
        backtest_id: str,
        iteration_num: int,
        cached_status: str | None = None
    ) -> IterationDict | Dict[str, Any]:
        """Execute a single backtest iteration at the given timestamp."""
        try:
            active_code_monitoring_local = None  # Declared here so it's always defined
            # Initialise up-front so locals() checks are never needed (issue #7)
            execution_result = None
            cycle_doc_dict = None
            cycle_id = None
            # Convert pandas Timestamp to datetime
            if isinstance(timestamp, pd.Timestamp):
                timestamp_dt = timestamp.to_pydatetime()
            else:
                timestamp_dt = timestamp
            
            # Ensure timezone-aware
            if timestamp_dt.tzinfo is None:
                timestamp_dt = timestamp_dt.replace(tzinfo=timezone.utc)
            
            # Use pre-fetched cached candles instead of fetching again
            # Filter candles up to the checkpoint timestamp
            if self._cached_5m_candles is None or self._cached_15m_candles is None:
                raise ValueError("Pre-fetched candles not available. Ensure execute_backtest() was called first.")
            
            # Use bisect for O(log n) slice instead of O(n) linear filter
            checkpoint_ts = pd.Timestamp(timestamp)
            
            # CSV timestamps are bar OPENS, just like the live MT5 feed.
            # At 12:00 the newest available M5/M15 bars open at 11:55/11:45.
            idx_5m = bisect.bisect_right(self._sorted_5m_times, checkpoint_ts - BAR_DURATION)
            start_5m = max(0, idx_5m - self.candle_count_5m)
            candles_5m_raw = self._cached_5m_candles[start_5m:idx_5m]
            
            idx_15m = bisect.bisect_right(self._sorted_15m_times, checkpoint_ts - pd.Timedelta(minutes=15))
            start_15m = max(0, idx_15m - self.candle_count_15m)
            candles_15m_raw = self._cached_15m_candles[start_15m:idx_15m]
            
            # Check if we have enough candles
            if len(candles_5m_raw) < self.candle_count_5m:
                logger.warning(
                    f"Insufficient 5m candles at {timestamp}: "
                    f"need {self.candle_count_5m}, have {len(candles_5m_raw)}"
                )
                return {
                    "iteration": iteration_num,
                    "timestamp": timestamp.isoformat(),
                    "status": "skipped",
                    "error": f"Insufficient 5m candle history: need {self.candle_count_5m}, have {len(candles_5m_raw)}"
                }
            
            if len(candles_15m_raw) < self.candle_count_15m:
                logger.warning(
                    f"Insufficient 15m candles at {timestamp}: "
                    f"need {self.candle_count_15m}, have {len(candles_15m_raw)}"
                )
                return {
                    "iteration": iteration_num,
                    "timestamp": timestamp.isoformat(),
                    "status": "skipped",
                    "error": f"Insufficient 15m candle history: need {self.candle_count_15m}, have {len(candles_15m_raw)}"
                }
            
            # Ensure types are correct before passing to multi_expert
            # This ensures format matches live trading exactly
            # Normalize types to datetime objects (not pd.Timestamp)
            candles_5m = normalize_candle_list_types(candles_5m_raw)
            candles_15m = normalize_candle_list_types(candles_15m_raw)
            
            # Compare availability times, not the open timestamps of different
            # timeframes. A completed M15 bar at 11:45 contains M5 bars opening
            # 11:45/11:50/11:55. At other M5 checkpoints M15 may lag by 10min.
            if candles_5m and candles_15m:
                last_5m_close = pd.Timestamp(candles_5m[-1]["time"]) + BAR_DURATION
                last_15m_close = pd.Timestamp(candles_15m[-1]["time"]) + pd.Timedelta(minutes=15)
                if abs(last_5m_close - last_15m_close) > pd.Timedelta(minutes=10):
                    logger.warning(
                        f"5m/15m availability misalignment at checkpoint {timestamp}: "
                        f"last_5m_close={last_5m_close}, last_15m_close={last_15m_close}"
                    )

            # Validate types (log warning if invalid but continue)
            is_valid_5m, error_msg_5m = validate_candle_list_types(candles_5m)
            if not is_valid_5m:
                logger.warning(f"Type validation warning for 5m candles before multi_expert: {error_msg_5m}")
            
            is_valid_15m, error_msg_15m = validate_candle_list_types(candles_15m)
            if not is_valid_15m:
                logger.warning(f"Type validation warning for 15m candles before multi_expert: {error_msg_15m}")
            
            # Get prediction
            if self.use_code_sniper:
                # Use multi-expert code sniper mode
                # Use already-normalised candles directly — no DataFrame/JSON roundtrip needed
                cycle_id = str(uuid4())
                try:
                    # candles_5m/candles_15m are already normalised lists of dicts
                    c5_candles = candles_5m
                    c15_candles = candles_15m
                    
                    logger.info(
                        f"[BACKTEST-ITERATION {iteration_num}] Calling multi_expert_decision_with_details",
                        extra={
                            "backtest_id": backtest_id,
                            "iteration": iteration_num,
                            "cycle_id": cycle_id,
                            "candles_5m_count": len(c5_candles),
                            "candles_15m_count": len(c15_candles)
                        }
                    )
                    
                    # Check if backtest has been stopped before making GPT call (use cached status)
                    backtest_status = cached_status if cached_status is not None else "running"
                    if backtest_status not in ["running", "pending"]:
                        logger.info(
                            f"[BACKTEST-ITERATION {iteration_num}] Backtest stopped before GPT call (status: {backtest_status})",
                            extra={"backtest_id": backtest_id, "status": backtest_status, "iteration": iteration_num}
                        )
                        return {
                            "iteration": iteration_num,
                            "timestamp": timestamp.isoformat(),
                            "status": "skipped",
                            "error": f"Backtest stopped (status: {backtest_status})"
                        }
                    
                    # ── Live-parity cycle context ────────────────────────
                    # The live runner fetches 1h/4h/1d from the broker and puts
                    # them in gpt_cfg BEFORE calling the judge; without them the
                    # whole HTF layer is inert (see _build_htf_candles). Build
                    # them from the 5m cache as of this checkpoint, then derive
                    # the same cycle meta the live executor consumes.
                    htf_candles = self._build_htf_candles(checkpoint_ts)
                    self._cycle_context = self._compute_cycle_context(
                        htf_candles, c5_candles, timestamp_dt
                    )
                    session_tape = self._observe_session_tape(
                        c5_candles, self._cycle_context.get("htf_trend")
                    )

                    signal, cycle_doc = await multi_expert_decision_with_details(
                        c5_superset=c5_candles,
                        c15_superset=c15_candles,
                        gpt_cfg={
                            **self.gpt_config,
                            "asset": self.asset,
                            "store_response": True,
                            "agent_id": f"backtest_{backtest_id}",
                            "htf_candles": htf_candles,
                            "as_of": timestamp_dt,
                            "recent_trades": self._operational_ledger.recent_views(self._ledger_account, self.asset, timestamp_dt),
                            # None unless SESSION_TAPE mode is "context" for
                            # this asset — same contract as the live runner.
                            "session_tape": session_tape,
                            "prompt_hash": hashlib.sha256(
                                f"{backtest_id}:{iteration_num}:{cycle_id}".encode()
                            ).hexdigest()[:16],
                        },
                        cycle_id=cycle_id,
                        use_code_sniper=True,
                    )
                    
                    # Check again after GPT call - if stopped during call, skip processing
                    # Use cached status (refreshed every N iterations in execute_backtest)
                    if backtest_status not in ["running", "pending"]:
                        logger.info(
                            f"[BACKTEST-ITERATION {iteration_num}] Backtest stopped during GPT call (status: {backtest_status}), skipping result",
                            extra={"backtest_id": backtest_id, "status": backtest_status, "iteration": iteration_num}
                        )
                        return {
                            "iteration": iteration_num,
                            "timestamp": timestamp.isoformat(),
                            "status": "skipped",
                            "error": f"Backtest stopped during GPT call (status: {backtest_status})"
                        }
                    
                    logger.info(
                        f"[BACKTEST-ITERATION {iteration_num}] Multi-expert decision received",
                        extra={"backtest_id": backtest_id, "iteration": iteration_num, "has_signal": signal is not None}
                    )
                    
                    # Handle flat decision (judge decided no trade setup)
                    if signal is None:
                        logger.info(
                            f"[BACKTEST-ITERATION {iteration_num}] Judge decided flat - skipping iteration",
                            extra={"backtest_id": backtest_id, "iteration": iteration_num}
                        )
                        flat_iteration = {
                            "iteration": iteration_num,
                            "iteration_timestamp": timestamp,
                            "prediction": {"direction": None, "entry": None, "stop": None, "target": None, "pattern": "flat", "status": "flat"},
                            "trade_outcome": {"direction": None, "entry": None, "stop": None, "target": None, "profit": 0.0, "result": "Flat"},
                            "status": "completed"
                        }
                        try:
                            if hasattr(backtest_repo, 'add_iteration'):
                                await backtest_repo.add_iteration(backtest_id=backtest_id, iteration_data=flat_iteration)
                        except Exception as e:
                            logger.warning(f"Could not save flat iteration: {e}")
                        return flat_iteration
                    
                    # Convert cycle_doc to dict for storage (if it's a Pydantic model)
                    if hasattr(cycle_doc, 'model_dump'):
                        cycle_doc_dict = cycle_doc.model_dump()
                    elif hasattr(cycle_doc, 'dict'):
                        cycle_doc_dict = cycle_doc.dict()
                    else:
                        cycle_doc_dict = dict(cycle_doc) if cycle_doc else {}
                    
                    # Extract prediction from signal (aligned with live flow)
                    # Handle both MultiExpertSignal and CodeSniperSignal
                    if hasattr(signal, 'model_dump'):
                        signal_dict = signal.model_dump()
                    elif hasattr(signal, 'dict'):
                        signal_dict = signal.dict()
                    else:
                        signal_dict = {}
                    
                    # Check if this is a CodeSniperSignal (contains code)
                    generated_code = signal_dict.get('code') or getattr(signal, 'code', None)
                    execution_result = {}
                    
                    if generated_code:
                        # Handle CodeSniperSignal - execute code continuously on each 5m candle until TTL or trade triggers
                        logger.info(f"Executing generated code for iteration {iteration_num} - will monitor until TTL or trade triggers")
                        
                        # TTL from the signal, snapped to the legal enum. The
                        # old literal default of 60 stopped being a valid TTL on
                        # 2026-08-06 (TTL_MINUTES_ALLOWED = 90/180/240), so a
                        # signal that fell back to it armed a monitoring window
                        # the live system can never produce.
                        ttl_minutes = coerce_ttl(
                            _first_not_none(signal_dict.get('ttl_minutes'), signal_dict.get('ttl')),
                            _TTL_DEFAULT_MINUTES,
                        )

                        
                        # ── Arm the strategy. Do not look for its trigger. ──
                        # This is the cycle clock's whole job: the model has
                        # produced a strategy and a TTL, so the strategy is armed
                        # and the iteration ends.
                        #
                        # The trigger is checked by the bar clock
                        # (_advance_closed_bars_to), once per bar, as each bar
                        # closes. This used to scan `ttl_minutes // 5` bars
                        # forward here — up to 48 bars, on an hourly loop — which
                        # is how a position could be opened AND resolved inside
                        # the iteration that created it, hours before the clock
                        # said those bars existed.
                        ttl_expiry = timestamp_dt + timedelta(minutes=ttl_minutes)
                        logger.info(
                            f"[BACKTEST-RUNNER] Strategy armed at {timestamp_dt.isoformat()} "
                            f"(TTL {ttl_minutes}min, expires {ttl_expiry.isoformat()})",
                            extra={
                                "backtest_id": backtest_id,
                                "iteration": iteration_num,
                                "ttl_minutes": ttl_minutes,
                                "ttl_expiry": ttl_expiry.isoformat(),
                            },
                        )
                        active_code_monitoring_local = {
                            "code": generated_code,
                            "ttl_expiry": ttl_expiry,
                            "start_time": timestamp_dt,
                            "signal_dict": signal_dict,
                            # Freeze the cycle's context with the strategy: when
                            # this window triggers hours later, the sl_anchor pad
                            # and the touch-fill's trend agreement must be the
                            # ones the judge reasoned with, exactly as the live
                            # strategy record carries its own `context`.
                            "cycle_context": dict(self._cycle_context or {}),
                        }
                        pred = {
                            "direction": None,
                            "entry": None,
                            "stop": None,
                            "target": None,
                            "pattern": "code_sniper",
                            "status": "monitoring",
                            "code_executed": True,
                            "ttl_expired": False,
                            "ttl_expiry": ttl_expiry.isoformat(),
                        }
                    else:
                        # Handle regular MultiExpertSignal
                        # MultiExpertSignal has 'signal' (buy/sell/flat), NOT 'direction' (long/short)
                        # Map: buy -> long, sell -> short
                        raw_signal = getattr(signal, 'signal', None) or signal_dict.get('signal')
                        direction_map = {"buy": "long", "sell": "short"}
                        mapped_direction = direction_map.get(raw_signal)
                        
                        if raw_signal == "flat":
                            pred = {
                                "direction": None,
                                "entry": None,
                                "stop": None,
                                "target": None,
                                "pattern": "flat",
                                "confidence": getattr(signal, 'confidence', None),
                                "status": "flat",
                            }
                        elif mapped_direction:
                            entry_val = getattr(signal, 'entry', None) or signal_dict.get('entry')
                            pred = {
                                "direction": mapped_direction,
                                "entry": entry_val,
                                "stop": getattr(signal, 'stop', None),
                                "target": getattr(signal, 'target', None),
                                "pattern": "code_sniper",
                                "confidence": getattr(signal, 'confidence', None),
                                "status": "waiting",
                            }
                        else:
                            logger.warning(f"Unknown signal type from MultiExpertSignal: {raw_signal}")
                            pred = {}
                except Exception as e:
                    logger.error(f"Multi-expert decision failed at {timestamp}: {e}")
                    return {
                        "iteration": iteration_num,
                        "timestamp": timestamp.isoformat(),
                        "status": "error",
                        "error": str(e)
                    }
            # ── No simulation here. ─────────────────────────────────
            # A cycle produces a decision; it does not produce an outcome. This
            # method used to fetch BT_MONITOR_CANDLES_5M + 12 bars — five hours
            # of them, on an hourly loop — and run `simulate_trade` to completion
            # against the future, which is what let the runner report a closed
            # trade at the same checkpoint that opened it and then immediately
            # call the model again for the next one.
            #
            # A position is opened by the bar clock, at the bar its trigger
            # fires, and it is carried by PositionMonitor until a bar closes it.
            # The row recorded here says only what the cycle decided.
            outcome = {
                "direction": pred.get("direction"),
                "entry": pred.get("entry"),
                "stop": pred.get("stop"),
                "target": pred.get("target"),
                "profit": 0.0,
                # "No Entry" either way: the cycle opened nothing. Whether a
                # strategy is now armed is carried by pred["status"], exactly as
                # before — an outcome row must never imply a position.
                "result": "No Entry",
            }


            # Prepare iteration data with cycle info (if available)
            iteration_result = {
                "iteration": iteration_num,
                "iteration_timestamp": timestamp,
                "signal_data": signal_dict if self.use_code_sniper else {},
                "prediction": pred,
                "trade_outcome": outcome,
                "status": "completed",
                # "monitoring" = a strategy is armed and waiting for its trigger
                # bar; "none" = the cycle produced nothing to watch.
                "trade_status": (
                    "monitoring" if pred.get("status") == "monitoring" else "none"
                ),
                "monitoring_ttl_expiry": pred.get("ttl_expiry"),
            }
            
            # Add execution result if code was executed
            if execution_result:
                iteration_result["execution_result"] = execution_result
            
            # Add cycle data if multi-expert was used
            if self.use_code_sniper and cycle_doc_dict is not None:
                iteration_result["cycle_id"] = cycle_id
                iteration_result["cycle_data"] = cycle_doc_dict
                iteration_result["ai_cost"] = cycle_doc_dict.get("ai_cost")
                iteration_result["trade_reason"] = trade_reason(signal_dict)
            
            # Normalize iteration data before storing (standardizes field names)
            try:
                normalized_iteration = normalize_iteration_data(iteration_result)
                trade_closed_at = (
                    normalized_iteration.get("trade_outcome", {}).get("trade_closed_at")
                    if isinstance(normalized_iteration.get("trade_outcome"), dict)
                    else None
                )
                if trade_closed_at is not None:
                    logger.info(
                        "[TIMESTAMP-VALIDATION] trade_closed_at mapped from exit_time",
                        extra={
                            "backtest_id": backtest_id,
                            "iteration": iteration_num,
                            "trade_closed_at": trade_closed_at.isoformat() if hasattr(trade_closed_at, "isoformat") else str(trade_closed_at),
                            "validation_context": "trade_closed_at_mapping"
                        }
                    )
            except Exception as norm_error:
                logger.error(
                    f"Failed to normalize iteration data: {norm_error}",
                    extra={
                        "backtest_id": backtest_id,
                        "iteration": iteration_num,
                        "error_type": type(norm_error).__name__
                    },
                    exc_info=True
                )
                # Continue with original data if normalization fails
                normalized_iteration = iteration_result
            
            # Return result with ISO format timestamp for JSON serialization.
            # Include normalized_iteration so execute_backtest can batch-write to DB.
            result = {
                "iteration": iteration_num,
                "timestamp": timestamp.isoformat(),
                "prediction": pred,
                "outcome": outcome,
                "cycle_id": cycle_id if self.use_code_sniper else None,
                "status": "completed",
                "_normalized_for_db": normalized_iteration,
            }
            
            # Include active_code_monitoring state if it was set (for parent to track)
            if active_code_monitoring_local:
                result["active_code_monitoring"] = active_code_monitoring_local
            
            return result
            
        except Exception as e:
            logger.error(f"Error executing iteration at {timestamp}: {e}")
            return {
                "iteration": iteration_num,
                "timestamp": timestamp.isoformat(),
                "status": "error",
                "error": str(e)
            }
    
    # ── Bar clock: the live order of operations ──────────────────────────
    # Live never "runs an iteration". Two clocks drive it: a CLOSED 5m bar wakes
    # the monitor, and the cycle interval wakes the model. The four methods below
    # are the bar clock; execute_backtest's checkpoint loop is the cycle clock.
    #
    # Separating them is what removed the look-ahead. A bar is processed only
    # once it has closed, and the model is called only at a checkpoint where
    # nothing is open and nothing is armed — which is precisely the two rules the
    # live runner enforces with `active_trade` and `active_code_monitoring`.

    def _strategy_context_before(self, bar_index: int) -> Optional[List[Dict[str, Any]]]:
        """The consecutive 5m window the strategy code is executed against.

        Returns None when the cached series has a hole the session calendar does
        not explain: feeding discontinuous history to generated code produces a
        trigger on price action that never happened.
        """
        window = self._bar_feed.history_before_index(bar_index, _HISTORICAL_WINDOW)
        if window is None:
            return None
        previous_time = None
        for candle in window:
            candle_time = pd.Timestamp(candle.get("time"))
            if (
                previous_time is not None
                and candle_time - previous_time != timedelta(minutes=5)
                and not is_expected_market_gap(previous_time, candle_time, self.asset)
            ):
                logger.error(
                    f"[BAR-CLOCK] {self.asset} non-consecutive 5m history at {candle_time} "
                    f"(previous {previous_time}) — cannot build strategy context"
                )
                return None
            previous_time = candle_time
        return [normalize_candle_types(candle) for candle in window]

    def _run_armed_strategy_on_bar(
        self,
        armed: ArmedStrategy,
        bar: Dict[str, Any],
        bar_index: int,
    ) -> Optional[Dict[str, Any]]:
        """Execute the armed strategy against one closed bar.

        Returns the execution result when the strategy asks for a fill, else
        None. The backtest twin of CodeSniperMonitor's per-bar execution,
        including the aligned touch-fill promotion the live executor applies to a
        'hold' whose judged entry that bar touched.
        """
        history = self._strategy_context_before(bar_index)
        if history is None:
            return None

        # The touch-fill promotion below reads trend_agreement off the cycle
        # context, so it must be the reading the JUDGE was given when this
        # strategy was armed — not whatever a later checkpoint has since
        # computed for an unrelated cycle.
        self._cycle_context = armed.cycle_context or self._cycle_context

        current_candle = normalize_candle_types(bar)
        try:
            if armed.signal.get("metadata", {}).get("typed_setup"):
                from .typed_setup import evaluate_observed_setup
                result = evaluate_observed_setup(armed.signal["metadata"], history, current_candle, armed.cycle_context)
                if result.get("typed_status") in ("invalidated", "expired", "missed_trigger"):
                    armed.ttl_expiry = _as_utc_datetime(bar_close_time(bar))
            else:
                result = execute_generated_code(code=armed.code, current_candle=current_candle,
                    historical_candles=history, backtest_mode=True, asset=self.asset)
        except Exception as exc:
            if armed.register_code_failure():
                raise ValueError(
                    f"Generated code failed {armed.consecutive_code_failures} consecutive "
                    f"times (last error: {exc}). Code is invalid — abandoning strategy."
                ) from exc
            logger.warning(
                f"[BAR-CLOCK] {self.asset} strategy code failed on "
                f"{bar.get('time')} ({armed.consecutive_code_failures}/"
                f"{MAX_CONSECUTIVE_CODE_FAILURES}): {exc}"
            )
            return None
        armed.register_code_success()

        direction = (result.get("direction") or "hold").lower()
        if direction == "hold":
            if armed.signal.get("metadata", {}).get("typed_setup"):
                return None
            promoted = self._aligned_touch_fill(
                entry=result.get("entry"),
                stop_loss=_first_not_none(result.get("stop_loss"), result.get("stop")),
                take_profit_1=_first_not_none(result.get("take_profit_1"), result.get("target")),
                trigger_candle=current_candle,
            )
            if not promoted:
                return None
            direction = promoted
            result["rationale"] = f"[ALIGNED-TOUCH-FILL] {result.get('rationale', '')}"

        if direction not in ("long", "short"):
            return None

        result["direction"] = direction
        result["triggered_candle_time"] = bar_close_time(bar)
        return result

    def _open_position_from_trigger(
        self,
        armed: ArmedStrategy,
        trigger_result: Dict[str, Any],
        bar: Dict[str, Any],
        *,
        entry_iteration: int,
        refuse_signature: Optional[tuple],
    ) -> tuple[Optional[OpenPosition], Optional[str]]:
        """The ONE place a position is ever opened. Returns (position, rejection).

        Consolidating the two former trigger sites is what makes their gate
        chains identical: the in-iteration site applied an SL-inside-trigger-bar
        check the cross-checkpoint site did not, so the same setup could be
        rejected or taken depending only on which checkpoint it fired on.
        """
        entry_time = _as_utc_datetime(bar_close_time(bar))
        direction = trigger_result["direction"]

        # The judge's own cycle context decided these levels; a later
        # checkpoint's reading must not be substituted for it.
        self._cycle_context = armed.cycle_context or self._cycle_context

        executed = self._execute_triggered_setup(
            direction, trigger_result, armed.signal, bar, timestamp_dt=entry_time,
        )
        if not executed["ok"]:
            return None, executed["reason"]

        entry = executed["entry"]
        stop_loss = executed["stop"]

        # Entry is after this candle closes. Its earlier wick cannot stop a
        # position that did not yet exist. Final directional level validation
        # still requires the stop to be on the loss side of the actual entry.
        signature = position_signature(entry, stop_loss, direction)
        if refuse_signature is not None and signature == refuse_signature:
            return None, "identical to the trade that just closed — re-entry refused"

        quantity = executed["quantity"]
        atr_1h = (self._cycle_context or {}).get("atr_1h")
        trade_state = {
            "direction": direction,
            "entry": entry,
            "sl": stop_loss,
            "initial_stop": stop_loss,
            "sl_moved": False,
            "tp1_hit": False,
            "tp2_hit": False,
            "quantity": quantity,
            "asset": self.asset,
        }
        prediction = {
            "direction": direction,
            "entry": entry,
            "stop": stop_loss,
            "target": executed["tp2"],
            "tp1": executed["tp1"],
            "tp2": executed["tp2"],
            "quantity": quantity,
            "atr_1h": atr_1h,
            "pattern": "code_sniper",
            "confidence": trigger_result.get("confidence") or armed.signal.get("confidence"),
            "status": "waiting",
            "code_executed": True,
            "entry_timestamp": entry_time.isoformat(),
            "trade_reason": trade_reason(armed.signal),
            "trigger_reason": trigger_result.get("rationale"),
            "ai_cost": armed.signal.get("metadata", {}).get("ai_cost"),
        }
        from .exit_policy import family_exit_rules
        setup = armed.signal.get("metadata", {}).get("typed_setup", {})
        position = OpenPosition(
            asset=self.asset,
            trade_state=trade_state,
            rules=family_exit_rules(setup.get("family"), build_rules(
                executed["tp1"], executed["tp2"],
                entry=entry, stop_loss=stop_loss, atr_1h=atr_1h,
            ), max_age_minutes=setup.get("max_position_age_minutes")),
            quantity=quantity,
            prediction=prediction,
            entry_time=entry_time,
            entry_iteration=entry_iteration,
            expiry=entry_time + timedelta(minutes=setup.get("max_position_age_minutes", _TRADE_TIMEOUT_MINUTES)),
            # The position opened at THIS bar's close, so the ladder resumes on
            # the next one — live's monitor starts counting after the fill.
            last_evaluated_bar_time=entry_time,
        )
        self._operational_ledger.record_simulation(self._ledger_account, position)
        from packages.db.persistence_gate import log_event
        log_event("backtest_trade_open", {
            "asset": self.asset, "prediction": prediction,
            "ai_cost": prediction.get("ai_cost"), "trade_reason": prediction.get("trade_reason"),
            "trigger_reason": prediction.get("trigger_reason"),
            "broker_cost": unrealised_snapshot(position, entry).get("broker_cost"),
        }, asset=self.asset, account=self._ledger_account)
        return position, None

    def _advance_closed_bars_to(
        self,
        now: datetime,
        *,
        open_position: Optional[OpenPosition],
        armed_strategy: Optional[ArmedStrategy],
        last_processed_bar_time: Optional[datetime],
        entry_iteration: int,
        refuse_signature: Optional[tuple],
    ) -> BarAdvance:
        """Replay every bar that closed since the last checkpoint, in order.

        One bar, one decision, and only ever the decision live would have made
        with the same information:

        * a position is open  -> advance it; a bar that closes it does not also
          arm or fill anything else, because live's monitor and its cycle are
          different clocks;
        * a strategy is armed -> execute it; a fill consumes the strategy whether
          or not the gates then accept it, exactly as the live executor does;
        * neither             -> nothing happens until the next checkpoint.
        """
        advance = BarAdvance(
            open_position=open_position,
            armed_strategy=armed_strategy,
            last_processed_bar_time=last_processed_bar_time,
            refuse_signature=refuse_signature,
        )

        if last_processed_bar_time is None or (open_position is None and armed_strategy is None):
            # Nothing to watch: the first checkpoint, or a slot that is simply
            # idle. There is no per-bar decision to make, so the clock moves to
            # the checkpoint in one step instead of walking bars that cannot
            # change anything.
            newest = self._bar_feed.last_bar_closed_at_or_before(now)
            if newest is not None:
                advance.last_processed_bar_time = _as_utc_datetime(bar_close_time(newest))
            return advance

        for bar in self._bar_feed.bars_closed_between(last_processed_bar_time, now):
            bar_time = _as_utc_datetime(bar_close_time(bar))
            advance.last_processed_bar_time = bar_time

            if advance.open_position is not None:
                closed = self._position_monitor.step(advance.open_position, bar)
                self._operational_ledger.record_simulation(self._ledger_account, advance.open_position)
                if closed is not None:
                    advance.closes.append(closed)
                    advance.refuse_signature = advance.open_position.signature
                    advance.open_position = None
                continue

            if advance.armed_strategy is None:
                continue

            if advance.armed_strategy.has_expired_at(bar_time):
                logger.info(
                    f"[BAR-CLOCK] {self.asset} strategy TTL expired at "
                    f"{advance.armed_strategy.ttl_expiry.isoformat()} without a trigger"
                )
                advance.armed_strategy = None
                continue

            bar_index = self._bar_feed.index_of_bar_at(bar.get("time"))
            if bar_index is None:
                continue

            try:
                trigger = self._run_armed_strategy_on_bar(advance.armed_strategy, bar, bar_index)
            except ValueError as exc:
                logger.error(f"[BAR-CLOCK] {exc}")
                advance.armed_strategy = None
                continue
            if trigger is None:
                continue

            armed = advance.armed_strategy
            position, rejection = self._open_position_from_trigger(
                armed, trigger, bar,
                entry_iteration=entry_iteration,
                refuse_signature=advance.refuse_signature,
            )
            # A fired strategy is consumed either way: live's monitor clears the
            # armed window the moment the code returns a direction, whether the
            # order is then accepted or rejected by a gate.
            advance.armed_strategy = None
            if position is None:
                from packages.db.persistence_gate import log_event

                reason = rejection or "rejected"
                advance.rejections.append(reason)
                log_event("backtest_trade_rejected", {
                    "reason": reason,
                    "trigger_time": _as_utc_datetime(bar_close_time(bar)).isoformat(),
                    "direction": trigger["direction"],
                    "armed_iteration": armed.armed_at_iteration,
                    "iteration": entry_iteration,
                }, asset=self.asset, account=self._ledger_account)
                continue

            advance.open_position = position
            advance.opened.append(position)
            advance.refuse_signature = None

        return advance

    async def execute_backtest(
        self,
        test_name: str,
        interval_hours: int = 1,
        backtest_id: str | None = None
    ) -> None:
        """Execute the backtest across the date range.
        
        Parameters
        ----------
        test_name : str
            Name for this backtest run
        interval_hours : int
            Hours between iterations (default: 1)
        backtest_id : str, optional
            Existing backtest ID to update. If None, test_name will be used as identifier.
        """
        # Use test_name as backtest_id if not provided
        if backtest_id is None:
            backtest_id = test_name
        # A restarted/reused runner begins a new isolated operational episode.
        self._ledger_account = f"replay:{backtest_id}:{uuid4()}"
        
        # STEP 2: Pre-fetch full date range candles once (already fetched from MT5)
        # Filter by backtest_id if available
        logger.info(
            f"[BACKTEST-RUNNER] Pre-fetching full date range candles for backtest {backtest_id}",
            extra={"backtest_id": backtest_id, "asset": self.asset}
        )
        
        try:
            # ── Warm-up lookback ─────────────────────────────────────────
            # The fetch used to start exactly at start_date, so the first
            # checkpoints had no history and were skipped for "insufficient 5m
            # candles" — and the resampled 1h/4h/1d series stayed shallow for
            # days, silently degrading the HTF context the judge is given.
            # Live never starts cold: it always reads 120×1h / 90×4h / 60×1d
            # from the broker. Reach back far enough to serve the deepest of
            # those (plus the 5m/15m windows) BEFORE the first checkpoint.
            counts = resolve_for_asset(HTF_CANDLE_COUNTS_BY_ASSET, self.asset, None) or HTF_CANDLE_COUNTS
            warmup_days = max(
                int(counts.get("1d", 0) or 0),
                int(counts.get("4h", 0) or 0) * 4 // 24 + 1,
                int(counts.get("1h", 0) or 0) // 24 + 1,
                # 5m/15m prompt windows, generously rounded to whole days.
                (max(self.candle_count_5m * 5, self.candle_count_15m * 15) // (60 * 24)) + 1,
            ) if HTF_CONTEXT_ENABLED else 2
            # Calendar days, so weekends/holidays inside the window don't eat
            # into the bar count for the 5-day-week instruments.
            fetch_start = self.start_date - timedelta(days=int(warmup_days * 1.6) + 3)
            logger.info(
                f"[BACKTEST-RUNNER] Warm-up lookback: fetching from {fetch_start.isoformat()} "
                f"({(self.start_date - fetch_start).days}d before start) so the first checkpoint "
                f"has the same 5m/15m/HTF depth live always has"
            )

            # limit=0 fetches ALL candles in the date range — the CSV loader
            # already filters by start/end so no artificial cap is needed.
            self._cached_5m_candles = await self.candle_service.fetch_historical_5m(
                asset=self.asset,
                start_date=fetch_start,
                end_date=self.end_date,
                limit=0
            )

            self._cached_15m_candles = await self.candle_service.fetch_historical_15m(
                asset=self.asset,
                start_date=fetch_start,
                end_date=self.end_date,
                limit=0
            )

            logger.info(
                f"[BACKTEST-RUNNER] Pre-fetched {len(self._cached_5m_candles)} 5m and {len(self._cached_15m_candles)} 15m candles",
                extra={"backtest_id": backtest_id}
            )
            
            # Build O(1) lookup maps for candle indexing
            self._candle_5m_index_map = {
                pd.Timestamp(c.get('time')): idx 
                for idx, c in enumerate(self._cached_5m_candles)
            }
            self._candle_15m_index_map = {
                pd.Timestamp(c.get('time')): idx 
                for idx, c in enumerate(self._cached_15m_candles)
            }
            # Pre-sorted lists for O(log n) bisect-based nearest-candle lookups
            self._sorted_5m_times = sorted(self._candle_5m_index_map.keys())
            self._sorted_15m_times = sorted(self._candle_15m_index_map.keys())
            # Index the 5m series for HTF resampling (live's broker HTF fetch).
            self._build_htf_source()

            # The bar clock. Every 5m read from here on goes through the feed,
            # which cannot return a bar that had not closed at the instant it is
            # asked about — that is what makes look-ahead structurally
            # impossible rather than a rule each call site has to remember.
            self._bar_feed = ClosedBarFeed(
                self._cached_5m_candles,
                self._sorted_5m_times,
                self._candle_5m_index_map,
            )
            self._position_monitor = PositionMonitor(_is_market_closing_soon)
        except Exception as e:
            logger.error(f"Failed to pre-fetch candles: {e}", exc_info=True)
            raise
        
        # Update status to running (if method exists)
        try:
            if hasattr(backtest_repo, 'update_backtest_status'):
                await backtest_repo.update_backtest_status(backtest_id, "running")
        except Exception as e:
            logger.warning(f"Could not update backtest status: {e}")
        
        # Convert datetime to pandas Timestamp for comparison
        start_ts = pd.Timestamp(self.start_date)
        end_ts = pd.Timestamp(self.end_date)
        
        # Get hourly checkpoints
        checkpoints = self._get_hourly_checkpoints(start_ts, end_ts, interval_hours)
        
        logger.info(
            f"[BACKTEST-RUNNER] Starting backtest '{test_name}': {len(checkpoints)} iterations "
            f"from {start_ts} to {end_ts} (interval: {interval_hours}h)",
            extra={
                "backtest_id": backtest_id,
                "test_name": test_name,
                "asset": self.asset,
                "total_iterations": len(checkpoints),
                "use_code_sniper": self.use_code_sniper
            }
        )
        
        iterations = []
        total_signals = 0
        wins = 0
        losses = 0
        total_pnl = 0.0
        # Peak-to-trough drawdown, reported as a POSITIVE dollar amount.
        # This used to be `min(cumulative_pnl)` — i.e. "the worst the running
        # total ever got, but only if it went below zero". A run that climbed to
        # +$400 and fell back to +$50 reported a $0.00 drawdown, and the
        # scorecard's "Max Drawdown < $2000" check therefore passed on runs that
        # had never been measured at all.
        max_drawdown = 0.0
        _equity_peak = 0.0
        cumulative_pnl = 0.0
        
        # ── Daily trade guards (cooldown + max trades/day) ───────────
        daily_guards = DailyGuards(
            max_loss=float("inf"),          # no daily loss limit for now
            max_trades=BACKTEST_MAX_TRADES_PER_DAY,
            cooldown=BACKTEST_COOLDOWN_AFTER_SL,
            loss_threshold=BACKTEST_COOLDOWN_SL_THRESHOLD,
        )
        _current_guard_day: Optional[str] = None   # track UTC date for resets
        # Day-start equity for the account drawdown halt (live parity).
        _day_start_equity = self.balance
        total_filtered_by_guards = 0
        
        # ── Execution state carried across checkpoints ───────────────
        # These three ARE the live runner's state: one position at most, one
        # armed strategy at most, and the bar the clock last advanced to. The
        # checkpoint loop only ever reads them; the bar clock owns the writes.
        open_position: Optional[OpenPosition] = None
        armed_strategy: Optional[ArmedStrategy] = None
        last_processed_bar_time: Optional[datetime] = None
        
        # P&L is contract-size based (backtest_account.usd_pnl) and the lot comes
        # from per-trade risk sizing, so the old pip-scaling locals are gone —
        # BACKTEST_INSTRUMENT_CONFIG now only supplies the fixed-lot fallback.
        _asset_key = (self.asset or "default").upper()
        _instr_cfg = BACKTEST_INSTRUMENT_CONFIG.get(_asset_key, BACKTEST_INSTRUMENT_CONFIG["default"])
        _lot = self.lot_size if self.lot_size is not None else _instr_cfg["default_lot"]

        logger.info(
            f"[BACKTEST-RUNNER] Account: starting balance ${self.initial_capital:,.2f}, "
            f"risk {self.risk_pct * 100:.2f}%/trade, "
            f"sizing={'risk-based (live parity)' if self.use_risk_sizing else f'fixed {_lot} lots'}"
        )

        # Duplicate/zombie prevention: the signature of the trade that closed
        # most recently, so an identical immediate re-entry is refused.
        last_closed_signature: Optional[tuple] = None
        
        # ── Consecutive GPT error tracking ───────────────────────────
        _consecutive_gpt_errors = 0
        _MAX_CONSECUTIVE_GPT_ERRORS = 3
        
        # Batch DB write buffer — flushed every _DB_BATCH_SIZE iterations
        _DB_BATCH_SIZE = 10
        _iteration_write_buffer: List[Dict[str, Any]] = []
        
        # Cache backtest status to reduce database calls
        # Initialize to "running" since we're starting execution - this prevents false "unknown" stops
        cached_backtest_status: str = "running"
        status_cache_ttl = 3  # Refresh status every 3 iterations for responsive stop
        
        # Perform initial status check before starting iterations
        try:
            backtest_doc = await backtest_repo.get_backtest(backtest_id)
            if backtest_doc:
                cached_backtest_status = backtest_doc.get("status", "running")
                logger.info(
                    f"[BACKTEST-RUNNER] Initial status check: {cached_backtest_status}",
                    extra={"backtest_id": backtest_id, "status": cached_backtest_status}
                )
            else:
                logger.warning(
                    f"[BACKTEST-RUNNER] Backtest document not found, assuming running status",
                    extra={"backtest_id": backtest_id}
                )
                cached_backtest_status = "running"
        except Exception as e:
            logger.warning(f"Could not check initial backtest status: {e}, assuming running")
            cached_backtest_status = "running"
        
        try:
            for i, checkpoint in enumerate(checkpoints, 1):
                # Check if backtest has been stopped before proceeding
                # Cache status check to reduce database calls (refresh every N iterations)
                if i % status_cache_ttl == 0:
                    try:
                        backtest_doc = await backtest_repo.get_backtest(backtest_id)
                        if backtest_doc:
                            cached_backtest_status = backtest_doc.get("status", "running")
                        else:
                            logger.warning(
                                f"[BACKTEST-RUNNER] Backtest document not found during status check, assuming running",
                                extra={"backtest_id": backtest_id, "iteration": i}
                            )
                            cached_backtest_status = "running"
                    except Exception as e:
                        # If we can't check status, log warning but continue with cached status
                        logger.warning(f"Could not check backtest status at iteration {i}: {e}")
                        # Keep existing cached status
                
                if cached_backtest_status not in ["running", "pending"]:
                    logger.info(
                        f"[BACKTEST-RUNNER] Backtest stopped (status: {cached_backtest_status}), stopping execution",
                        extra={"backtest_id": backtest_id, "status": cached_backtest_status, "iteration": i}
                    )
                    break
                
                # ── Bar clock ────────────────────────────────────────────
                # Everything live's monitor would have woken on since the last
                # checkpoint, and nothing stamped after this one.
                checkpoint_time = _as_utc_datetime(checkpoint)
                advance = self._advance_closed_bars_to(
                    checkpoint_time,
                    open_position=open_position,
                    armed_strategy=armed_strategy,
                    last_processed_bar_time=last_processed_bar_time,
                    entry_iteration=i,
                    refuse_signature=last_closed_signature,
                )
                open_position = advance.open_position
                armed_strategy = advance.armed_strategy
                last_processed_bar_time = advance.last_processed_bar_time
                last_closed_signature = advance.refuse_signature

                total_signals += len(advance.opened)
                for opened_position in advance.opened:
                    logger.info(
                        f"[BACKTEST-RUNNER] Position opened at "
                        f"{opened_position.entry_time.isoformat()}: "
                        f"{opened_position.trade_state['direction']} "
                        f"{opened_position.quantity} @ {opened_position.trade_state['entry']} "
                        f"— no model call until it closes",
                        extra={"backtest_id": backtest_id, "iteration": i},
                    )

                for closed in advance.closes:
                    profit = closed.profit
                    total_pnl += profit
                    cumulative_pnl += profit
                    daily_guards.register_trade(profit)
                    self._settle_closed_trade(profit)
                    if profit > 0:
                        wins += 1
                    elif profit < 0:
                        losses += 1
                    _equity_peak = max(_equity_peak, cumulative_pnl)
                    max_drawdown = max(max_drawdown, _equity_peak - cumulative_pnl)

                    closed_iteration = {
                        "iteration": i,
                        "iteration_timestamp": checkpoint.isoformat(),
                        "timestamp": checkpoint.isoformat(),
                        "prediction": closed.prediction,
                        "trade_outcome": closed.outcome,
                        "outcome": closed.outcome,
                        "status": "completed",
                        "monitoring_active_trade": True,
                        "entry_iteration": closed.entry_iteration,
                        # The lifecycle field the iteration document already
                        # models. `monitoring_active_trade`/`entry_iteration` are
                        # stripped before the DB write, so this is what makes
                        # occupancy auditable from the exported stream at all.
                        "trade_status": "closed",
                    }
                    iterations.append(closed_iteration)
                    try:
                        _iteration_write_buffer.append(normalize_iteration_data(closed_iteration))
                    except Exception as persist_err:
                        logger.warning(
                            f"Could not normalise closed-position iteration {i}: {persist_err}",
                            extra={"backtest_id": backtest_id, "iteration": i},
                        )
                    logger.info(
                        f"[BACKTEST-RUNNER] Iteration {i}: position closed "
                        f"{closed.outcome.get('result')} profit={profit:.4f}",
                        extra={"backtest_id": backtest_id, "iteration": i},
                    )

                # ── Cycle clock ─────────────────────────────────────────
                # Live skips its cycle entirely while a position is open — no
                # model call, no new strategy, no second position. This is the
                # rule the old runner declared and never reached, because the
                # iteration resolved every position before this point was hit.
                if open_position is not None:
                    held_minutes = (
                        checkpoint_time - open_position.entry_time
                    ).total_seconds() / 60
                    logger.info(
                        f"[BACKTEST-RUNNER] Iteration {i}: position from iteration "
                        f"{open_position.entry_iteration} still open "
                        f"({held_minutes:.0f}min held, expiry "
                        f"{open_position.expiry.isoformat()}) — skipping model call",
                        extra={
                            "backtest_id": backtest_id,
                            "iteration": i,
                            "entry_iteration": open_position.entry_iteration,
                            "held_minutes": held_minutes,
                        },
                    )
                    snapshot = unrealised_snapshot(
                        open_position, self._price_at(checkpoint_time)
                    )
                    monitoring_iteration = {
                        "iteration": i,
                        "iteration_timestamp": checkpoint.isoformat(),
                        "timestamp": checkpoint.isoformat(),
                        "prediction": open_position.prediction,
                        "trade_outcome": snapshot,
                        "outcome": snapshot,
                        "status": "completed",
                        "monitoring_active_trade": True,
                        "entry_iteration": open_position.entry_iteration,
                        "trade_status": "active",
                        "trade_entry_time": open_position.entry_time,
                    }
                    iterations.append(monitoring_iteration)
                    # Persisted, not just held in memory: "how many checkpoints
                    # were spent monitoring a position" is the metric that
                    # exposed the old runner's missing occupancy guard, and it
                    # can only be audited from the exported stream.
                    try:
                        _iteration_write_buffer.append(
                            normalize_iteration_data(monitoring_iteration)
                        )
                    except Exception as persist_err:
                        logger.warning(
                            f"Could not normalise monitoring iteration {i}: {persist_err}",
                            extra={"backtest_id": backtest_id, "iteration": i},
                        )
                    continue

                # An armed strategy inside its TTL also occupies the slot: live
                # does not re-run the model while a window it already paid for is
                # still watching for its trigger.
                if armed_strategy is not None:
                    if armed_strategy.has_expired_at(checkpoint_time):
                        armed_strategy = None
                    else:
                        logger.info(
                            f"[BACKTEST-RUNNER] Iteration {i}: strategy armed at "
                            f"iteration {armed_strategy.armed_at_iteration} still watching "
                            f"(TTL expires {armed_strategy.ttl_expiry.isoformat()}) "
                            f"— skipping model call",
                            extra={"backtest_id": backtest_id, "iteration": i},
                        )
                        continue

                # ── Daily guards ────────────────────────────────────────
                checkpoint_day = checkpoint_time.strftime("%Y-%m-%d")
                if checkpoint_day != _current_guard_day:
                    if _current_guard_day is not None:
                        logger.info(
                            f"[TRADE-GUARD] New UTC day {checkpoint_day} — resetting daily guards "
                            f"(prev day {_current_guard_day}: {daily_guards.trade_count} trades, "
                            f"PnL={daily_guards.cumulative_pnl:+.2f})",
                            extra={"backtest_id": backtest_id, "iteration": i},
                        )
                    daily_guards = DailyGuards(
                        max_loss=float("inf"),
                        max_trades=BACKTEST_MAX_TRADES_PER_DAY,
                        cooldown=BACKTEST_COOLDOWN_AFTER_SL,
                        loss_threshold=BACKTEST_COOLDOWN_SL_THRESHOLD,
                    )
                    _current_guard_day = checkpoint_day
                    # New UTC day = new drawdown baseline, exactly as
                    # account_guard re-keys its persisted baseline by date.
                    _day_start_equity = self.balance

                # Account daily drawdown kill switch (live parity): live calls
                # account_trading_halted() before every entry. Open positions are
                # never touched, matching live.
                if (
                    self.use_risk_sizing
                    and ACCOUNT_DAILY_DD_GUARD_ENABLED
                    and _day_start_equity > 0
                ):
                    _dd_pct = (_day_start_equity - self.balance) / _day_start_equity * 100.0
                    if _dd_pct >= ACCOUNT_MAX_DAILY_DD_PCT:
                        logger.warning(
                            f"[TRADE-GUARD] Account daily DD guard — skipping iteration {i}: "
                            f"equity ${self.balance:,.2f} is {_dd_pct:.2f}% below the "
                            f"{checkpoint_day} open of ${_day_start_equity:,.2f} "
                            f"(limit {ACCOUNT_MAX_DAILY_DD_PCT}%)",
                            extra={
                                "backtest_id": backtest_id,
                                "iteration": i,
                                "guard": "account_daily_dd",
                                "day_start_equity": round(_day_start_equity, 2),
                                "equity": round(self.balance, 2),
                                "dd_pct": round(_dd_pct, 2),
                            },
                        )
                        total_filtered_by_guards += 1
                        continue

                from .config import TRADE_COOLDOWN_MINUTES, TRADE_THROTTLE_ENABLED
                operational_blocked, operational_reason = self._operational_ledger.throttle(
                    self._ledger_account, self.asset, checkpoint_time,
                    cap=BACKTEST_MAX_TRADES_PER_DAY, loss_threshold=BACKTEST_COOLDOWN_SL_THRESHOLD,
                    cooldown_minutes=TRADE_COOLDOWN_MINUTES,
                )
                if operational_blocked and TRADE_THROTTLE_ENABLED:
                    logger.info(
                        f"[TRADE-GUARD] Skipping iteration {i}: Daily guard blocked "
                        f"(trades={daily_guards.trade_count}/{BACKTEST_MAX_TRADES_PER_DAY}, "
                        f"reason={operational_reason})",
                        extra={
                            "backtest_id": backtest_id,
                            "iteration": i,
                            "guard": "daily_budget",
                            "trade_count": daily_guards.trade_count,
                            "cooldown_remaining": daily_guards.cooldown_remaining,
                        },
                    )
                    total_filtered_by_guards += 1
                    continue

                # ── The model call ──────────────────────────────────────
                # Nothing open, nothing armed, guards clear: this is the only
                # state in which live starts a new cycle.
                logger.info(
                    f"[BACKTEST-RUNNER] Iteration {i}/{len(checkpoints)} at {checkpoint}: "
                    f"slot free — running cycle",
                    extra={"backtest_id": backtest_id, "iteration": i},
                )
                iteration = await self._execute_iteration(
                    timestamp=checkpoint,
                    backtest_id=backtest_id,
                    iteration_num=i,
                    cached_status=cached_backtest_status,
                )

                monitoring_state = iteration.get("active_code_monitoring")
                if monitoring_state:
                    armed_strategy = ArmedStrategy.from_monitoring_state(
                        monitoring_state, armed_at=checkpoint_time, armed_at_iteration=i,
                    )
                    if armed_strategy is not None:
                        logger.info(
                            f"[BACKTEST-RUNNER] Iteration {i}: strategy armed until "
                            f"{armed_strategy.ttl_expiry.isoformat()} — the trigger is "
                            f"checked on each bar that closes from here",
                            extra={"backtest_id": backtest_id, "iteration": i},
                        )

                _pending_norm = iteration.pop("_normalized_for_db", None)
                iterations.append(iteration)

                if iteration.get("status") in ("error", "skipped"):
                    logger.info(
                        f"[BACKTEST-RUNNER] Iteration {i} status: {iteration.get('status')}",
                        extra={
                            "backtest_id": backtest_id,
                            "iteration": i,
                            "status": iteration.get("status"),
                            "error": iteration.get("error"),
                        },
                    )
                    if iteration.get("status") == "error":
                        _consecutive_gpt_errors += 1
                        if _consecutive_gpt_errors >= _MAX_CONSECUTIVE_GPT_ERRORS:
                            logger.error(
                                f"[BACKTEST-RUNNER] Terminating backtest: "
                                f"{_consecutive_gpt_errors} consecutive GPT/API errors. "
                                f"Last error: {iteration.get('error', 'unknown')}",
                                extra={"backtest_id": backtest_id, "iteration": i},
                            )
                            break
                    continue

                _consecutive_gpt_errors = 0


                # Collect normalised iteration for batch DB write
                _norm = _pending_norm
                if _norm is not None:
                    _iteration_write_buffer.append(_norm)
                    if len(_iteration_write_buffer) >= _DB_BATCH_SIZE:
                        try:
                            await backtest_repo.add_iterations_bulk(backtest_id, _iteration_write_buffer)
                        except Exception as _be:
                            logger.warning("Batch DB write failed, falling back to individual writes: %s", _be)
                            for _it in _iteration_write_buffer:
                                try:
                                    await backtest_repo.add_iteration(backtest_id=backtest_id, iteration_data=_it)
                                except Exception as _ie:
                                    logger.error("Failed to save iteration individually: %s", _ie)
                        _iteration_write_buffer.clear()

                # Update progress periodically (if method exists)
                if i % 10 == 0:
                    try:
                        if hasattr(backtest_repo, 'update_backtest_progress'):
                            await backtest_repo.update_backtest_progress(
                                backtest_id=backtest_id,
                                iterations=i,
                                total_signals=total_signals,
                                wins=wins,
                                losses=losses,
                                total_pnl=total_pnl,
                                max_drawdown=max_drawdown,
                            )
                    except Exception as e:
                        logger.warning(f"Failed to update progress: {e}")
            
            # Flush any remaining buffered iterations to DB
            if _iteration_write_buffer:
                try:
                    await backtest_repo.add_iterations_bulk(backtest_id, _iteration_write_buffer)
                except Exception as _be:
                    logger.warning("Final batch DB write failed, falling back to individual writes: %s", _be)
                    for _it in _iteration_write_buffer:
                        try:
                            await backtest_repo.add_iteration(backtest_id=backtest_id, iteration_data=_it)
                        except Exception as _ie:
                            logger.error("Failed to save iteration individually in final flush: %s", _ie)
                _iteration_write_buffer.clear()

            # If terminated due to consecutive GPT errors, mark backtest as failed
            if _consecutive_gpt_errors >= _MAX_CONSECUTIVE_GPT_ERRORS:
                logger.error(
                    f"Backtest '{test_name}' terminated early due to {_consecutive_gpt_errors} consecutive GPT errors",
                    extra={"backtest_id": backtest_id}
                )
                try:
                    if hasattr(backtest_repo, 'update_backtest_status'):
                        await backtest_repo.update_backtest_status(
                            backtest_id,
                            "failed",
                            error_message=f"Terminated: {_consecutive_gpt_errors} consecutive GPT/API errors"
                        )
                except Exception as _ue:
                    logger.error(f"Failed to update backtest status after GPT abort: {_ue}")
                return

            # Calculate final metrics
            win_rate = wins / total_signals if total_signals > 0 else 0.0
            
            # Calculate average risk/reward
            rr_values = [
                it.get("outcome", {}).get("risk_reward", 0.0)
                for it in iterations
                if it.get("status") == "completed" and it.get("outcome", {}).get("risk_reward")
            ]
            avg_rr = sum(rr_values) / len(rr_values) if rr_values else 0.0
            
            # Update final status (if method exists)
            try:
                if hasattr(backtest_repo, 'update_backtest_completion'):
                    await backtest_repo.update_backtest_completion(
                        backtest_id=backtest_id,
                        iterations=len(iterations),
                        total_signals=total_signals,
                        wins=wins,
                        losses=losses,
                        total_pnl=total_pnl,
                        max_drawdown=max_drawdown,
                        win_rate=win_rate,
                        avg_rr=avg_rr,
                    )
            except Exception as e:
                logger.warning(f"Failed to update completion status: {e}")
            
            logger.info(
                f"Backtest '{test_name}' completed: "
                f"{total_signals} signals, {wins} wins, {losses} losses, "
                f"PNL: {total_pnl:.2f}, Win Rate: {win_rate:.2%}"
            )
            if self.use_risk_sizing:
                _ret_pct = (
                    (self.balance - self.initial_capital) / self.initial_capital * 100.0
                    if self.initial_capital else 0.0
                )
                logger.info(
                    f"[BACKTEST-RUNNER] Account: ${self.initial_capital:,.2f} -> "
                    f"${self.balance:,.2f} ({_ret_pct:+.2f}%), "
                    f"max drawdown ${max_drawdown:,.2f}, risk {self.risk_pct * 100:.2f}%/trade"
                )
            logger.info(
                f"[TRADE-GUARD] Guard summary: {total_filtered_by_guards} iterations skipped by daily guards "
                f"(max {BACKTEST_MAX_TRADES_PER_DAY}/day, cooldown={BACKTEST_COOLDOWN_AFTER_SL} after SL, "
                f"min RR={BACKTEST_MIN_RR})",
                extra={"backtest_id": backtest_id, "filtered_by_guards": total_filtered_by_guards},
            )
            
        except Exception as e:
            logger.error(f"Backtest '{test_name}' failed: {e}", exc_info=True)
            try:
                if hasattr(backtest_repo, 'update_backtest_status'):
                    await backtest_repo.update_backtest_status(
                        backtest_id,
                        "failed",
                        error_message=str(e)
                    )
            except Exception as update_error:
                logger.error(f"Failed to update backtest status: {update_error}")

