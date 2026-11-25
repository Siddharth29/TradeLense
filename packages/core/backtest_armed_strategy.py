"""A strategy the judge has armed, waiting for its trigger bar.

Live's ``CodeSniperMonitor`` holds exactly this between the cycle that produced a
strategy and the bar that fires it: the generated code, the judge's signal, the
cycle context that produced them, and a TTL. It is re-executed on every closed 5m
bar until it triggers or the TTL expires, and a fill consumes it.

The backtest used to model this twice and differently — once inside the iteration
that created the strategy (scanning ``ttl_minutes // 5`` bars into the future in
one go, which is where the look-ahead lived) and once across checkpoints (one bar
per hour, so most bars were never evaluated at all). Both are replaced by this
object plus a single per-bar call site.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

# Generated code that raises this many times in a row is broken rather than
# unlucky. Live aborts the strategy instead of retrying it every bar; the
# backtest matches so an invalid strategy cannot silently consume its whole TTL.
MAX_CONSECUTIVE_CODE_FAILURES = 3


@dataclass
class ArmedStrategy:
    """One armed strategy and the TTL that bounds its life."""

    code: str
    signal: Dict[str, Any]
    cycle_context: Dict[str, Any]
    armed_at: datetime
    ttl_expiry: datetime
    armed_at_iteration: int
    consecutive_code_failures: int = 0
    history: List[Dict[str, Any]] = field(default_factory=list, repr=False)

    def has_expired_at(self, bar_time: datetime) -> bool:
        """True once the bar clock has reached the judge's TTL."""
        return bar_time >= self.ttl_expiry

    def register_code_failure(self) -> bool:
        """Record a raising execution. Returns True when the strategy is unusable."""
        self.consecutive_code_failures += 1
        return self.consecutive_code_failures >= MAX_CONSECUTIVE_CODE_FAILURES

    def register_code_success(self) -> None:
        self.consecutive_code_failures = 0

    @classmethod
    def from_monitoring_state(
        cls,
        state: Dict[str, Any],
        *,
        armed_at: datetime,
        armed_at_iteration: int,
    ) -> Optional["ArmedStrategy"]:
        """Adapt ``_execute_iteration``'s monitoring-state dict, or None if unusable."""
        code = state.get("code")
        ttl_expiry = state.get("ttl_expiry")
        if not code or not isinstance(ttl_expiry, datetime):
            return None
        return cls(
            code=code,
            signal=state.get("signal_dict") or {},
            cycle_context=state.get("cycle_context") or {},
            armed_at=armed_at,
            ttl_expiry=ttl_expiry,
            armed_at_iteration=armed_at_iteration,
        )
