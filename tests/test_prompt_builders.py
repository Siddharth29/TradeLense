"""Unit tests for prompt builders ensuring guard-rail instructions."""

import pytest

from packages.core import prompt_builders as pb


@pytest.fixture(params=[
    pb.unified_expert_system_prompt,
    pb.judge_system_prompt,
    pb.code_sniper_judge_system_prompt,
])
def active_prompt_func(request):
    return request.param


def test_active_prompts_non_empty(active_prompt_func):
    """Active prompt functions must return non-empty strings."""
    text = active_prompt_func()
    assert isinstance(text, str)
    assert len(text) > 50


def test_unified_expert_prompt_includes_guardrails():
    text = pb.unified_expert_system_prompt()
    lowered = text.lower()

    assert "rr" in lowered
    assert "ttl_minutes" in text
    assert "json" in lowered
    assert "opposite directions" in lowered


def test_judge_prompt_includes_guardrails():
    text = pb.judge_system_prompt()
    lowered = text.lower()

    assert "rr" in lowered
    assert "json" in lowered
    assert "flat" in lowered


def test_code_sniper_prompt_includes_code_template():
    text = pb.code_sniper_judge_system_prompt()
    lowered = text.lower()

    assert "def run(" in text
    assert "candles_5" in text
    assert "direction" in text
    assert "stop_loss" in text
    assert "take_profit" in text
    assert "json" in lowered


def test_code_sniper_prompt_includes_empty_block_guardrail():
    """The prompt must instruct AI to avoid empty if/else blocks."""
    text = pb.code_sniper_judge_system_prompt()
    lowered = text.lower()
    # Must mention the empty-block issue or use of 'pass'
    assert "pass" in lowered


def test_active_prompts_are_distinct():
    """All active prompts must be distinct strings."""
    prompts = [
        pb.unified_expert_system_prompt(),
        pb.judge_system_prompt(),
        pb.code_sniper_judge_system_prompt(),
    ]
    assert len(set(prompts)) == len(prompts), "All active prompts must be unique"


def test_expert_user_prompt_includes_asset_one_liner():
    prompt = pb.build_expert_user_prompt(
        [["2025-01-01T09:15", 1, 2, 0.5, 1.5, 100]],
        [["2025-01-01T09:15", 1, 2, 0.5, 1.5, 100]],
        asset="GBPUSD",
    )

    assert "ASSET: GBPUSD - Forex major pair, British pound vs US dollar." in prompt


def test_judge_user_prompt_includes_asset_one_liner():
    prompt = pb.build_judge_user_prompt(
        proposals=[{"signal": "buy", "confidence": 0.7}],
        advanced_context={"market": "test"},
        recent_candles_5m=[["2025-01-01T09:15", 1, 2, 0.5, 1.5, 100]],
        recent_candles_15m=[["2025-01-01T09:15", 1, 2, 0.5, 1.5, 100]],
        use_code_sniper=True,
        asset="XAUUSD",
    )

    assert "ASSET: XAUUSD - Spot gold priced in USD." in prompt


def test_judge_user_prompt_code_sniper_action_line():
    """Code sniper mode must include the 7-field return requirement."""
    prompt = pb.build_judge_user_prompt(
        proposals=[{"signal": "buy", "confidence": 0.7}],
        advanced_context={},
        recent_candles_5m=[],
        use_code_sniper=True,
    )
    assert "take_profit_1" in prompt
    assert "take_profit_2" in prompt
    assert "direction" in prompt


def test_judge_user_prompt_standard_action_line():
    """Standard judge mode must include buy/sell/flat instruction."""
    prompt = pb.build_judge_user_prompt(
        proposals=[{"signal": "buy", "confidence": 0.7}],
        advanced_context={},
        recent_candles_5m=[],
        use_code_sniper=False,
    )
    assert "buy" in prompt.lower() or "sell" in prompt.lower()


# ---------- Float hygiene (PROMPT_FLOAT_DECIMALS, 2026-06-12) ----------

def test_round_floats_kills_repr_artifacts():
    assert pb._round_floats(1.3412899999999999) == 1.34129
    assert pb._round_floats(1.3330600000000001) == 1.33306
    # ints, strings, None untouched
    assert pb._round_floats({"a": 1, "b": "x", "c": None}) == {"a": 1, "b": "x", "c": None}
    # nested lists/dicts/tuples
    out = pb._round_floats({"zones": [(1.3412899999999999, "sup")], "n": [0.1234567899]})
    assert out == {"zones": [[1.34129, "sup"]], "n": [0.123457]}


def test_expert_user_prompt_has_no_long_floats():
    c5 = [["2026-06-12T10:00:00", 1.3412899999999999, 1.3413, 1.3412, 1.3412899999999999, 100.0]]
    c15 = [["2026-06-12T10:00:00", 1.3330600000000001, 1.3334, 1.3330, 1.3332899999999999, 300.0]]
    text = pb.build_expert_user_prompt(c5, c15, asset="GBPUSD")
    assert "1.34129" in text
    assert "1.3412899999999999" not in text
    assert "1.3330600000000001" not in text


def test_judge_user_prompt_rounds_context_and_proposals():
    proposals = [{"entry": 1.3412899999999999, "stop": 1.3399999999999998}]
    context = {"sr_levels": [1.3330600000000001], "atr": 0.0012345678901234}
    text = pb.build_judge_user_prompt(
        proposals, context,
        recent_candles_5m=[["t", 1.3412899999999999, 1.0, 1.0, 1.0, 1.0]],
        asset="GBPUSD",
    )
    assert "1.3412899999999999" not in text
    assert "1.3330600000000001" not in text
    assert "0.001235" in text  # 6 dp keeps derived-stat precision
