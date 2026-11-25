"""Tests for the 2026-07-18 EUR/GBP stop redesign (user-approved freeze exception).

Covers: session bucketing, the session-noise SL floor term (4-year order
statistic that does not collapse in ATR lulls), its wiring into
recommended_stop_distance, the metadata.sl_anchor executor contract
(widen/log/off), session-ER percentiles, and the prompt additions
(sweep-re-entry rule, sl_anchor instruction, er_1h_session_pctile explainer).

Design doc: docs/trading_insights/design/REDESIGN_INPUT_2026-07-18_news-stops-sizing.md §2
Evidence:   docs/trading_insights/weekly/WEEKLY_REVIEW_2026-07-18.md (LP-03 × LP-08)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

import packages.core.config as config
import packages.core.session_stats as session_stats
from packages.core.session_stats import (
    session_of,
    session_noise_floor_pct,
    session_er_percentile,
)
from packages.core.market_structure import recommended_stop_distance, compute_htf_context
from packages.core.code_sniper_executor import CodeSniperExecutor
from packages.core.models import Side
from packages.core.prompt_builders import (
    unified_expert_system_prompt,
    judge_system_prompt,
    code_sniper_judge_system_prompt,
)


# ── fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture()
def stats_table(tmp_path, monkeypatch):
    """Point SESSION_STATS_PATH at a controlled table and reset the cache."""
    table = {
        "assets": {
            "EURUSD": {
                "asia": {"tr_pct": {"p50": 0.085, "p60": 0.096, "p75": 0.126},
                         "er_deciles": [0.04, 0.07, 0.10, 0.14, 0.20, 0.26, 0.32, 0.38, 0.45]},
                "eu":   {"tr_pct": {"p50": 0.126, "p60": 0.139, "p75": 0.172},
                         "er_deciles": [0.03, 0.06, 0.09, 0.13, 0.19, 0.25, 0.31, 0.38, 0.46]},
                "ny":   {"tr_pct": {"p50": 0.148, "p60": 0.170, "p75": 0.216},
                         "er_deciles": [0.04, 0.07, 0.11, 0.15, 0.20, 0.26, 0.32, 0.38, 0.45]},
                "evening": {"tr_pct": {"p50": 0.064, "p60": 0.075, "p75": 0.097},
                            "er_deciles": [0.04, 0.07, 0.10, 0.14, 0.20, 0.26, 0.33, 0.39, 0.46]},
            },
        }
    }
    path = tmp_path / "session_stats.json"
    path.write_text(json.dumps(table))
    monkeypatch.setattr("packages.core.config.SESSION_STATS_PATH", str(path))
    monkeypatch.setattr(session_stats, "_cache", {"path": None, "mtime": None, "data": None})
    return path


def _utc(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, 17, hour, minute, tzinfo=timezone.utc)


# ── session bucketing ─────────────────────────────────────────────────────

def test_session_of_buckets():
    assert session_of(_utc(0)) == "asia"
    assert session_of(_utc(7, 59)) == "asia"
    assert session_of(_utc(8)) == "eu"
    assert session_of(_utc(12, 59)) == "eu"
    assert session_of(_utc(13)) == "ny"
    assert session_of(_utc(17, 59)) == "ny"
    assert session_of(_utc(18)) == "evening"
    assert session_of(_utc(23, 59)) == "evening"


# ── session noise floor ───────────────────────────────────────────────────

def test_floor_uses_session_quantile(stats_table):
    # 03:00 asia, +2h still asia → asia p60 = 0.096
    assert session_noise_floor_pct("EURUSD", _utc(3)) == pytest.approx(0.096)


def test_floor_lookahead_takes_noisier_next_session(stats_table):
    # 07:00 asia, +2h = 09:00 eu → max(asia 0.096, eu 0.139) = 0.139
    # (the "born in Asia, dies at the EU open" hole)
    assert session_noise_floor_pct("EURUSD", _utc(7)) == pytest.approx(0.139)


def test_floor_disabled_asset_is_none(stats_table):
    assert session_noise_floor_pct("XAUUSD", _utc(3)) is None


def test_floor_missing_table_is_none(monkeypatch, tmp_path):
    monkeypatch.setattr("packages.core.config.SESSION_STATS_PATH",
                        str(tmp_path / "absent.json"))
    monkeypatch.setattr(session_stats, "_cache", {"path": None, "mtime": None, "data": None})
    assert session_noise_floor_pct("EURUSD", _utc(3)) is None


def test_floor_broker_suffix_resolves(stats_table):
    assert session_noise_floor_pct("EURUSDm", _utc(3)) == pytest.approx(0.096)


def test_recommended_stop_distance_raised_by_session_floor(stats_table, monkeypatch):
    """The freeze-week defect: live ATR collapsed to ~9 pips → 0.6× = 5.4 pip
    floor, inside session noise. The session term must lift it."""
    monkeypatch.setattr(
        "packages.core.session_stats.session_noise_floor_pct",
        lambda asset, when=None: 0.096 if asset == "EURUSD" else None,
    )
    d = recommended_stop_distance(0.0009, 1.144, "EURUSD")
    assert d == pytest.approx(0.096 / 100 * 1.144)  # ≈ 11.0 pips, not 5.4


def test_recommended_stop_distance_untouched_for_disabled_asset(monkeypatch):
    monkeypatch.setattr(
        "packages.core.session_stats.session_noise_floor_pct",
        lambda asset, when=None: None,
    )
    # BTC: 0.8 × ATR dominates as before
    d = recommended_stop_distance(300.0, 64000.0, "BTCUSD")
    assert d == pytest.approx(0.8 * 300.0)


# ── session ER percentile ─────────────────────────────────────────────────

def test_er_percentile_dead_and_directional(stats_table):
    when = _utc(3)  # asia
    assert session_er_percentile("EURUSD", 0.003, when) == 10   # below p10: dead
    assert session_er_percentile("EURUSD", 0.20, when) == 60    # at p50 decile → 60
    assert session_er_percentile("EURUSD", 0.99, when) == 90    # above p90
    assert session_er_percentile("EURUSD", None, when) is None
    assert session_er_percentile("USTEC", 0.2, when) is None    # not in table


# ── sl_anchor executor contract ───────────────────────────────────────────

def _result(direction="buy", entry=1.14431, sl=1.14377, tp1=1.14528, tp2=1.14600):
    return {
        "direction": direction, "entry": entry, "stop_loss": sl,
        "take_profit_1": tp1, "take_profit_2": tp2,
        "confidence": 0.7, "rationale": "test",
    }


def _signal(metadata=None):
    sig = MagicMock()
    sig.ttl_minutes = 180
    sig.confidence = 0.7
    sig.metadata = metadata if metadata is not None else MagicMock()  # non-dict default
    return sig


def _executor(monkeypatch):
    ex = CodeSniperExecutor()
    monkeypatch.setattr(CodeSniperExecutor, "_calculate_position_size",
                        lambda self, *a, **k: 0.12)
    return ex


def test_anchor_widens_buy_stop(monkeypatch):
    ex = _executor(monkeypatch)
    # judge stop 5.4 pips, but declared sweep_low at 1.14360 → stop must sit
    # beyond it minus pad (0.1 × atr_1h 0.0009 = 0.00009)
    meta = {"sl_anchor": {"type": "sweep_low", "price": 1.14360}}
    ctx = {"risk_pct": 0.01, "atr_1h": 0.0009}
    decision = ex._convert_to_decision(
        _result(), "EURUSD", "EURUSD", _signal(meta), ctx, risk_service=MagicMock())
    assert decision.sl == pytest.approx(1.14360 - 0.00009)


def test_anchor_widens_sell_stop(monkeypatch):
    ex = _executor(monkeypatch)
    meta = {"sl_anchor": {"type": "swing_high", "price": 1.34500}}
    ctx = {"risk_pct": 0.01, "atr_1h": 0.0010}
    decision = ex._convert_to_decision(
        _result(direction="sell", entry=1.34403, sl=1.34465, tp1=1.34263, tp2=1.34150),
        "GBPUSD", "GBPUSD", _signal(meta), ctx, risk_service=MagicMock())
    assert decision.sl == pytest.approx(1.34500 + 0.00010)


def test_anchor_beyond_stop_untouched(monkeypatch):
    ex = _executor(monkeypatch)
    # stop already beyond anchor+pad → unchanged
    meta = {"sl_anchor": {"type": "swing_low", "price": 1.14400}}
    ctx = {"risk_pct": 0.01, "atr_1h": 0.0009}
    decision = ex._convert_to_decision(
        _result(sl=1.14350, tp1=1.14600), "EURUSD", "EURUSD", _signal(meta), ctx,
        risk_service=MagicMock())
    assert decision.sl == pytest.approx(1.14350)


def test_anchor_wrong_side_ignored(monkeypatch):
    ex = _executor(monkeypatch)
    # buy anchor ABOVE entry is malformed → ignored, stop unchanged
    meta = {"sl_anchor": {"type": "swing_low", "price": 1.14500}}
    ctx = {"risk_pct": 0.01, "atr_1h": 0.0009}
    decision = ex._convert_to_decision(
        _result(), "EURUSD", "EURUSD", _signal(meta), ctx, risk_service=MagicMock())
    assert decision.sl == pytest.approx(1.14377)


def test_anchor_log_mode_never_moves_stop(monkeypatch):
    # per-asset override wins over the module default, so patch the map itself
    monkeypatch.setitem(config.SL_ANCHOR_MODE_BY_ASSET, "EURUSD", "log")
    ex = _executor(monkeypatch)
    meta = {"sl_anchor": {"type": "sweep_low", "price": 1.14360}}
    ctx = {"risk_pct": 0.01, "atr_1h": 0.0009}
    decision = ex._convert_to_decision(
        _result(), "EURUSD", "EURUSD", _signal(meta), ctx, risk_service=MagicMock())
    assert decision.sl == pytest.approx(1.14377)


# ── sl_anchor PER-ASSET SCOPE (2026-07-23 correction) ─────────────────────
# As shipped 07-18 the contract had one global mode and silently widened stops
# on every asset (19 events on 07-22 across XAU/XAG/BTC/USTEC). These lock the
# scope to the assets the redesign was actually approved for.

def test_anchor_mode_resolver_defaults_and_overrides():
    # 2026-08-06: the LP-03 control group is closed and the enabled set grew
    # from EUR/GBP to the whole traded roster. USOIL stays out — it has been
    # benched since 07-14 and is not part of the change set.
    for asset in ("EURUSD", "GBPUSD", "XAUUSD", "XAGUSD", "BTCUSD", "USTEC"):
        assert config.sl_anchor_mode_for(asset) == "widen", asset
    assert config.sl_anchor_mode_for("USOIL") == "log"
    # broker 'm' suffix resolves like the base symbol
    assert config.sl_anchor_mode_for("EURUSDm") == "widen"
    # explicit default wins for un-listed assets, never for listed ones
    assert config.sl_anchor_mode_for("USOIL", "off") == "off"
    assert config.sl_anchor_mode_for("EURUSD", "off") == "widen"
    assert config.sl_anchor_mode_for(None, "log") == "log"


@pytest.mark.parametrize("asset", ["XAUUSD", "XAGUSD", "BTCUSD", "USTEC"])
def test_anchor_widens_the_enabled_roster(monkeypatch, asset):
    """Inverted 2026-08-06 (was ``test_anchor_does_not_widen_non_fx_stops``).

    The 07-22 XAU event this used to guard against — judge stop 4108.5 with a
    declared swing_low at 4109.186, widened to 4107.841 — was reclassified from
    leak to fix. A stop placed INSIDE the level the judge itself named as its
    invalidation is the LP-03 mechanism in one line, and the 11-day census
    priced that mechanism at roughly one swept multi-R thesis per trading day.
    The stop is now expected to move.
    """
    ex = _executor(monkeypatch)
    meta = {"sl_anchor": {"type": "swing_low", "price": 4109.186}}
    ctx = {"risk_pct": 0.01, "atr_1h": 13.45}
    decision = ex._convert_to_decision(
        _result(entry=4117.261, sl=4108.5, tp1=4130.0, tp2=4140.0),
        asset, asset, _signal(meta), ctx, risk_service=MagicMock())
    # Pad is SL_ANCHOR_PAD_ATR_FRAC × ATR(1h) beyond the declared anchor.
    assert decision.sl == pytest.approx(4109.186 - 0.1 * 13.45)
    assert decision.sl < 4108.5, f"{asset} stop should have widened beyond the anchor"


def test_benched_asset_stop_is_still_untouched(monkeypatch):
    """USOIL is not on the enabled roster, so its geometry must not move.

    Keeps the negative case alive: the resolver must still be capable of
    leaving an asset alone, or the 07-18→07-23 divergence could recur unseen.
    """
    ex = _executor(monkeypatch)
    meta = {"sl_anchor": {"type": "swing_low", "price": 4109.186}}
    ctx = {"risk_pct": 0.01, "atr_1h": 13.45}
    decision = ex._convert_to_decision(
        _result(entry=4117.261, sl=4108.5, tp1=4130.0, tp2=4140.0),
        "USOIL", "USOIL", _signal(meta), ctx, risk_service=MagicMock())
    assert decision.sl == pytest.approx(4108.5)


def test_anchor_still_widens_the_approved_fx_assets(monkeypatch):
    """The scope fix must not disarm the redesign where it was approved."""
    ex = _executor(monkeypatch)
    meta = {"sl_anchor": {"type": "sweep_low", "price": 1.14360}}
    ctx = {"risk_pct": 0.01, "atr_1h": 0.0009}
    decision = ex._convert_to_decision(
        _result(), "EURUSD", "EURUSD", _signal(meta), ctx, risk_service=MagicMock())
    assert decision.sl == pytest.approx(1.14360 - 0.00009)


def test_log_mode_still_observes_without_moving_the_stop(monkeypatch, caplog):
    """Log mode must keep accruing evidence — the would-have-widened event is
    still recorded, the stop just doesn't move. Exercised on USOIL now that the
    traded roster is all in ``widen``."""
    ex = _executor(monkeypatch)
    meta = {"sl_anchor": {"type": "swing_low", "price": 4109.186}}
    ctx = {"risk_pct": 0.01, "atr_1h": 13.45}
    with caplog.at_level("WARNING"):
        ex._convert_to_decision(
            _result(entry=4117.261, sl=4108.5, tp1=4130.0, tp2=4140.0),
            "USOIL", "USOIL", _signal(meta), ctx, risk_service=MagicMock())
    assert any("SL inside declared anchor" in r.message and "log-only" in r.message
               for r in caplog.records)


def test_anchor_missing_metadata_no_crash(monkeypatch):
    ex = _executor(monkeypatch)
    decision = ex._convert_to_decision(
        _result(), "EURUSD", "EURUSD", _signal(), {"risk_pct": 0.01},
        risk_service=MagicMock())
    assert decision.sl == pytest.approx(1.14377)


def test_anchor_composes_with_floor_widening(monkeypatch):
    """Floor widens first, anchor widens further — final stop is the furthest."""
    ex = _executor(monkeypatch)
    meta = {"sl_anchor": {"type": "sweep_low", "price": 1.14300}}
    ctx = {"risk_pct": 0.01, "atr_1h": 0.0009, "sl_floor": 0.0011}
    decision = ex._convert_to_decision(
        _result(tp1=1.14700, tp2=1.14800), "EURUSD", "EURUSD", _signal(meta), ctx,
        risk_service=MagicMock())
    # floor puts stop at 1.14431-0.0011=1.14321; anchor requires 1.14300-0.00009
    assert decision.sl == pytest.approx(1.14300 - 0.00009)


# ── prompt additions ──────────────────────────────────────────────────────

def test_public_prompts_keep_generic_decision_contract():
    for p in (unified_expert_system_prompt("EURUSD"),
              judge_system_prompt("GBPUSD"),
              code_sniper_judge_system_prompt("EURUSD")):
        assert "JSON" in p
        assert "RR" in p
        assert "SWEEP RE-ENTRY" not in p


def test_public_code_sniper_prompt_keeps_execution_schema():
    p = code_sniper_judge_system_prompt("EURUSD")
    assert "def run(candles_5, now)" in p
    assert "stop_loss" in p
    assert "take_profit_1" in p
    assert "take_profit_2" in p


def test_private_session_heuristics_are_not_embedded_in_public_prompts():
    assert "er_1h_session_pctile" not in unified_expert_system_prompt("EURUSD")
    assert "er_1h_session_pctile" not in judge_system_prompt("EURUSD")
    assert "er_1h_session_pctile" not in code_sniper_judge_system_prompt("EURUSD")


# ── session-pctile injection scoped to the FX redesign (2026-07-24) ─────────
# The shared prompt says "er_1h_session_pctile <= 20 => entries need an extreme
# edge or nothing". As shipped 07-18 the pctile was injected into HTF_CONTEXT for
# EVERY asset that had a profiler table (all of them), so that selectivity line
# silently fired on metals/BTC/USTEC too — expert-flat rose ~42->~70/day and
# trades ~14->~4/day across the boundary with the tape unchanged. This scopes the
# injection to the redesign's approved assets (EUR/GBP), mirroring the 07-23
# sl_anchor seal: non-FX HTF_CONTEXT is byte-identical to freeze week 1 again.

@pytest.fixture()
def stats_table_with_disabled_asset(tmp_path, monkeypatch):
    """Controlled table that ALSO carries a *disabled* asset. The enable-gate —
    not a missing table — must be what keeps the pctile out of an unlisted
    asset's context, so the disabled asset needs real deciles present.

    2026-08-06: the disabled exemplar moved from XAUUSD to USOIL. Gold is now on
    the enabled roster; benched oil is the remaining genuine negative case.
    """
    deciles = [0.04, 0.07, 0.10, 0.14, 0.20, 0.26, 0.32, 0.38, 0.45]
    one_asset = {
        s: {"tr_pct": {"p50": 0.10, "p60": 0.12, "p75": 0.15}, "er_deciles": deciles}
        for s in ("asia", "eu", "ny", "evening")
    }
    table = {"assets": {"EURUSD": dict(one_asset), "USOIL": dict(one_asset)}}
    path = tmp_path / "session_stats.json"
    path.write_text(json.dumps(table))
    monkeypatch.setattr("packages.core.config.SESSION_STATS_PATH", str(path))
    monkeypatch.setattr(session_stats, "_cache", {"path": None, "mtime": None, "data": None})
    return path


def _h1_trending(n: int = 30):
    """Strictly rising 1h closes → er_1h computes cleanly (regime 'trending')."""
    return {"1h": [{"open": 100.0 + i, "high": 100.5 + i,
                    "low": 99.5 + i, "close": 100.0 + i} for i in range(n)]}


def test_session_pctile_follows_its_own_enable_table(stats_table_with_disabled_asset):
    candles = _h1_trending()
    eur = compute_htf_context(candles, "EURUSD")
    oil = compute_htf_context(candles, "USOIL")

    # The gate is ONLY about the session pctile — er_1h itself is unchanged.
    assert "er_1h" in eur and "er_1h" in oil

    # Enabled asset keeps the session-scaled selectivity context.
    assert "er_1h_session_pctile" in eur
    assert "session" in eur

    # Disabled asset — present in the table with data — must NOT receive it,
    # so the shared-prompt "<=20 => extreme edge or nothing" line goes inert.
    assert "er_1h_session_pctile" not in oil
    assert "session" not in oil


def test_session_pctile_table_is_independent_of_the_stop_floor(monkeypatch):
    """The two levers must be separately controllable.

    Sharing one flag between stop geometry and judge selectivity is exactly how
    the 07-18 → 07-23 scope divergence happened. Turning the pctile off for an
    asset must not disturb its stop floor.
    """
    from packages.core import config as cfg

    monkeypatch.setitem(cfg.SESSION_PCTILE_CONTEXT_ENABLED_BY_ASSET, "XAUUSD", False)
    assert cfg.SESSION_NOISE_FLOOR_ENABLED_BY_ASSET["XAUUSD"] is True
    assert cfg.sl_anchor_mode_for("XAUUSD") == "widen"
