"""The only 5-minute candle view the backtest is allowed to read.

Why this exists
---------------
MT5 and the downloaded CSVs stamp bars at their OPEN. A 12:00 M5 bar is
available at 12:05, not at 12:00. All clock arguments below are wall-clock
instants; candle payloads retain the broker's open timestamps.

Before this module the runner clamped its own windows with ad-hoc arithmetic, and
every one of those was a separate opportunity to read the future:

* ``max_monitoring_candles = ttl_minutes // 5`` — scanned up to 48 bars (4h) PAST
  the checkpoint looking for a trigger, on an hourly checkpoint loop;
* ``max_fetch = BT_MONITOR_CANDLES_5M + 12`` — handed ``simulate_trade`` 60 bars
  (5h) so a position was resolved to completion inside the iteration that opened
  it, which is why ``active_trade`` was never set and the runner opened
  overlapping positions the live system can never hold;
* ``_end_idx = checkpoint_idx + _MIN_FUTURE_CANDLES`` — let the position monitor
  evaluate an hour beyond the checkpoint it was called for.

Bounding the data at the point of access, once, is what makes look-ahead
impossible by construction rather than by a convention the next edit can quietly
break.

Cost
----
Every lookup is O(1) (dict) or O(log n) (bisect over a pre-sorted time index) and
slices are views over the already-cached list, so the invariant adds no
measurable latency to the hot path.
"""

from __future__ import annotations

import bisect
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd

CandleDict = Dict[str, Any]
BAR_DURATION = pd.Timedelta(minutes=5)


def bar_close_time(bar: CandleDict) -> pd.Timestamp:
    """Availability time of an open-stamped MT5 M5 candle."""
    return pd.Timestamp(bar["time"]) + BAR_DURATION


class ClosedBarFeed:
    """Bars that have already closed at a given instant — and nothing else.

    Construct once per run from the runner's cached candles. Every method takes
    the clock explicitly, so a caller cannot accidentally read ahead: there is no
    method that returns a bar later than the ``now`` it was given.
    """

    __slots__ = ("_bars", "_times", "_index_of_time")

    def __init__(
        self,
        bars: Sequence[CandleDict],
        sorted_times: Sequence[pd.Timestamp],
        index_of_time: Dict[pd.Timestamp, int],
    ) -> None:
        self._bars = bars
        self._times = sorted_times
        self._index_of_time = index_of_time

    def __len__(self) -> int:
        return len(self._bars)

    # ── position on the timeline ─────────────────────────────────────────
    def index_of_last_bar_closed_at_or_before(self, now: Any) -> Optional[int]:
        """Index of the newest bar that had closed by ``now``, or None."""
        if not self._times:
            return None
        pos = bisect.bisect_right(self._times, pd.Timestamp(now) - BAR_DURATION) - 1
        if pos < 0:
            return None
        return self._index_of_time[self._times[pos]]

    def index_of_bar_at(self, when: Any) -> Optional[int]:
        """Index of the bar stamped exactly ``when``, or None."""
        return self._index_of_time.get(pd.Timestamp(when))

    def bar_at_index(self, index: int) -> Optional[CandleDict]:
        if 0 <= index < len(self._bars):
            return self._bars[index]
        return None

    def last_bar_closed_at_or_before(self, now: Any) -> Optional[CandleDict]:
        index = self.index_of_last_bar_closed_at_or_before(now)
        return None if index is None else self._bars[index]

    # ── the two windows the runner actually needs ────────────────────────
    def bars_closed_between(self, after_exclusive: Any, upto_inclusive: Any) -> List[CandleDict]:
        """Bars that closed strictly after ``after_exclusive`` and by ``upto_inclusive``.

        This is the catch-up window: everything live's monitor would have woken
        on since the backtest last looked. Empty when the clock has not advanced
        past another bar close, which is the correct answer, not an error.
        """
        end = self.index_of_last_bar_closed_at_or_before(upto_inclusive)
        if end is None:
            return []
        start = self.index_of_last_bar_closed_at_or_before(after_exclusive)
        first = 0 if start is None else start + 1
        if first > end:
            return []
        return list(self._bars[first : end + 1])

    def history_before_index(self, index: int, count: int) -> Optional[List[CandleDict]]:
        """The ``count`` bars immediately BEFORE ``index``, or None if short.

        The bar at ``index`` is excluded: it is the bar being evaluated, and the
        strategy code receives it separately as ``current_candle``.
        """
        if index < count:
            return None
        return list(self._bars[index - count : index])
