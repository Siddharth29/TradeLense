"""Anthropic prompt-caching policy — decide where the cache breakpoint goes.

Why this exists
---------------
Measured on the 2026-07 logs (20 days, 2,176 calls, 20.48M tokens): the stable
prefix of every prompt is the *system* block — 31-36% of an expert prompt and
51-55% of a judge prompt. The rest is candles and live context, which change
every cycle and can never cache. Caching that prefix is worth roughly 15-19% of
API spend; it is not a rewrite, it is one breakpoint in the right place.

Design constraints (non-negotiable, from the ship request)
----------------------------------------------------------
1. **Zero latency.** Pure in-memory dict work — no I/O, no network, no
   ``count_tokens`` round-trip. Everything here is O(len(system)) at worst.
2. **Never break a call.** :func:`apply_cache_control` is total: on *any*
   exception, or any input it does not fully understand, it returns the caller's
   kwargs unchanged and logs a WARNING. A caching bug must never cost a trade.
3. **Fallback is observable.** Every decline path logs why, so a silently
   cold cache shows up in the day log instead of only on the invoice.

SOLID
-----
- **S**  A policy decides *where* a breakpoint belongs and nothing else;
  applying it to the request kwargs is a separate function.
- **O**  New behaviour arrives as a new :class:`CacheBreakpointPolicy`
  implementation. Neither the applier nor the client is edited.
- **L**  Every policy honours the same contract: return a ``CacheDecision``,
  never raise, never mutate its input.
- **I**  The protocol is one method. Implementations need nothing else.
- **D**  ``anthropic_client`` depends on the protocol and a module-level
  default, not on any concrete policy.

Caching mechanics that drove the design
---------------------------------------
- Caching is a **prefix match**: render order is ``tools`` -> ``system`` ->
  ``messages``, so one breakpoint on the system block caches tools+system
  together and nothing volatile.
- There is a **minimum cacheable prefix** (1024 tokens on Sonnet 4.6 / Sonnet 5).
  Below it the cache silently does not form and the 1.25x write premium is paid
  for nothing — so we refuse to mark a prefix we estimate is too short.
- **TTL matters more than it looks.** Live, this system runs ~1.15 cycles per
  asset per hour, so a 5-minute cache is always cold by the next call and only
  ever pays the write premium. Backtests replay cycles back-to-back, where 5m is
  fine and cheaper to write. Hence the two profiles below.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol, runtime_checkable

from .logger import get_logger

logger = get_logger(__name__)


# ── Tunables ────────────────────────────────────────────────────────────────
# Local token estimate. The API bills real tokens; we only need a *safe*
# lower-bound guess to avoid marking a prefix that cannot cache. English prose
# runs ~4 chars/token; prompts here carry numbers and JSON, which tokenize
# worse, so 4.0 understates the count and errs toward declining. That is the
# correct direction: a missed cache costs nothing, a wasted write costs 1.25x.
CHARS_PER_TOKEN_ESTIMATE = 4.0

# Sonnet 4.6 and Sonnet 5 both require a 1024-token minimum cacheable prefix.
# Padded 15% so an optimistic char estimate cannot slip under the real limit.
MIN_CACHEABLE_TOKENS = 1024
MIN_CACHEABLE_CHARS = int(MIN_CACHEABLE_TOKENS * CHARS_PER_TOKEN_ESTIMATE * 1.15)

TTL_LIVE = "1h"        # cycles are ~52 min apart per asset; 5m would always miss
TTL_BACKTEST = "5m"    # replay runs back-to-back; cheaper write, always warm


@dataclass(frozen=True)
class CacheDecision:
    """What a policy decided, and why.

    ``reason`` is always populated — including on success — so the log line is
    the same shape either way and a cold cache is greppable.
    """

    should_cache: bool
    ttl: str = TTL_LIVE
    reason: str = ""


@runtime_checkable
class CacheBreakpointPolicy(Protocol):
    """Decides whether a request's system prefix is worth a breakpoint.

    Implementations MUST NOT raise and MUST NOT mutate ``kwargs``. The applier
    guards against both anyway, but a policy that relies on that guard is
    logging a WARNING on every call.
    """

    def decide(self, kwargs: Dict[str, Any]) -> CacheDecision: ...


class SystemPrefixPolicy:
    """Cache the system block when it is long enough to actually form a cache.

    This is the whole policy for this codebase: the system block *is* the stable
    prefix (verified against the logs), and the user turn is volatile candle
    data that would poison the key if included.
    """

    def __init__(self, ttl: str = TTL_LIVE, min_chars: int = MIN_CACHEABLE_CHARS) -> None:
        self._ttl = ttl
        self._min_chars = min_chars

    def decide(self, kwargs: Dict[str, Any]) -> CacheDecision:
        system = kwargs.get("system")

        if not system:
            return CacheDecision(False, self._ttl, "no system block to cache")

        if not isinstance(system, str):
            # Already structured (blocks). Someone else owns the breakpoints;
            # re-marking risks exceeding the 4-breakpoint cap.
            return CacheDecision(False, self._ttl, "system already structured — leaving as-is")

        if len(system) < self._min_chars:
            return CacheDecision(
                False,
                self._ttl,
                f"system {len(system)} chars < {self._min_chars} min — below cacheable prefix",
            )

        return CacheDecision(True, self._ttl, f"system {len(system)} chars")


class DisabledPolicy:
    """Kill switch. Used when the feature flag is off, so the call path is
    identical whether caching is enabled or not — no branching at the call site.
    """

    def decide(self, kwargs: Dict[str, Any]) -> CacheDecision:  # noqa: D102
        return CacheDecision(False, TTL_LIVE, "caching disabled by config")


def apply_cache_control(
    kwargs: Dict[str, Any],
    policy: Optional[CacheBreakpointPolicy] = None,
    *,
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return ``kwargs`` with a cache breakpoint applied, or unchanged.

    Total function. Any failure — a bad policy, an unexpected payload shape, an
    exception from anywhere — returns the original object and logs a WARNING.
    The caller never needs a try/except and the request always goes out.

    The input dict is not mutated; on success a shallow copy is returned with
    only ``system`` replaced.
    """
    if policy is None:
        return kwargs

    ctx = dict(context or {})

    try:
        decision = policy.decide(kwargs)

        if not decision.should_cache:
            logger.info(
                "prompt_cache: not cached",
                extra={**ctx, "cache_applied": False, "cache_reason": decision.reason},
            )
            return kwargs

        system_text = kwargs["system"]
        cached = dict(kwargs)
        cached["system"] = [
            {
                "type": "text",
                "text": system_text,
                "cache_control": {"type": "ephemeral", "ttl": decision.ttl},
            }
        ]

        logger.info(
            "prompt_cache: breakpoint applied",
            extra={
                **ctx,
                "cache_applied": True,
                "cache_ttl": decision.ttl,
                "cache_prefix_chars": len(system_text),
                "cache_reason": decision.reason,
            },
        )
        return cached

    except Exception as exc:  # noqa: BLE001 — deliberate: never break the call
        logger.warning(
            "prompt_cache: FALLBACK to uncached prompt — %s: %s",
            type(exc).__name__,
            exc,
            extra={**ctx, "cache_applied": False, "cache_reason": f"exception:{type(exc).__name__}"},
        )
        return kwargs


def read_cache_usage(response: Any) -> Dict[str, int]:
    """Pull cache counters off a response for logging. Never raises.

    ``cache_read_input_tokens`` staying at 0 across repeated calls is the signal
    that something upstream is invalidating the prefix — that is the number to
    watch, not the bill.
    """
    out = {"cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "input_tokens": 0}
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            return out
        for field in out:
            value = getattr(usage, field, None)
            if isinstance(value, int):
                out[field] = value
    except Exception:  # noqa: BLE001 — logging helper must never break a call
        pass
    return out


def build_policy(enabled: bool, ttl: str) -> CacheBreakpointPolicy:
    """Resolve config into a policy. The only place the flag is read."""
    if not enabled:
        return DisabledPolicy()
    return SystemPrefixPolicy(ttl=ttl)
