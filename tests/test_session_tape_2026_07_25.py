"""Tests for the 2026-07-25 session-tape observation (LP-08 fix plan, Phase 0).

Covers: the compute_session_tape signal math (direction / elapsed-session ER /
LP-14 2h persistence), the conflict definition, "when present" semantics
(absent below SESSION_TAPE_MIN_BARS), time-format tolerance, the per-asset
mode resolver, and the dormant Phase-2a context merge (byte-identical prompt
context while every asset is in "log" mode).

Plan: chat-approved LP-08 phase plan, 2026-07-25 (Phase 0 = measurement only).
Evidence: PATTERN_LIBRARY.md LP-08 (4 days), LP-14 (2h-ratio persistence).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import packages.core.config as config
from packages.core.config import session_tape_mode_for
from packages.core.market_structure import compute_session_tape
from packages.core.session_stats import session_bounds


# 06:00 UTC start → pre-session (Asia tail) bars first, EU session opens 08:00.
BASE = datetime(2026, 7, 21, 6, 0, tzinfo=timezone.utc)


def _series(closes, start=BASE, iso=True):
    """5m candle dicts from a close series (open = previous close)."""
    out = []
    for i, px in enumerate(closes):
        t = start + timedelta(minutes=5 * i)
        prev = closes[i - 1] if i else px
        out.append({
            "time": t.isoformat() if iso else t.timestamp(),
            "open": float(prev),
            "high": float(max(prev, px)) + 0.01,
            "low": float(min(prev, px)) - 0.01,
            "close": float(px),
            "volume": 100.0,
        })
    return out


def _downtrend(n_total=49, step=0.05):
    """Monotonic decline 06:00 → …; ER ≈ 1.0 at every window."""
    return [100.0 - step * i for i in range(n_total)]


# ── signal math + conflict definition ─────────────────────────────────────

def test_conflict_on_trending_down_session_vs_up_label():
    candles = _series(_downtrend())          # last bar 10:00 → EU session
    tape = compute_session_tape(candles, "up", "XAUUSD")
    assert tape is not None
    assert tape["session"] == "eu"
    assert tape["direction"] == "down"
    assert tape["er_session"] is not None and tape["er_session"] >= 0.99
    assert tape["er_2h"] is not None and tape["er_2h"] >= 0.99
    assert tape["conflict"] is True


def test_aligned_session_is_not_conflict():
    candles = _series(_downtrend())
    tape = compute_session_tape(candles, "down", "XAUUSD")
    assert tape is not None
    assert tape["conflict"] is False


def test_nondirectional_label_never_conflicts():
    candles = _series(_downtrend())
    for label in ("mixed", "range", "na", None):
        tape = compute_session_tape(candles, label, "XAUUSD")
        assert tape is not None
        assert tape["conflict"] is False, f"label={label!r} must not conflict"


def test_absent_when_insufficient_session_bars():
    # Plenty of pre-session bars, but only 7 bars at/after the 08:00 EU open
    # (06:00 + 30 bars ends 08:30) — below SESSION_TAPE_MIN_BARS ⇒ no signal.
    candles = _series(_downtrend(n_total=31))
    assert compute_session_tape(candles, "up", "XAUUSD") is None
    assert compute_session_tape([], "up", "XAUUSD") is None
    assert compute_session_tape([{"open": 1.0, "close": 1.0}], "up", "XAUUSD") is None


def test_spike_without_2h_persistence_is_not_conflict():
    # 24 flat pre-session bars, a hard 12-bar session drop, then ≥2h of pure
    # oscillation: the session is net-down and directional (er_session high)
    # but the trailing-2h ER collapses — LP-14 says spike, not a move.
    closes = [100.0] * 24
    closes += [100.0 - 0.8 * i for i in range(1, 13)]          # → 90.4
    closes += [90.4 + (0.02 if i % 2 else -0.02) for i in range(25)]
    candles = _series(closes)                                   # last bar 11:00, EU
    tape = compute_session_tape(candles, "up", "XAUUSD")
    assert tape is not None
    assert tape["direction"] == "down"
    assert tape["er_session"] >= config.SESSION_TAPE_MIN_ER
    assert tape["er_2h"] is not None and tape["er_2h"] < config.SESSION_TAPE_MIN_ER_2H
    assert tape["conflict"] is False


def test_epoch_times_parse_like_iso():
    a = compute_session_tape(_series(_downtrend(), iso=True), "up", "XAUUSD")
    b = compute_session_tape(_series(_downtrend(), iso=False), "up", "XAUUSD")
    assert a is not None and b is not None
    assert a == b


def test_session_bounds_helper():
    name, start, end = session_bounds(datetime(2026, 7, 21, 10, 0, tzinfo=timezone.utc))
    assert name == "eu"
    assert start == datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 7, 21, 13, 0, tzinfo=timezone.utc)
    # evening runs to 24:00 — end must land on next midnight, not crash
    name2, start2, end2 = session_bounds(datetime(2026, 7, 21, 22, 30, tzinfo=timezone.utc))
    assert name2 == "evening"
    assert end2 == datetime(2026, 7, 22, 0, 0, tzinfo=timezone.utc)


# ── mode resolver (mirrors sl_anchor_mode_for) ────────────────────────────

def test_mode_resolver_defaults_and_overrides(monkeypatch):
    assert session_tape_mode_for("BTCUSD") == "log"          # global default
    monkeypatch.setattr(config, "SESSION_TAPE_MODE_BY_ASSET", {"EURUSD": "context"})
    assert session_tape_mode_for("EURUSD") == "context"
    assert session_tape_mode_for("EURUSDm") == "context"     # broker suffix normalised
    assert session_tape_mode_for("BTCUSD") == "log"
    monkeypatch.setattr(config, "SESSION_TAPE_MODE", "off")
    assert session_tape_mode_for("BTCUSD") == "off"
    assert session_tape_mode_for(None) == "off"


def test_phase2a_enable_set_pinned():
    """2026-07-27 user-approved enable set — a change here is a scope change
    and must be deliberate (the 07-18 lesson: leaks ship in unpinned scopes)."""
    for asset in ("XAUUSD", "XAGUSD", "USTEC"):
        assert session_tape_mode_for(asset) == "context", asset
    assert session_tape_mode_for("XAUUSDm") == "context"     # broker suffix
    for asset in ("BTCUSD", "EURUSD", "GBPUSD", "USOIL"):
        assert session_tape_mode_for(asset) == "log", asset


def test_public_prompts_use_supplied_context_without_private_session_rules():
    from packages.core.prompt_builders import (
        unified_expert_system_prompt, judge_system_prompt,
        code_sniper_judge_system_prompt,
    )
    for build in (unified_expert_system_prompt, judge_system_prompt,
                  code_sniper_judge_system_prompt):
        p = build("XAUUSD")
        assert "supplied" in p
        assert "session_tape" not in p


# ── Phase-2a surface stays dormant in "log" mode ──────────────────────────

def _h1_trending(n: int = 30):
    return {"1h": [{"open": 100.0 + i, "high": 100.5 + i,
                    "low": 99.5 + i, "close": 100.0 + i} for i in range(n)]}


def test_context_merge_dormant_without_session_tape():
    from packages.core.multi_expert import _cycle_htf_context
    gpt_cfg = {"htf_candles": _h1_trending(), "asset": "XAUUSD", "session_tape": None}
    ctx = _cycle_htf_context(gpt_cfg)
    assert ctx  # HTF context itself computed
    assert "session_tape" not in ctx


def test_context_merge_active_only_when_tape_present():
    from packages.core.multi_expert import _cycle_htf_context
    tape = {"session": "eu", "direction": "down", "move_pct": -1.2,
            "er_session": 0.41, "er_2h": 0.22, "er_ratio_2h_1h": 0.9,
            "bars": 30, "htf_trend": "up", "conflict": True}
    gpt_cfg = {"htf_candles": _h1_trending(), "asset": "XAUUSD", "session_tape": tape}
    ctx = _cycle_htf_context(gpt_cfg)
    assert ctx["session_tape"] == {"session": "eu", "direction": "down",
                                   "move_pct": -1.2, "er_session": 0.41,
                                   "er_2h": 0.22, "conflict": True}
