"""MT5-specific risk management utilities."""

from dataclasses import dataclass
from typing import Dict, Optional
from ..volume import floor_volume, positive_decimal


@dataclass
class ContractSpecs:
    """Specification for trading a particular contract."""

    symbol: str
    lot_step: float
    min_lot: float
    max_lot: float
    base_currency: str
    quote_currency: str
    contract_size: float


def calc_lot_size_by_risk(
    balance: float,
    risk_pct: float,
    sl_points: float,
    symbol: str,
    specs: Dict[str, ContractSpecs],
    entry_price: float = 1.0,
    account_currency: Optional[str] = None,
) -> float:
    """Calculate lot size based on account risk and stop-loss distance.

    The result is rounded to the contract's ``lot_step`` and clamped between
    ``min_lot`` and ``max_lot``.

    Parameters
    ----------
    balance: float
        Account balance.
    risk_pct: float
        Fraction of ``balance`` to risk (e.g. ``0.01`` for 1%).
    sl_points: float
        Stop-loss distance in points.
    symbol: str
        Symbol to calculate the lot size for.
    specs: dict[str, ContractSpecs]
        Mapping of symbols to their contract specifications.
    entry_price: float, optional
        Price of the symbol used for currency conversion. Defaults to ``1.0``.
    account_currency: str, optional
        Account currency. When ``None`` the contract's quote currency is
        assumed.

    Returns
    -------
    float
        Lot size respecting rounding and contract bounds.

    Raises
    ------
    ValueError
        If any of ``balance``, ``risk_pct`` or ``sl_points`` is non-positive.
    """

    contract = specs[symbol]
    risk_amount = positive_decimal(balance, "balance") * positive_decimal(risk_pct, "risk_pct")
    stop_distance = positive_decimal(sl_points, "sl_points")
    contract_size = positive_decimal(contract.contract_size, "contract_size")

    if account_currency is None:
        account_currency = contract.quote_currency

    if account_currency == contract.quote_currency:
        conversion_rate = positive_decimal(1, "conversion_rate")
    elif account_currency == contract.base_currency:
        # USDJPY-style: risk is denominated in the quote currency; dividing by
        # the pair's own price converts quote → base (= account) currency.
        conversion_rate = positive_decimal(entry_price, "entry_price")
    else:
        # True cross (e.g. EURGBP on a USD account): converting quote → account
        # needs a THIRD rate this function does not have. The old code used
        # entry_price here, silently mis-sizing the position. Fail closed —
        # a wrong lot size on a live order is worse than no order.
        raise ValueError(
            f"Cannot size {symbol}: converting {contract.quote_currency} risk to "
            f"{account_currency} requires a {contract.quote_currency}/{account_currency} "
            "rate that is not available. Cross-quoted instruments are unsupported."
        )

    # Universal formula: risk_per_lot = sl_distance * contract_size / conversion_rate
    # sl_points here is the absolute price difference (entry - stop)
    risk_per_lot = stop_distance * contract_size / conversion_rate
    
    if risk_per_lot <= 0:
        raise ValueError(f"Invalid risk_per_lot={risk_per_lot} for {symbol}")
    
    raw_lot = risk_amount / risk_per_lot

    # Floor (not round) to the lot step: rounding UP would risk MORE than the
    # requested risk_pct. Flooring guarantees realised risk ≤ intended (the only
    # exception is the min_lot clamp below, which the caller flags as overshoot).
    # This is a candidate size, BEFORE the caller's min-lot risk-budget gate.
    # Preserve that explicit policy; final broker normalization must never
    # increase the quantity the caller actually approved.
    candidate = max(positive_decimal(contract.min_lot, "min_lot"), raw_lot)
    return floor_volume(candidate, contract.lot_step, contract.min_lot, contract.max_lot)
