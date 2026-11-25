"""Prompt caching + candle repository — 2026-08-14 ship.

The load-bearing property under test is the failure posture: caching must never
be able to break a call. Every decline path returns the caller's kwargs
untouched, and a policy that raises is contained rather than propagated.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from packages.core.candle_repository import (
    CandleSpec, CandleValidator, CoverageRule, MonotonicRule, NonEmptyRule,
    OhlcSanityRule, RuleResult,
)
from packages.core.candles import Candle
from packages.core.prompt_cache import (
    MIN_CACHEABLE_CHARS, DisabledPolicy, SystemPrefixPolicy, apply_cache_control,
    build_policy, read_cache_usage,
)

BIG = "S" * (MIN_CACHEABLE_CHARS + 500)
SMALL = "S" * 50


# ── Caching: the happy path ─────────────────────────────────────────────────

def test_long_system_gets_a_breakpoint():
    out = apply_cache_control({"system": BIG}, build_policy(True, "1h"))
    assert isinstance(out["system"], list)
    assert out["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert out["system"][0]["text"] == BIG


def test_ttl_is_configurable():
    out = apply_cache_control({"system": BIG}, build_policy(True, "5m"))
    assert out["system"][0]["cache_control"]["ttl"] == "5m"


def test_other_kwargs_survive_untouched():
    src = {"system": BIG, "model": "claude-sonnet-5", "max_tokens": 4096, "messages": [1]}
    out = apply_cache_control(src, build_policy(True, "1h"))
    assert out["model"] == "claude-sonnet-5"
    assert out["max_tokens"] == 4096
    assert out["messages"] == [1]


def test_input_is_not_mutated():
    src = {"system": BIG}
    apply_cache_control(src, build_policy(True, "1h"))
    assert isinstance(src["system"], str), "caller's dict must not be mutated"


# ── Caching: every decline path returns the original ────────────────────────

@pytest.mark.parametrize(
    "kwargs",
    [
        {"system": SMALL},                      # under the cacheable minimum
        {"messages": [{"role": "user"}]},       # no system block at all
        {"system": ""},                         # empty system
        {"system": [{"type": "text", "text": "x"}]},  # already structured
    ],
)
def test_declines_return_kwargs_unchanged(kwargs):
    out = apply_cache_control(dict(kwargs), build_policy(True, "1h"))
    assert out == kwargs


def test_disabled_policy_is_a_noop():
    src = {"system": BIG}
    assert apply_cache_control(src, DisabledPolicy()) is src


def test_no_policy_is_a_noop():
    src = {"system": BIG}
    assert apply_cache_control(src, None) is src


def test_a_raising_policy_falls_back_and_does_not_propagate():
    class Exploding:
        def decide(self, kwargs):
            raise RuntimeError("policy blew up")

    src = {"system": BIG, "messages": [1]}
    out = apply_cache_control(src, Exploding())
    assert out is src, "must fall back to the exact original kwargs"


def test_a_policy_returning_garbage_falls_back():
    class Garbage:
        def decide(self, kwargs):
            return "not a CacheDecision"

    src = {"system": BIG}
    assert apply_cache_control(src, Garbage()) is src


def test_min_chars_threshold_is_respected():
    policy = SystemPrefixPolicy(ttl="1h", min_chars=10_000)
    assert policy.decide({"system": "x" * 9_999}).should_cache is False
    assert policy.decide({"system": "x" * 10_001}).should_cache is True


# ── Usage readback never raises ─────────────────────────────────────────────

def test_read_cache_usage_on_junk():
    assert read_cache_usage(None)["cache_read_input_tokens"] == 0
    assert read_cache_usage(object())["cache_creation_input_tokens"] == 0


def test_read_cache_usage_reads_real_fields():
    class Usage:
        cache_read_input_tokens = 1234
        cache_creation_input_tokens = 56
        input_tokens = 78

    class Resp:
        usage = Usage()

    got = read_cache_usage(Resp())
    assert got == {
        "cache_read_input_tokens": 1234,
        "cache_creation_input_tokens": 56,
        "input_tokens": 78,
    }


# ── Candle validation rules ─────────────────────────────────────────────────

def _bar(minute: int, o=100.0, h=101.0, lo=99.0, c=100.5) -> Candle:
    return Candle(
        time=pd.Timestamp("2026-07-13 00:00:00", tz="UTC") + timedelta(minutes=minute),
        open=o, high=h, low=lo, close=c, volume=1.0,
    )


def _spec(hours: int = 1) -> CandleSpec:
    start = datetime(2026, 7, 13, tzinfo=timezone.utc)
    return CandleSpec("XAUUSD", "5m", start, start + timedelta(hours=hours))


def test_non_empty_rule():
    assert NonEmptyRule().check([], _spec()).ok is False
    assert NonEmptyRule().check([_bar(0)], _spec()).ok is True


def test_monotonic_rule_catches_duplicates_and_reordering():
    assert MonotonicRule().check([_bar(0), _bar(0)], _spec()).ok is False
    assert MonotonicRule().check([_bar(5), _bar(0)], _spec()).ok is False
    assert MonotonicRule().check([_bar(0), _bar(5), _bar(10)], _spec()).ok is True


def test_ohlc_sanity_rule_catches_inverted_bar():
    bad = _bar(0, o=100, h=98, lo=101, c=99)   # high < low
    assert OhlcSanityRule().check([bad], _spec()).ok is False


def test_coverage_rule_catches_a_short_pull():
    # Asked for 24h, broker returned 30 minutes: the failure that would
    # otherwise sweep the wrong fortnight without complaint.
    spec = _spec(hours=24)
    short = [_bar(i) for i in range(0, 30, 5)]
    assert CoverageRule().check(short, spec).ok is False


def test_coverage_rule_passes_within_tolerance():
    spec = _spec(hours=1)
    full = [_bar(i) for i in range(0, 65, 5)]
    assert CoverageRule().check(full, spec).ok is True


def test_validator_reports_every_failure_not_just_the_first():
    report = CandleValidator().validate([], _spec())
    assert report.ok is False
    assert len(report.failures) >= 2, "all rules must run, not short-circuit"
    assert "FAIL" in report.summary()


def test_a_raising_rule_becomes_a_failure_not_a_crash():
    class Exploding:
        name = "exploding"

        def check(self, candles, spec):
            raise ValueError("rule blew up")

    report = CandleValidator([Exploding()]).validate([_bar(0)], _spec())
    assert report.ok is False
    assert "raised ValueError" in report.failures[0].detail
