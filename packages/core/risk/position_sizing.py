import math


def _round_down(value: float, step: float) -> float:
    """Round down *value* to the nearest multiple of *step*.

    Parameters
    ----------
    value: float
        The value to round.
    step: float
        The increment to round to. Must be positive.
    """
    if step <= 0:
        raise ValueError("step must be positive")
    return math.floor(value / step) * step


def calc_risk_reward(entry: float | None, stop: float | None, take_profit: float | None) -> float:
    """Return reward-to-risk ratio for a trade setup.

    Parameters
    ----------
    entry: float | None
        Intended entry price. None indicates flat signal.
    stop: float | None
        Protective stop loss price. None indicates flat signal.
    take_profit: float | None
        Target take profit price. None indicates flat signal.

    Returns
    -------
    float
        Reward divided by risk.

    Raises
    ------
    ValueError
        If any price is None (flat signal) or stop/take profit distance is zero or negative.
    """
    
    # Guard against flat signals (None prices)
    if entry is None or stop is None or take_profit is None:
        raise ValueError(
            f"Risk/reward calculation requires non-None prices. "
            f"Got: entry={entry}, stop={stop}, take_profit={take_profit}. "
            f"This likely indicates a flat signal that should not be processed."
        )

    risk = abs(entry - stop)
    reward = abs(take_profit - entry)
    if risk <= 0:
        raise ValueError("stop distance must be greater than zero")
    if reward <= 0:
        raise ValueError("take profit distance must be greater than zero")
    return reward / risk


# pylint: disable=too-many-arguments
# The function mirrors common exchange parameters and therefore needs many
# arguments for flexibility.
def calc_quantity(
    balance: float,
    risk: float,
    entry: float,
    stop: float,
    tick_size: float,
    lot_size: float,
    min_qty: float,
    max_qty: float,
    risk_is_pct: bool = False,
) -> float:
    """Calculate trade quantity based on risk parameters.

    Parameters
    ----------
    balance: float
        Account balance.
    risk: float
        Risk amount or percentage depending on ``risk_is_pct``.
    entry: float
        Entry price.
    stop: float
        Stop loss price.
    tick_size: float
        Minimum price increment supported by the exchange. Quantity will be
        rounded down to this increment.
    lot_size: float
        Minimum quantity increment. Quantity will be rounded down to this
        increment before tick-size rounding.
    min_qty: float
        Minimum allowable quantity.
    max_qty: float
        Maximum allowable quantity.
    risk_is_pct: bool, optional
        Interpret ``risk`` as a percentage of ``balance`` when ``True``.

    Returns
    -------
    float
        The final trade quantity respecting rounding and bounds.

    Raises
    ------
    ValueError
        If any numeric input is invalid or stop distance is zero.
    """

    if balance <= 0:
        raise ValueError("balance must be positive")
    if risk <= 0:
        raise ValueError("risk must be positive")
    if tick_size <= 0 or lot_size <= 0:
        raise ValueError("tick_size and lot_size must be positive")
    if min_qty <= 0 or max_qty <= 0 or min_qty > max_qty:
        raise ValueError("invalid quantity bounds")

    stop_distance = abs(entry - stop)
    if stop_distance <= 0:
        raise ValueError("stop distance must be greater than zero")

    risk_amount = balance * risk if risk_is_pct else risk
    quantity = risk_amount / stop_distance

    quantity = _round_down(quantity, lot_size)
    quantity = _round_down(quantity, tick_size)

    quantity = max(min_qty, quantity)
    quantity = min(max_qty, quantity)
    return quantity
