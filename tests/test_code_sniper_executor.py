from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from packages.core import code_sniper_executor
from packages.core.code_sniper_executor import (
    CodeSniperExecutor,
    _fill_empty_blocks,
    _min_target_distances,
    MIN_DISTANCE_TO_TP_PCT,
    MIN_DISTANCE_TO_SL_PCT,
)
from packages.core.schemas_trade import CodeSniperSignal
from packages.core.simple_sandbox import run_in_sandbox
from packages.core.strategy_candle_gate import CandleContractError


class DummyRisk:
    def __init__(self, funds: float = 100000.0) -> None:
        self._funds = funds

    def available_funds(self) -> float:
        return self._funds


@pytest.mark.parametrize(("stop", "expected_lot"), [(64_000, 0), (64_300, 0.01), (64_100, 0)])
def test_live_minimum_lot_must_fit_exact_stop_risk_budget(stop, expected_lot):
    quantity = CodeSniperExecutor()._calculate_position_size(
        67_000, stop, {"risk_pct": 0.015}, DummyRisk(1800), "BTCUSD", "BTCUSD"
    )
    assert quantity == expected_lot


# Production expects 40 historical + 1 current = 41 candles total
# (aligned with the judge template, 2026-06-11 quality upgrade).
_TRIGGER_TIME = datetime(2025, 10, 7, 9, 15, tzinfo=timezone.utc)
_BASE_CANDLE = {"time": _TRIGGER_TIME.isoformat(), "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10}
_HISTORY = [
    {**_BASE_CANDLE, "time": (_TRIGGER_TIME - timedelta(minutes=5 * offset)).isoformat()}
    for offset in range(40, 0, -1)
]


@pytest.fixture(autouse=True)
def freeze_executor_clock(monkeypatch):
    class FixtureDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return (_TRIGGER_TIME + timedelta(minutes=5)).astimezone(tz)

    monkeypatch.setattr(code_sniper_executor, "datetime", FixtureDatetime)


def _make_signal(code: str, confidence: float = 0.5) -> CodeSniperSignal:
    return CodeSniperSignal(
        code=code,
        input_schema={},
        output_schema={},
        metadata={},
        ttl_minutes=90,
        confidence=confidence,
    )


def _run(code: str, risk_funds: float = 100_000.0, asset: str = "TEST",
         current_candle: dict | None = None) -> object:
    execu = CodeSniperExecutor()
    return execu.execute_strategy(
        code_signal=_make_signal(code),
        current_candle=current_candle or _BASE_CANDLE,
        historical_candles=_HISTORY,
        context={},
        asset=asset,
        symbol=asset,
        risk_service=DummyRisk(risk_funds),
    )


def _candle_at(price: float) -> dict:
    """A trigger candle closing AT the judged entry, so the fill-fidelity gate
    (which substitutes the trigger close for the ENTRY constant) sees a clean
    zero-deviation fill and the downstream gates get exercised."""
    return {"time": _TRIGGER_TIME.isoformat(), "open": price, "high": price,
            "low": price, "close": price, "volume": 10}


# ---------------------------------------------------------------------------
# Strategy execution tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad_code, expected_msg",
    [
        (
            # run returns non-dict → must raise
            """
def run(candles_5, now):
    return 123
""",
            "Strategy must return a dictionary",
        ),
        (
            # run returns None for a required price field → TypeError must be caught clearly
            """
def run(candles_5, now):
    return {
        "direction": "buy",
        "entry": None,
        "stop_loss": 95.0,
        "take_profit_1": 110.0,
        "take_profit_2": 120.0,
        "confidence": 0.8,
        "rationale": "none price test"
    }
""",
            "None for required numeric field",
        ),
    ],
)
def test_executor_fail_fast_on_invalid_result(bad_code: str, expected_msg: str):
    with pytest.raises(Exception) as exc:
        _run(bad_code)
    assert expected_msg in str(exc.value)


def test_executor_trade_path_and_risk_sizing():
    good_code = """
def run(candles_5, now):
    return {
        "direction": "buy",
        "entry": 100.0,
        "stop_loss": 95.0,
        "take_profit_1": 112.0,
        "take_profit_2": 120.0,
        "confidence": 0.8,
        "rationale": "test"
    }
"""
    # Registry asset (BTCUSD: contract_size 1.0): full trade path with
    # contract-aware sizing succeeds.
    decision = _run(good_code, risk_funds=100_000.0, asset="BTCUSD",
                    current_candle=_candle_at(100.0))
    assert decision.quantity > 0
    assert decision.entry == 100.0
    assert decision.sl == 95.0


def test_executor_unknown_asset_fails_closed():
    """No ASSET_PROFILES entry ⇒ no contract specs ⇒ no order. The old
    fallback formula sized unknown symbols with dimensionally-wrong math."""
    good_code = """
def run(candles_5, now):
    return {
        "direction": "buy",
        "entry": 100.0,
        "stop_loss": 95.0,
        "take_profit_1": 112.0,
        "take_profit_2": 120.0,
        "confidence": 0.8,
        "rationale": "test"
    }
"""
    decision = _run(good_code, risk_funds=100_000.0, asset="TEST",
                    current_candle=_candle_at(100.0))
    assert decision.quantity == 0.0


# ---------------------------------------------------------------------------
# Bug 1 — IndentationError: empty if/else blocks in GPT-generated code
# ---------------------------------------------------------------------------

def test_fill_empty_blocks_inserts_pass():
    """_fill_empty_blocks must add 'pass' so ast.parse succeeds."""
    import ast

    broken = (
        "def run(candles_5, now):\n"
        "    price = candles_5[-1][4]\n"
        "    if price > 0:\n"          # empty block — no body
        "\n"
        "    return {'direction': 'hold'}\n"
    )
    fixed = _fill_empty_blocks(broken)
    # Must be parseable now
    ast.parse(fixed)
    assert "pass" in fixed


def test_executor_handles_empty_if_block():
    """GPT code with an empty 'if' block must be auto-repaired and execute."""
    code = (
        "def run(candles_5, now):\n"
        "    price = candles_5[-1][4]\n"
        "    if price > 0:\n"
        "\n"
        "    return {\n"
        "        'direction': 'hold',\n"
        "        'entry': 0.0,\n"
        "        'stop_loss': 0.0,\n"
        "        'take_profit_1': 0.0,\n"
        "        'take_profit_2': 0.0,\n"
        "        'confidence': 0.0,\n"
        "        'rationale': 'empty if repaired'\n"
        "    }\n"
    )
    decision = _run(code)
    # hold → quantity == 0 but no exception
    assert decision.quantity == 0.0


def test_executor_handles_empty_else_block():
    """Empty else block is also auto-repaired."""
    code = (
        "def run(candles_5, now):\n"
        "    price = candles_5[-1][4]\n"
        "    if price > 9999999:\n"
        "        pass\n"
        "    else:\n"
        "\n"                          # empty else — no body
        "    return {\n"
        "        'direction': 'hold',\n"
        "        'entry': 0.0,\n"
        "        'stop_loss': 0.0,\n"
        "        'take_profit_1': 0.0,\n"
        "        'take_profit_2': 0.0,\n"
        "        'confidence': 0.0,\n"
        "        'rationale': 'empty else repaired'\n"
        "    }\n"
    )
    decision = _run(code)
    assert decision.quantity == 0.0


# ---------------------------------------------------------------------------
# Bug 2 — TypeError: float(None) when GPT returns None for a price field
# ---------------------------------------------------------------------------

def test_executor_none_stop_loss_raises_clear_error():
    """None stop_loss must produce a clear ValueError, not bare TypeError."""
    code = """
def run(candles_5, now):
    return {
        "direction": "buy",
        "entry": 100.0,
        "stop_loss": None,
        "take_profit_1": 110.0,
        "take_profit_2": 120.0,
        "confidence": 0.8,
        "rationale": "none sl"
    }
"""
    with pytest.raises(Exception) as exc:
        _run(code)
    assert "None for required numeric field" in str(exc.value)


def test_executor_rejects_missing_candle_price():
    with pytest.raises(CandleContractError, match="'open' is missing"):
        _run("def run(candles_5, now): return {'direction': 'hold'}",
             current_candle={**_BASE_CANDLE, "open": None})


def test_executor_keeps_monitoring_when_hold_omits_levels():
    decision = _run("def run(candles_5, now): return {'direction': 'hold', 'rationale': 'waiting'}")
    assert decision.quantity == 0
    assert decision.notes.endswith("waiting")


def test_executor_rejects_unknown_direction():
    code = "def run(candles_5, now): return {'direction': 'unexpected'}"
    with pytest.raises(RuntimeError, match="Unsupported strategy direction"):
        _run(code)


# ---------------------------------------------------------------------------
# Bug 3 — NameError: candles_15m not injected into sandbox namespace
# ---------------------------------------------------------------------------

def test_sandbox_candles_15m_is_defined():
    """candles_15m must be available in the sandbox namespace (as empty list)."""
    source = """
def run(candles_5, now):
    # Reference candles_15m at call time — must not raise NameError
    _ = len(candles_15m)
    return {
        "direction": "hold",
        "entry": 0.0,
        "stop_loss": 0.0,
        "take_profit_1": 0.0,
        "take_profit_2": 0.0,
        "confidence": 0.0,
        "rationale": "candles_15m available"
    }
"""
    result = run_in_sandbox(source, candles_5=[], now="2025-01-01T09:15:00")
    assert result["direction"] == "hold"


# ---------------------------------------------------------------------------
# Per-asset distance floors (GBPUSD/XAUUSD fix)
#
# The "large candle protection" filter used one flat % floor (0.3% TP / 0.1%
# SL) for every asset. FX/metals move ~10x less in % terms than crypto, so on
# 2026-06-04 it rejected 10/10 GBPUSD setups and 5 XAUUSD setups (zero GBP
# trades all day). These tests pin the new per-asset behaviour and guard the
# regression in the *real* execute_strategy() flow, not just the helper.
# ---------------------------------------------------------------------------

import pytest as _pytest  # noqa: E402  (local alias; file already imports pytest)


def _run_levels(asset, entry, sl, tp1, tp2, direction="buy", risk_funds=100_000.0):
    """Run execute_strategy() for a strategy that returns fixed price levels."""
    code = (
        "def run(candles_5, now):\n"
        "    return {\n"
        f"        'direction': '{direction}',\n"
        f"        'entry': {entry},\n"
        f"        'stop_loss': {sl},\n"
        f"        'take_profit_1': {tp1},\n"
        f"        'take_profit_2': {tp2},\n"
        "        'confidence': 0.7,\n"
        "        'rationale': 'per-asset distance test'\n"
        "    }\n"
    )
    execu = CodeSniperExecutor()
    return execu.execute_strategy(
        code_signal=_make_signal(code),
        current_candle=_candle_at(entry),
        historical_candles=_HISTORY,
        context={},
        asset=asset,
        symbol=asset,
        risk_service=DummyRisk(risk_funds),
    )


def test_min_target_distances_defaults_for_crypto_and_unknown():
    """Assets without an override fall back to the flat defaults."""
    assert _min_target_distances("BTCUSD") == (MIN_DISTANCE_TO_TP_PCT, MIN_DISTANCE_TO_SL_PCT)
    assert _min_target_distances("TEST") == (MIN_DISTANCE_TO_TP_PCT, MIN_DISTANCE_TO_SL_PCT)
    assert _min_target_distances("") == (MIN_DISTANCE_TO_TP_PCT, MIN_DISTANCE_TO_SL_PCT)


def test_min_target_distances_per_asset_overrides():
    """Only GBPUSD is overridden. BTCUSD and XAUUSD intentionally stay on the
    default floor so their (good) current entry/exit behaviour is preserved."""
    assert _min_target_distances("GBPUSD") == (0.05, 0.03)
    assert _min_target_distances("XAUUSD") == (MIN_DISTANCE_TO_TP_PCT, MIN_DISTANCE_TO_SL_PCT)
    assert _min_target_distances("BTCUSD") == (MIN_DISTANCE_TO_TP_PCT, MIN_DISTANCE_TO_SL_PCT)


def test_min_target_distances_case_and_suffix_tolerant():
    """Lookup is case-insensitive and tolerates the broker's demo 'm' suffix."""
    assert _min_target_distances("gbpusd") == (0.05, 0.03)
    assert _min_target_distances("GBPUSDm") == (0.05, 0.03)
    # an unlisted asset (even with the demo suffix) falls back to the defaults
    assert _min_target_distances("xauusdm") == (MIN_DISTANCE_TO_TP_PCT, MIN_DISTANCE_TO_SL_PCT)


def test_gbpusd_realistic_scalp_now_executes():
    """The exact GBPUSD setup rejected on 2026-06-04 (TP1 ~0.079%, SL ~0.04%)
    must now produce a trade under the FX floor (TP 0.05% / SL 0.03%)."""
    entry = 1.34206
    sl = round(entry * (1 - 0.0004), 5)    # ~0.04% below  (>= 0.03 floor)
    tp1 = round(entry * (1 + 0.00079), 5)  # ~0.079% above (>= 0.05 floor)
    tp2 = round(entry * (1 + 0.00153), 5)  # ~0.153% above -> RR ~= 3.8
    decision = _run_levels("GBPUSD", entry, sl, tp1, tp2, direction="buy")
    assert decision.quantity > 0, "GBPUSD realistic scalp should now produce a trade"
    assert decision.entry == _pytest.approx(entry)


def test_xauusd_stays_on_default_floor_unchanged():
    """Regression guard: gold deliberately keeps the default floor — its current
    entry/exit behaviour is good and must NOT change with the GBPUSD fix. A 0.09%
    SL (< 0.1% default) stays rejected, exactly as before."""
    entry = 4450.0
    sl = round(entry * (1 - 0.0009), 2)    # 0.09% < 0.1% default SL floor
    tp1 = round(entry * (1 + 0.00258), 2)  # 0.258%
    tp2 = round(entry * (1 + 0.0036), 2)   # 0.36%
    decision = _run_levels("XAUUSD", entry, sl, tp1, tp2, direction="buy")
    assert decision.quantity == 0.0, "XAUUSD must remain on the default floor (gold unchanged)"


def test_same_fx_distances_still_rejected_for_crypto_default():
    """A 0.079%/0.04% setup is valid for GBPUSD but must STILL be rejected on
    the default (crypto) floor — proves the change is scoped, not global."""
    entry = 1.34206
    sl = round(entry * (1 - 0.0004), 5)
    tp1 = round(entry * (1 + 0.00079), 5)
    tp2 = round(entry * (1 + 0.00153), 5)
    decision = _run_levels("BTCUSD", entry, sl, tp1, tp2, direction="buy")
    assert decision.quantity == 0.0, "tiny % distances must still be rejected on the default floor"


def test_gbpusd_below_new_floor_still_rejected():
    """The floor is lowered, not removed: GBPUSD distances under the new floor
    (TP 0.05% / SL 0.03%) must still be rejected."""
    entry = 1.34206
    sl = round(entry * (1 - 0.0001), 5)    # 0.01% < 0.03 floor
    tp1 = round(entry * (1 + 0.0002), 5)   # 0.02% < 0.05 floor
    tp2 = round(entry * (1 + 0.0004), 5)
    decision = _run_levels("GBPUSD", entry, sl, tp1, tp2, direction="buy")
    assert decision.quantity == 0.0, "sub-floor GBPUSD distances must still be rejected"


def test_sandbox_candles_15m_module_level_reference():
    """GPT code that reads candles_15m at module level (outside run()) must not NameError."""
    source = """
# Some GPT strategies inspect candle arrays at module level
_c15_len = len(candles_15m)

def run(candles_5, now):
    return {
        "direction": "hold",
        "entry": 0.0,
        "stop_loss": 0.0,
        "take_profit_1": 0.0,
        "take_profit_2": 0.0,
        "confidence": 0.0,
        "rationale": f"15m candles seen: {_c15_len}"
    }
"""
    result = run_in_sandbox(source, candles_5=[], now="2025-01-01T09:15:00")
    assert result["direction"] == "hold"



# ---------------------------------------------------------------------------
# LP-09 (2026-07-08 audit) — zombie strategies from same-indent block bodies.
# Post the 2026-07-06 prompt redesign, Sonnet 4.6 emitted `if rr<1.2:` with the
# `return` at the SAME indent (IndentationError). The old pass-fill repair made
# the orphaned `return hold` unconditional, so the strategy monitored its whole
# TTL as a no-op — 32% of judge programs on 07-07, incl. two USOIL winners.
# ---------------------------------------------------------------------------

from packages.core.code_sniper_executor import (  # noqa: E402
    _indent_orphan_returns,
    validate_judge_code,
)

# The real broken shape from the 07-07 USOIL 15:30 program (abridged): a guard
# whose return sits at the opener's indent, then a firing branch further down.
_ZOMBIE_GUARD_CODE = (
    "def run(candles_5,now):\n"
    " entry=70.10\n"
    " sl=69.82\n"
    " tp1=70.44\n"
    " tp2=70.65\n"
    " rr=abs(tp1-entry)/abs(entry-sl)\n"
    " if rr<1.2:\n"
    " return {'direction':'hold','entry':entry,'stop_loss':sl,'take_profit_1':tp1,"
    "'take_profit_2':tp2,'confidence':0.0,'rationale':'RR below threshold'}\n"
    " curr_c=candles_5[-1][4]\n"
    " if curr_c>entry:\n"
    "  return {'direction':'buy','entry':entry,'stop_loss':sl,'take_profit_1':tp1,"
    "'take_profit_2':tp2,'confidence':0.5,'rationale':'fires'}\n"
    " return {'direction':'hold','entry':entry,'stop_loss':sl,'take_profit_1':tp1,"
    "'take_profit_2':tp2,'confidence':0.4,'rationale':'monitoring'}\n"
)


def test_indent_orphan_return_produces_valid_code():
    import ast

    fixed = _indent_orphan_returns(_ZOMBIE_GUARD_CODE)
    ast.parse(fixed)  # must not raise
    # the guard return is now INSIDE the if (deeper indent than the opener)
    lines = fixed.splitlines()
    guard_idx = next(i for i, ln in enumerate(lines) if "rr<1.2" in ln)
    opener_indent = len(lines[guard_idx]) - len(lines[guard_idx].lstrip())
    body_indent = len(lines[guard_idx + 1]) - len(lines[guard_idx + 1].lstrip())
    assert body_indent > opener_indent


def test_repaired_zombie_still_fires_buy():
    """The semantics-preserving repair must keep the firing branch alive —
    the exact behavior the old pass-fill repair destroyed."""
    ok, code, detail = validate_judge_code(_ZOMBIE_GUARD_CODE)
    assert ok and detail is not None
    result = run_in_sandbox(
        code,
        candles_5=[["2026-07-07T15:40", 70.08, 70.30, 70.03, 70.25, 100]] * 11,
        now="2026-07-07T15:45:05",
    )
    assert result["direction"] == "buy"


def test_repaired_zombie_guard_branch_intact():
    """With RR below threshold the guard must still return the guard hold —
    i.e. the return really is the if-body, not unconditional code."""
    bad_rr = _ZOMBIE_GUARD_CODE.replace("tp1=70.44", "tp1=70.15")  # RR ~0.18
    ok, code, _ = validate_judge_code(bad_rr)
    assert ok
    result = run_in_sandbox(
        code,
        candles_5=[["2026-07-07T15:40", 70.08, 70.30, 70.03, 70.25, 100]] * 11,
        now="2026-07-07T15:45:05",
    )
    assert result["direction"] == "hold"
    assert "RR below threshold" in result["rationale"]


def test_validate_passes_valid_code_unchanged():
    good = (
        "def run(candles_5, now):\n"
        "    return {'direction': 'hold', 'entry': 0.0, 'stop_loss': 0.0,\n"
        "            'take_profit_1': 0.0, 'take_profit_2': 0.0,\n"
        "            'confidence': 0.0, 'rationale': 'no setup'}\n"
    )
    ok, code, detail = validate_judge_code(good)
    assert ok and code == good and detail is None


def test_validate_rejects_orphan_nonreturn_body():
    """A same-indent NON-return statement after an opener is ambiguous —
    pass-filling it would change semantics, so registration must refuse."""
    ambiguous = (
        "def run(candles_5, now):\n"
        " total = 0\n"
        " for c in candles_5:\n"
        " total = total + c[4]\n"
        " return {'direction': 'hold', 'entry': 0.0, 'stop_loss': 0.0,\n"
        "         'take_profit_1': 0.0, 'take_profit_2': 0.0,\n"
        "         'confidence': 0.0, 'rationale': str(total)}\n"
    )
    ok, _, detail = validate_judge_code(ambiguous)
    assert not ok
    assert "orphaned block body" in detail


def test_validate_still_fills_genuinely_empty_block():
    """An empty block followed by a dedent/continuator keeps the old safe
    pass-fill repair."""
    empty_else = (
        "def run(candles_5, now):\n"
        "    price = candles_5[-1][4]\n"
        "    if price > 0:\n"
        "        d = 'hold'\n"
        "    else:\n"
        "    return {'direction': 'hold', 'entry': 0.0, 'stop_loss': 0.0,\n"
        "            'take_profit_1': 0.0, 'take_profit_2': 0.0,\n"
        "            'confidence': 0.0, 'rationale': d}\n"
    )
    # else: + same-indent return → the return is re-indented as the else body
    ok, code, detail = validate_judge_code(empty_else)
    assert ok and detail is not None
    import ast

    ast.parse(code)


def test_sanitize_gpt_code_repairs_orphan_return():
    """The runtime sanitize path applies the same repair, so pre-existing
    strategies (armed before the registration gate shipped) also recover."""
    executor = CodeSniperExecutor()
    import ast

    fixed = executor._sanitize_gpt_code(_ZOMBIE_GUARD_CODE)
    ast.parse(fixed)
