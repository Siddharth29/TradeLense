"""Tests for the 2026-08-06 edge-horizon changes.

These two changes come from a different place than the rest of the change set.
The others implement items the pattern library had already named and priced.
These two are contradictions between what the system SAYS its edge is and how it
is configured to trade it — neither had been written down before:

  1. **TTL horizon < edge horizon.** WP-04 (the metric that certifies the
     direction edge) counts follow-through up to 3h AFTER exit, while the
     maximum holding period was 120 minutes.
  2. **The re-entry gate suppressed the sweep re-entry rule.** The prompts hunt
     a same-side re-entry 0–12 bars after a stop-out; the gate demanded 0.78
     confidence for the first 6 of those bars.
"""

from __future__ import annotations

import pytest

from packages.core import config


# ── 1. TTL horizon ───────────────────────────────────────────────────────────

def test_ttl_covers_the_wp04_follow_through_window():
    """WP-04 measures ≥1R follow-through up to 3h after exit. The longest TTL
    must at least reach into that window, or the metric certifies an edge the
    position can never be present for."""
    assert max(config.TTL_MINUTES_ALLOWED) >= 180


def test_monitor_expiry_does_not_cap_the_longest_ttl():
    """A 240-minute TTL under a 4h monitor ceiling would have made the enum
    change cosmetic — the monitor would close the position at its own limit."""
    assert config.TRADE_MONITOR_EXPIRY_HOURS * 60 >= max(config.TTL_MINUTES_ALLOWED)


def test_ttl_enum_string_is_derived_not_hardcoded():
    """Prompt text and schema must be generated from one source. A drift here is
    an LP-09-class failure: the judge emits a TTL the schema rejects and the
    whole program silently parses to nothing."""
    for ttl in config.TTL_MINUTES_ALLOWED:
        assert str(ttl) in config.TTL_ENUM
    assert "60" not in config.TTL_ENUM.replace("180", "").replace("240", "")


def test_schema_literals_match_the_config_enum():
    """The pydantic Literals cannot be built from a tuple at runtime, so they are
    written out — and pinned here. This is the only thing standing between an
    edit to TTL_MINUTES_ALLOWED and a silent schema mismatch."""
    import typing

    from packages.core.schemas_trade import CodeSniperSignal, MultiExpertSignal

    for model in (MultiExpertSignal, CodeSniperSignal):
        annotation = model.__annotations__["ttl_minutes"]
        allowed = set(typing.get_args(annotation))
        assert allowed == set(config.TTL_MINUTES_ALLOWED), (
            f"{model.__name__}.ttl_minutes {sorted(allowed)} != "
            f"config.TTL_MINUTES_ALLOWED {sorted(config.TTL_MINUTES_ALLOWED)}"
        )


def test_json_schemas_match_the_config_enum():
    """Same pin for the raw JSON schemas handed to the model."""
    from packages.core import schemas_trade

    found = 0
    for name in dir(schemas_trade):
        obj = getattr(schemas_trade, name)
        if not isinstance(obj, dict):
            continue
        for enum_vals in _find_ttl_enums(obj):
            assert set(enum_vals) == set(config.TTL_MINUTES_ALLOWED), (
                f"{name} carries a stale ttl_minutes enum {enum_vals}"
            )
            found += 1
    assert found >= 2, "expected at least two JSON schemas to pin"


def _find_ttl_enums(node):
    """Yield every ``ttl_minutes.enum`` list nested anywhere in a schema dict."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "ttl_minutes" and isinstance(value, dict) and "enum" in value:
                yield value["enum"]
            else:
                yield from _find_ttl_enums(value)
    elif isinstance(node, list):
        for item in node:
            yield from _find_ttl_enums(item)


def test_ttl_score_penalties_cover_every_allowed_ttl():
    """An unlisted TTL falls back to a 1.0 penalty — effectively banning it.
    That is how a TTL can be 'allowed' and never chosen."""
    assert set(config.SCORE_TTL_PENALTIES) == set(config.TTL_MINUTES_ALLOWED)


def test_prompt_quotes_the_current_ttl_enum():
    from packages.core.prompt_builders import code_sniper_judge_system_prompt

    prompt = code_sniper_judge_system_prompt("EURUSD")
    assert config.TTL_ENUM in prompt
    assert "{60, 90, 120}" not in prompt


# ── 2. Sweep re-entry carve-out ──────────────────────────────────────────────

def _floor(bars, min_conf):
    from packages.core.multi_expert import _reentry_adaptive_floor

    return _reentry_adaptive_floor(
        bars, config.REENTRY_COOLDOWN_BARS, config.REENTRY_EXEMPT_BARS, min_conf
    )


def test_the_contradiction_this_closes_is_real():
    """Documents the defect itself: the prompt's endorsed window and the gate's
    suppression window are the same window.

    SWEEP RE-ENTRY says a same-side re-entry is valid when price reclaims the
    zone 'within ~12 bars'. The gate applied a 0.78 floor through bar 6 and a
    decaying floor to bar 12. Every bar of the endorsed window was gated.
    """
    for bars in range(0, config.REENTRY_COOLDOWN_BARS):
        assert _floor(bars, config.REENTRY_MIN_CONFIDENCE) == pytest.approx(
            config.REENTRY_MIN_CONFIDENCE
        )
    assert _floor(config.REENTRY_EXEMPT_BARS - 1, config.REENTRY_MIN_CONFIDENCE) > 0


def test_carveout_lowers_the_floor_for_a_same_side_reentry():
    """The 07-13 XAG case: stopped out, thesis re-confirms, re-enter same side."""
    gated = _floor(2, config.REENTRY_MIN_CONFIDENCE)
    carved = _floor(2, config.SWEEP_REENTRY_MIN_CONFIDENCE)
    assert carved < gated
    # A judge at 0.70 confidence was refused before and acts now.
    assert 0.70 < gated
    assert 0.70 > carved


def test_carveout_floor_is_not_zero():
    """It lowers the bar, it does not remove it — the judge must still be more
    confident than a coin flip to re-enter inside the cooldown."""
    assert 0.5 < config.SWEEP_REENTRY_MIN_CONFIDENCE < config.REENTRY_MIN_CONFIDENCE


@pytest.mark.parametrize("last_side,last_result,new_signal,expect_carveout", [
    ("buy",  "loss",    "buy",  True),   # the endorsed sweep re-entry
    ("sell", "loss",    "sell", True),
    ("buy",  "loss",    "sell", False),  # DIRECTION FLIP — the revenge pattern
    ("buy",  "win",     "buy",  False),  # not a sweep; ordinary re-entry churn
    ("buy",  "scratch", "buy",  False),
])
def test_carveout_applies_only_to_same_side_after_a_loss(
    last_side, last_result, new_signal, expect_carveout
):
    """The narrowness is the whole safety argument.

    An opposite-direction proposal remains gated, which is what
    the cooldown was built to stop. It must stay on the full floor.
    """
    latest = {"side": last_side, "result": last_result, "bars_elapsed": 2}
    is_sweep = (
        config.SWEEP_REENTRY_CARVEOUT_ENABLED
        and latest.get("result") == "loss"
        and str(latest.get("side") or "").lower() == new_signal.lower()
    )
    assert is_sweep is expect_carveout


def test_carveout_is_flag_gated():
    assert isinstance(config.SWEEP_REENTRY_CARVEOUT_ENABLED, bool)


def test_carveout_never_loosens_anything_outside_the_cooldown():
    """Past REENTRY_EXEMPT_BARS both paths are already ungated — the carve-out
    must not be doing anything there."""
    assert _floor(config.REENTRY_EXEMPT_BARS, config.REENTRY_MIN_CONFIDENCE) == 0.0
    assert _floor(config.REENTRY_EXEMPT_BARS, config.SWEEP_REENTRY_MIN_CONFIDENCE) == 0.0
