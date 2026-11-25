"""Tests for the 2026-06-11 trade-quality upgrade.

Covers: contract-size-correct USD P&L, TP2 runner (partial + chandelier),
executor stop auto-widen + RR-to-TP1 gate, ER regime hint, classifier fixes
(session / trend direction), exit-price sanity guard, and prompt sections.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from packages.core.trade_monitor import (
    evaluate_trade,
    EXIT_TP,
    EXIT_SL,
    PARTIAL_TP2,
    TRAIL_SL,
)
from packages.core.services.trade_monitor import _usd_pnl, _norm_side
from packages.core.market_structure import (
    MarketStructureAnalyzer,
    compute_htf_context,
    determine_trading_session,
)
from packages.core.code_sniper_executor import CodeSniperExecutor
from packages.core.models import Side
from packages.core.prompt_builders import (
    _recent_trades_section,
    build_expert_user_prompt,
    unified_expert_system_prompt,
    judge_system_prompt,
)
from packages.db.repositories.comprehensive_logging import _exit_price_sane


# ── USD P&L (contract size) ────────────────────────────────────────────────

def test_usd_pnl_contract_sizes():
    # BTC: contract size 1 → legacy formula preserved
    assert _usd_pnl("BTCUSD", "long", 100000.0, 100100.0, 0.05) == pytest.approx(5.0)
    # XAU: contract size 100 → the old formula under-reported 100×
    assert _usd_pnl("XAUUSD", "short", 4000.0, 3990.0, 0.02) == pytest.approx(20.0)
    # GBP: contract size 100,000
    assert _usd_pnl("GBPUSD", "long", 1.3400, 1.3410, 0.01) == pytest.approx(1.0)
    # Unknown asset → contract size 1.0 fallback, no crash
    assert _usd_pnl("UNKNOWN", "long", 10.0, 11.0, 2.0) == pytest.approx(2.0)


def test_norm_side():
    assert _norm_side("long") == "buy"
    assert _norm_side("short") == "sell"
    assert _norm_side("buy") == "buy"


# ── TP2 runner in evaluate_trade ──────────────────────────────────────────

def _runner_rules(**over):
    rules = {"tp1": 105.0, "tp": 110.0, "move_sl_to_be": True,
             "runner_enabled": True, "runner_trail_dist": 3.0}
    rules.update(over)
    return rules


def test_tp2_emits_partial_when_runner_enabled():
    state = {"direction": "long", "entry": 100.0, "sl": 97.0, "tp1_hit": True}
    action = evaluate_trade(state, 109.0, 110.5, 108.0, _runner_rules())
    assert action == PARTIAL_TP2


def test_tp2_full_exit_when_runner_disabled():
    state = {"direction": "long", "entry": 100.0, "sl": 97.0, "tp1_hit": True}
    action = evaluate_trade(state, 109.0, 110.5, 108.0,
                            _runner_rules(runner_enabled=False))
    assert action == EXIT_TP


def test_partial_tp2_fires_only_once():
    state = {"direction": "long", "entry": 100.0, "sl": 102.0,
             "tp1_hit": True, "tp2_hit": True, "runner_extreme": 110.5}
    # TP still inside the bar range, but tp2_hit gates it → chandelier path
    action = evaluate_trade(state, 109.0, 110.5, 108.5, _runner_rules())
    assert action != PARTIAL_TP2


def test_chandelier_ratchets_up_only():
    rules = _runner_rules()
    state = {"direction": "long", "entry": 100.0, "sl": 102.0,
             "tp1_hit": True, "tp2_hit": True, "runner_extreme": 110.5}
    # New high 115 → extreme 115, candidate SL = 112 > 102 → TRAIL_SL
    action = evaluate_trade(state, 114.0, 115.0, 111.0, rules)
    assert action == TRAIL_SL
    assert state["runner_extreme"] == 115.0
    assert state["_pending_trail_sl"] == pytest.approx(112.0)
    state["sl"] = 112.0  # monitor applies it
    # Lower high → extreme stays, candidate == current sl → no action
    action = evaluate_trade(state, 113.0, 113.5, 112.5, rules)
    assert action is None
    assert state["runner_extreme"] == 115.0


def test_runner_exits_on_stop_after_tp2():
    state = {"direction": "long", "entry": 100.0, "sl": 112.0,
             "tp1_hit": True, "tp2_hit": True, "runner_extreme": 115.0}
    action = evaluate_trade(state, 111.5, 113.0, 111.0, _runner_rules())
    assert action == EXIT_SL


def test_chandelier_short_side():
    rules = _runner_rules(tp1=95.0, tp=90.0)
    state = {"direction": "short", "entry": 100.0, "sl": 98.0,
             "tp1_hit": True, "tp2_hit": True, "runner_extreme": 89.0}
    # New low 85 → candidate SL = 88 < 98 → TRAIL_SL
    action = evaluate_trade(state, 86.0, 89.5, 85.0, rules)
    assert action == TRAIL_SL
    assert state["_pending_trail_sl"] == pytest.approx(88.0)


# ── Executor: stop auto-widen + RR-to-TP1 gate ────────────────────────────

def _result(direction="buy", entry=100000.0, sl=99900.0, tp1=100800.0, tp2=101400.0):
    return {
        "direction": direction, "entry": entry, "stop_loss": sl,
        "take_profit_1": tp1, "take_profit_2": tp2,
        "confidence": 0.7, "rationale": "test",
    }


def _make_signal():
    sig = MagicMock()
    sig.ttl_minutes = 180
    sig.confidence = 0.7
    return sig


def test_executor_widens_stop_to_floor(monkeypatch):
    ex = CodeSniperExecutor()
    monkeypatch.setattr(CodeSniperExecutor, "_calculate_position_size",
                        lambda self, *a, **k: 0.01)
    ctx = {"sl_floor": 600.0, "risk_pct": 0.01}
    decision = ex._convert_to_decision(
        _result(), "BTCUSD", "BTCUSD", _make_signal(), ctx,
        risk_service=MagicMock(),
    )
    # 0.1% stop widened to the 600-unit floor; RR(TP1)=800/600=1.33 passes
    assert decision.sl == pytest.approx(99400.0)
    assert decision.quantity > 0


def test_executor_rejects_rr_tp1_below_min(monkeypatch):
    ex = CodeSniperExecutor()
    monkeypatch.setattr(CodeSniperExecutor, "_calculate_position_size",
                        lambda self, *a, **k: 0.01)
    # After widening to 600, TP1 only 400 away → RR 0.67 < 1.2 → reject
    ctx = {"sl_floor": 600.0, "risk_pct": 0.01}
    decision = ex._convert_to_decision(
        _result(tp1=100400.0, tp2=101400.0), "BTCUSD", "BTCUSD",
        _make_signal(), ctx, risk_service=MagicMock(),
    )
    assert decision.quantity == 0


def test_stop_floor_cannot_repair_wrong_side_stop(monkeypatch):
    ex = CodeSniperExecutor()
    monkeypatch.setattr(CodeSniperExecutor, "_calculate_position_size",
                        lambda self, *a, **k: 0.01)
    decision = ex._convert_to_decision(
        _result(sl=100100.0), "BTCUSD", "BTCUSD", _make_signal(),
        {"sl_floor": 600.0, "risk_pct": 0.01}, risk_service=MagicMock(),
    )
    assert decision.quantity == 0
    assert "not protective" in decision.notes


def test_executor_no_floor_keeps_stop(monkeypatch):
    ex = CodeSniperExecutor()
    monkeypatch.setattr(CodeSniperExecutor, "_calculate_position_size",
                        lambda self, *a, **k: 0.01)
    decision = ex._convert_to_decision(
        _result(sl=99400.0), "BTCUSD", "BTCUSD", _make_signal(),
        {"risk_pct": 0.01}, risk_service=MagicMock(),
    )
    assert decision.sl == pytest.approx(99400.0)


def test_rr_tp1_gate_in_validate_levels():
    ex = CodeSniperExecutor()
    # risk 600, TP1 600 away → RR 1.0 < 1.2 → reject
    ok, *_ = ex._validate_trade_levels(
        100000.0, 99400.0, 100600.0, 102000.0, Side.BUY, "BTCUSD", "BTCUSD")
    assert ok is False
    # TP1 800 away → RR 1.33 → pass
    ok, *_ = ex._validate_trade_levels(
        100000.0, 99400.0, 100800.0, 102000.0, Side.BUY, "BTCUSD", "BTCUSD")
    assert ok is True


# ── ER regime hint ─────────────────────────────────────────────────────────

def _h1(closes):
    return [{"time": i, "open": c, "high": c + 1, "low": c - 1, "close": c}
            for i, c in enumerate(closes)]


def test_er_trending_vs_chop():
    trending = compute_htf_context({"1h": _h1([100.0 + 10 * i for i in range(30)])})
    assert trending.get("er_1h", 0) > 0.9
    assert trending.get("regime_hint") == "trending"

    chop = compute_htf_context({"1h": _h1([100.0 + (10 if i % 2 else 0) for i in range(30)])})
    assert chop.get("er_1h", 1) < 0.25
    assert chop.get("regime_hint") == "chop"


def test_er_absent_with_short_history():
    out = compute_htf_context({"1h": _h1([100.0, 101.0, 102.0])})
    assert "er_1h" not in out


# ── Classifier fixes ───────────────────────────────────────────────────────

def test_trading_sessions_cover_all_hours():
    assert determine_trading_session("2026-06-11T08:00:00+00:00") == "london"
    assert determine_trading_session("2026-06-11T13:00:00+00:00") == "london_ny"
    assert determine_trading_session("2026-06-11T18:00:00+00:00") == "ny"
    assert determine_trading_session("2026-06-11T22:30:00+00:00") == "asian"
    assert determine_trading_session("2026-06-11T03:00:00+00:00") == "asian"


def test_trend_direction_price_normalized():
    import numpy as np
    analyzer = MarketStructureAnalyzer()
    # GBP-scale uptrend: +0.01% per candle — the old absolute threshold
    # (slope > 0.001 in price units) called this "sideways" forever.
    gbp_up = np.array([1.34 * (1 + 0.0001 * i) for i in range(40)])
    assert analyzer._determine_trend_direction(gbp_up) == "bullish"
    gbp_flat = np.array([1.34 + 0.000001 * ((-1) ** i) for i in range(40)])
    assert analyzer._determine_trend_direction(gbp_flat) == "sideways"
    btc_down = np.array([100000.0 * (1 - 0.0002 * i) for i in range(40)])
    assert analyzer._determine_trend_direction(btc_down) == "bearish"


# ── Exit sanity guard ──────────────────────────────────────────────────────

def test_exit_price_sane():
    gbp_doc = {"entry_price": 1.3400}
    assert _exit_price_sane(gbp_doc, 1.3410) is True
    # A gold price written into a GBP doc (the real corruption observed)
    assert _exit_price_sane(gbp_doc, 4355.0) is False
    assert _exit_price_sane({"entry_price": None}, 4355.0) is True  # nothing to compare


# ── Prompt sections ────────────────────────────────────────────────────────

def test_recent_trades_section_renders():
    assert _recent_trades_section(None) == ""
    assert _recent_trades_section([]) == ""
    sec = _recent_trades_section([{"side": "sell", "outcome": "SL", "pnl": -4.2}])
    assert "RECENT_TRADES" in sec
    assert '"side":"sell"' in sec
    assert '"pnl":-4.2' in sec


def test_expert_prompt_includes_new_blocks():
    prompt = build_expert_user_prompt(
        [[1, 2, 3, 4, 5, 6]], [[1, 2, 3, 4, 5, 6]], asset="BTCUSD",
        htf_context={"htf_trend": "down", "regime_hint": "chop"},
        stop_hint="HARD FLOOR: 600 units",
        recent_trades=[{"side": "sell", "outcome": "SL"}],
    )
    assert "HTF_CONTEXT" in prompt
    assert "STOP_GUIDANCE" in prompt
    assert "RECENT_TRADES" in prompt


def test_public_system_prompts_preserve_schema_and_risk_contracts():
    expert = unified_expert_system_prompt()
    judge = judge_system_prompt()
    assert "JSON" in expert and "JSON" in judge
    assert "RR" in expert and "RR" in judge
    assert "ttl_minutes" in expert and "ttl_minutes" in judge
    assert "flat" in expert and "flat" in judge
    assert "volatility floor" not in expert
    assert "volatility floor" not in judge
