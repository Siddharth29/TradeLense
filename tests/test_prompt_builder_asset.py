from core import prompt_builder


def _cfg():
    return {
        "c15": "C15",
        "c5": "C5",
        "timestamp": "TS",
        "agent_id": "A1",
        "candle_count_5m": 42,
        "candle_count_15m": 11,
    }


def test_prompts_differ_and_meta():
    cfg = _cfg()
    prompt_index, hash_index, meta_index = prompt_builder.build_prompt("USTEC", cfg)
    prompt_fx, hash_fx, meta_fx = prompt_builder.build_prompt("XAUUSD", cfg)

    assert "C15" in prompt_index
    assert "C5" in prompt_index
    assert "TS" in prompt_index
    assert "A1" in prompt_index
    assert "42" in prompt_index
    assert "11" in prompt_index
    assert "A1" in prompt_fx
    assert "42" in prompt_fx
    assert "11" in prompt_fx
    assert prompt_index != prompt_fx
    assert meta_index == {"asset": "USTEC", "template": "index"}
    assert meta_fx == {"asset": "XAUUSD", "template": "fx_gold"}
    assert hash_index != hash_fx


def test_hash_determinism():
    cfg = _cfg()
    _, h1, _ = prompt_builder.build_prompt("USTEC", cfg)
    _, h2, _ = prompt_builder.build_prompt("USTEC", cfg)
    assert h1 == h2
