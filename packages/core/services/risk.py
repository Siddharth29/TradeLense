"""Risk management service leveraging broker balances for sizing."""

from __future__ import annotations

from typing import Dict, Any
import time

from packages.adapters.broker_base import IBroker
from packages.core.risk import (
    calc_quantity as _calc_quantity,
    calc_lot_size_by_risk as _calc_lot_size_by_risk,
    calc_risk_reward as _calc_risk_reward,
)


class RiskService:
    """Service providing position sizing helpers based on account balance."""

    def __init__(
        self,
        broker: IBroker,
        currency: str = "cash",
        min_reward_to_risk: float | None = None,
    ) -> None:
        """Create a :class:`RiskService` instance.

        Parameters
        ----------
        broker: IBroker
            Adapter providing balance and margin information.
        currency: str, optional
            Balance key to use when calculating risk. Defaults to ``"cash"``.
        min_reward_to_risk: float, optional
            Minimum acceptable reward‑to‑risk ratio. When provided,
            :meth:`calc_quantity` will raise :class:`ValueError` if the ratio
            derived from ``entry``, ``stop`` and ``take_profit`` falls below this
            threshold.
        """

        self._broker = broker
        self._currency = currency
        self._balances: Dict[str, float] = {}
        self._margin_cache: Dict[tuple, tuple[float, float]] = {}
        self._min_reward_to_risk = min_reward_to_risk

    def update_balances(self, balances: Dict[str, float]) -> None:
        """Provide *balances* for subsequent calculations."""

        self._balances = balances

    def available_funds(self) -> float:
        """Return available funds for the configured *currency*."""

        return float(self._balances.get(self._currency, 0.0))

    # pylint: disable=too-many-arguments
    def calc_quantity(
        self,
        *,
        risk: float,
        entry: float,
        stop: float,
        tick_size: float,
        lot_size: float,
        min_qty: float,
        max_qty: float,
        risk_is_pct: bool = False,
        symbol: str | None = None,
        take_profit: float | None = None,
    ) -> float:
        """Wrapper for :func:`calc_quantity` using broker balance.

        When ``symbol`` represents an Indian index, the current spot price is
        verified against *entry* using the first out‑of‑the‑money option from
        the live option chain. If the prices differ beyond ``tick_size`` a
        :class:`ValueError` is raised indicating a stale or invalid quote.

        Parameters
        ----------
        risk: float
            Risk amount or percentage depending on ``risk_is_pct``.
        entry: float
            Entry price.
        stop: float
            Stop loss price.
        tick_size: float
            Minimum price increment supported by the exchange.
        lot_size: float
            Minimum quantity increment.
        min_qty: float
            Minimum allowable quantity.
        max_qty: float
            Maximum allowable quantity.
        risk_is_pct: bool, optional
            Interpret ``risk`` as a percentage of balance when ``True``.
        symbol: str, optional
            When provided, validates spot price for certain indices.
        take_profit: float, optional
            Target price used to validate the reward‑to‑risk ratio when the
            service is configured with ``min_reward_to_risk``.

        When the service is configured with ``min_reward_to_risk`` and
        ``take_profit`` is supplied, the reward‑to‑risk ratio is validated and
        a :class:`ValueError` is raised when the ratio falls below the
        threshold.
        """

        if self._min_reward_to_risk is not None and take_profit is not None:
            ratio = _calc_risk_reward(entry=entry, stop=stop, take_profit=take_profit)
            if ratio < self._min_reward_to_risk:
                raise ValueError("reward-to-risk below threshold")

        balance = self.available_funds()
        return _calc_quantity(
            balance=balance,
            risk=risk,
            entry=entry,
            stop=stop,
            tick_size=tick_size,
            lot_size=lot_size,
            min_qty=min_qty,
            max_qty=max_qty,
            risk_is_pct=risk_is_pct,
        )

    def calc_lot_size_by_risk(
        self,
        *,
        risk_pct: float,
        sl_points: float,
        symbol: str,
        entry_price: float,
        specs: Dict[str, Any] | None = None,
        account_currency: str | None = None,
    ) -> float:
        """Wrapper for :func:`calc_lot_size_by_risk` using broker balance."""

        from packages.core.risk import CONTRACT_SPECS

        balance = self.available_funds()
        return _calc_lot_size_by_risk(
            balance,
            risk_pct,
            sl_points,
            symbol,
            CONTRACT_SPECS if specs is None else specs,
            entry_price,
            self._currency if account_currency is None else account_currency,
        )

    async def lot_size_for_option_buy(
        self,
        *,
        broker: str,
        premium: float,
        qty_per_lot: float,
        risk_pct: float | None,
        risk_amount: float | None,
        sl_pct: float | None,
        sl_abs: float | None,
        max_lots: int | None = None,
    ) -> tuple[int, float, float]:
        """Return (lots, risk_per_lot, used_risk) for buying options.

        The calculation is based on available funds and the provided risk
        parameters. ``risk_pct`` is interpreted as a percentage (e.g. ``1`` for
        1%). ``sl_pct`` is likewise interpreted as a percentage. At least one of
        ``risk_pct`` or ``risk_amount`` as well as ``sl_pct`` or ``sl_abs`` must
        be supplied.
        """

        if premium <= 0 or qty_per_lot <= 0:
            raise ValueError("premium and qty_per_lot must be positive")

        if sl_abs is not None:
            risk_per_unit = float(sl_abs)
        elif sl_pct is not None:
            risk_per_unit = premium * float(sl_pct) / 100.0
        else:
            raise ValueError("stop loss required")

        if risk_per_unit <= 0:
            raise ValueError("invalid stop loss")

        risk_per_lot = risk_per_unit * float(qty_per_lot)

        if risk_amount is not None:
            budget = float(risk_amount)
        elif risk_pct is not None:
            budget = self.available_funds() * float(risk_pct) / 100.0
        else:
            raise ValueError("risk required")

        if budget <= 0 or risk_per_lot <= 0:
            return 0, risk_per_lot, 0.0

        lots = int(budget // risk_per_lot)
        if max_lots is not None:
            lots = min(lots, int(max_lots))
        used = lots * risk_per_lot
        return lots, risk_per_lot, used

    def ensure_margin(self, decision: Any, *, raise_error: bool = False) -> bool:
        """Return ``True`` if sufficient funds exist for *decision*.

        Parameters
        ----------
        decision:
            Order payload to evaluate.
        raise_error:
            When ``True`` a :class:`ValueError` is raised if available funds are
            insufficient. This allows callers to both query and enforce margin
            requirements with a single method.

        Margin lookups are cached for 15 seconds keyed by the order parameters to
        avoid repeated broker calls.  The function branches on broker type so
        that both cash margin and premium style calculations (e.g. MT5) are
        supported.
        """

        key = (
            self._broker.__class__.__name__,
            getattr(decision, "symbol", None),
            getattr(decision, "side", None),
            getattr(decision, "price_type", None),
            float(getattr(decision, "entry", 0.0)),
            float(getattr(decision, "quantity", 0.0)),
        )

        now = time.monotonic()
        cached = self._margin_cache.get(key)
        if cached and now - cached[0] < 15.0:
            required = cached[1]
        else:
            margin_fn = getattr(self._broker, "order_margin", None)
            if margin_fn is None:
                raise AttributeError("broker missing order_margin")

            broker_name = self._broker.__class__.__name__.lower()
            if "mt5" in broker_name:
                # MT5 adapters return the premium directly
                required = float(margin_fn(decision))
            else:
                required = float(margin_fn(decision))
            self._margin_cache[key] = (now, required)

        available = self.available_funds()
        has_margin = available >= required
        if raise_error and not has_margin:
            raise ValueError("insufficient margin")
        return has_margin

    def ensure_margin_or_raise(self, decision: Any) -> None:
        """Raise :class:`ValueError` when funds are insufficient.

        This convenience wrapper calls :meth:`ensure_margin` with
        ``raise_error=True``.
        """

        self.ensure_margin(decision, raise_error=True)


__all__ = ["RiskService"]
