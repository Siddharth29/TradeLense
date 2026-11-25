"""Trend agreement + aligned touch-fill (2026-07-29).

Evidence and scope live in the ``TREND_AGREEMENT_ENABLED`` block of
``packages/core/config.py``. These tests pin the contract:

  * agreement means UNANIMOUS trend_1h/trend_4h/trend_1d — nothing weaker
  * a touch-fill fires only for a setup trading WITH that agreement, only when
    the bar's range actually contains the judged entry, and only where the
    per-asset mode says "fill"
  * "log" observes without trading; "off" is inert
  * the promotion replaces the trigger condition ONLY — the fill-fidelity gate,
    SL floor, RR gate and sizing still decide the trade
"""

import pytest

from packages.core import config
from packages.core.code_sniper_executor import CodeSniperExecutor
from packages.core.models import Side


# ── trend_agreement_from ────────────────────────────────────────────────

def test_unanimous_trends_agree():
    assert config.trend_agreement_from(
        {"trend_1h": "down", "trend_4h": "down", "trend_1d": "down"}) == "down"
    assert config.trend_agreement_from(
        {"trend_1h": "up", "trend_4h": "up", "trend_1d": "up"}) == "up"


def test_two_of_three_is_not_agreement():
    """The freeze split is 3/3 vs everything else — '2 of 3' was the WORST
    bucket (n=60, -$107.83), so it must never read as agreement."""
    assert config.trend_agreement_from(
        {"trend_1h": "up", "trend_4h": "down", "trend_1d": "down"}) is None


def test_missing_timeframe_is_not_agreement():
    """Silence is not consensus: an absent 1d trend cannot complete a 3/3."""
    assert config.trend_agreement_from({"trend_1h": "up", "trend_4h": "up"}) is None
    assert config.trend_agreement_from({}) is None
    assert config.trend_agreement_from(None) is None


def test_non_directional_labels_never_agree():
    """'mixed'/'range' unanimity is not a tradeable direction."""
    for label in ("mixed", "range", "na"):
        assert config.trend_agreement_from(
            {"trend_1h": label, "trend_4h": label, "trend_1d": label}) is None


# ── per-asset mode resolution ───────────────────────────────────────────

def test_mode_scope_matches_the_shipped_evidence():
    """XAUUSD is the one asset whose forfeited sample was negative (2/8 TP1),
    so it observes rather than fills; GBPUSD had no aligned samples at all."""
    assert config.aligned_touch_fill_mode_for("USTEC") == "fill"
    assert config.aligned_touch_fill_mode_for("XAGUSD") == "fill"
    assert config.aligned_touch_fill_mode_for("XAUUSD") == "log"
    assert config.aligned_touch_fill_mode_for("GBPUSD") == "log"


def test_unlisted_asset_falls_back_to_the_global_default():
    assert config.aligned_touch_fill_mode_for("USOIL") == config.ALIGNED_TOUCH_FILL_MODE


def test_broker_suffix_normalises_like_every_other_per_asset_gate():
    assert config.aligned_touch_fill_mode_for("USTECm") == "fill"
    assert config.aligned_touch_fill_mode_for("  xagusd  ") == "fill"


# ── the promotion itself ────────────────────────────────────────────────

SELL = dict(entry=58.20, stop_loss=58.44, take_profit_1=57.90)   # sl above entry
BUY = dict(entry=58.20, stop_loss=57.96, take_profit_1=58.60)    # sl below entry
DOWN = {"trend_agreement": "down"}
UP = {"trend_agreement": "up"}
TOUCHING = {"high": 58.25, "low": 58.10}      # range contains 58.20
MISSED = {"high": 58.15, "low": 58.02}        # never reached 58.20


def _fill(monkeypatch, asset="XAGUSD", levels=SELL, context=DOWN, candle=TOUCHING,
          mode=None):
    if mode is not None:
        monkeypatch.setitem(config.ALIGNED_TOUCH_FILL_MODE_BY_ASSET, asset, mode)
    return CodeSniperExecutor()._aligned_touch_fill(
        entry=levels["entry"], stop_loss=levels["stop_loss"],
        take_profit_1=levels["take_profit_1"], context=context,
        current_candle=candle, asset=asset, symbol=asset,
    )


def test_aligned_with_trend_and_entry_touched_promotes(monkeypatch):
    assert _fill(monkeypatch) == "sell"


def test_aligned_buy_promotes_in_an_uptrend(monkeypatch):
    assert _fill(monkeypatch, levels=BUY, context=UP) == "buy"


def test_entry_never_touched_does_not_promote(monkeypatch):
    """The whole mechanism is 'the level was reached' — no touch, no fill."""
    assert _fill(monkeypatch, candle=MISSED) is None


def test_setup_against_its_own_agreement_does_not_promote(monkeypatch):
    """A buy under a unanimous DOWN trend is outside the measured class."""
    assert _fill(monkeypatch, levels=BUY, context=DOWN) is None


def test_no_agreement_leaves_the_trigger_alone(monkeypatch):
    """This is the -8.2R class the trigger correctly dodges — never promote."""
    assert _fill(monkeypatch, context={"trend_agreement": None}) is None
    assert _fill(monkeypatch, context={}) is None


def test_log_mode_observes_but_never_trades(monkeypatch):
    assert _fill(monkeypatch, asset="XAUUSD", mode="log") is None


def test_off_mode_is_inert(monkeypatch):
    assert _fill(monkeypatch, mode="off") is None


def test_missing_or_degenerate_levels_never_promote(monkeypatch):
    for bad in (dict(entry=0, stop_loss=58.44, take_profit_1=57.90),
                dict(entry=58.20, stop_loss=None, take_profit_1=57.90),
                dict(entry=58.20, stop_loss=58.44, take_profit_1=None),
                dict(entry=58.20, stop_loss=58.20, take_profit_1=57.90)):
        assert _fill(monkeypatch, levels=bad) is None


def test_unusable_candle_never_promotes(monkeypatch):
    for bad in (None, {}, {"high": "x", "low": 58.1}):
        assert _fill(monkeypatch, candle=bad) is None


# ── the promotion is only a trigger substitute ──────────────────────────

def test_promoted_trade_still_faces_the_rr_gate(monkeypatch):
    """A promoted setup whose TP1 cannot pay CS_MIN_RR_RATIO_TP1 must still be
    rejected — touch-fill replaces the trigger, not the risk gates."""
    ex = CodeSniperExecutor()
    monkeypatch.setitem(config.ALIGNED_TOUCH_FILL_MODE_BY_ASSET, "XAGUSD", "fill")
    result = {
        "direction": "hold",
        "entry": 58.20, "stop_loss": 58.44,
        "take_profit_1": 58.10,      # 0.10 reward on 0.24 risk => RR 0.42
        "take_profit_2": 58.00,
        "rationale": "monitoring",
    }
    decision = ex._convert_to_decision(
        result=result, asset="XAGUSD", symbol="XAGUSD", code_signal=None,
        context=dict(DOWN), risk_service=None, trigger_price=58.20,
        current_candle=TOUCHING,
    )
    assert decision.quantity == 0.0


def test_hold_without_promotion_is_byte_identical_to_before(monkeypatch):
    """Assets not enabled for fill keep the exact previous hold behaviour."""
    ex = CodeSniperExecutor()
    result = {
        "direction": "hold", "entry": 4100.0, "stop_loss": 4110.0,
        "take_profit_1": 4080.0, "take_profit_2": 4070.0, "rationale": "no setup",
    }
    decision = ex._convert_to_decision(
        result=result, asset="XAUUSD", symbol="XAUUSD", code_signal=None,
        context=dict(DOWN), risk_service=None, trigger_price=4100.0,
        current_candle={"high": 4105.0, "low": 4095.0},
    )
    assert decision.quantity == 0.0
    assert decision.notes.startswith("Hold signal from code sniper")
    assert "ALIGNED-TOUCH-FILL" not in decision.notes
    assert result["rationale"] == "no setup"   # result left untouched in log mode


class _DummyRisk:
    """Mirrors tests/test_code_sniper_executor.py's stub."""

    def available_funds(self) -> float:
        return 100_000.0


def test_promotion_produces_a_real_sized_trade(monkeypatch):
    """End to end: hold -> promoted -> through every gate -> non-zero quantity,
    tagged so the next audit can separate promoted fills from real triggers."""
    monkeypatch.setitem(config.ALIGNED_TOUCH_FILL_MODE_BY_ASSET, "XAGUSD", "fill")
    result = {
        "direction": "hold", "entry": 58.20, "stop_loss": 58.44,
        "take_profit_1": 57.90, "take_profit_2": 57.70, "rationale": "monitoring",
    }
    decision = CodeSniperExecutor()._convert_to_decision(
        result=result, asset="XAGUSD", symbol="XAGUSD", code_signal=None,
        context=dict(DOWN), risk_service=_DummyRisk(), trigger_price=58.20,
        current_candle=TOUCHING,
    )
    assert decision.side == Side.SELL
    assert decision.quantity > 0
    assert result["rationale"].startswith("[ALIGNED-TOUCH-FILL]")
    assert "ALIGNED-TOUCH-FILL" in decision.notes


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
