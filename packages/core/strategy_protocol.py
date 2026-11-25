"""Protocol definitions for strategy execution."""

from __future__ import annotations

from typing import Callable, Dict, List

DecideFunc = Callable[[List[Dict], str], Dict]
"""Type alias for a strategy ``decide`` function.

The callable receives two arguments:

* ``candles_5``: list of candle dictionaries.
* ``now``: ISO formatted timestamp string.

It must return a ``dict`` containing at least the following keys:

* ``action``: the action to take (e.g. ``"BUY"`` or ``"SELL"``).
* ``size``: numerical position size to use.
"""

__all__ = ["DecideFunc"]
