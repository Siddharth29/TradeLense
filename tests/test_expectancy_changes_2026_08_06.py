"""Tests for the 2026-08-06 expectancy change set.

Each test is anchored to the ledger exhibit that motivated the change, so a
future reader can tell what real money the assertion is standing in for.

Covered:
  1. Risk-budget gate (LP-01)          — packages/core/code_sniper_executor.py
  2. Trail arm-on-touch (LP-03/WP-01)  — packages/core/trade_monitor.py
  3. Green-TTL extension (SP-03)       — packages/core/trade_monitor.py
  4. Scheduled-release blackout (LP-04)— packages/core/risk/news_blackout.py
  5. Scope of the stop-geometry flips  — packages/core/config.py
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from packages.core import trade_monitor as tm
from packages.core.risk import news_blackout as nb


# ── 1. Risk-budget gate (LP-01) ──────────────────────────────────────────────
#
# Exhibit: 2026-07-23, XAGUSD stopped out twice at 3.51% of a $390 account
# against an intended 1.5% — 2.34× budget. Week 2 of the freeze was XAG
# −$64.55 while every other asset combined was +$0.04.

class _StubLogger:
    def __init__(self):
        self.warnings: list[str] = []

    def warning(self, msg, *a, **kw):
        self.warnings.append(str(msg))

    def info(self, msg, *a, **kw):
        pass

    def error(self, msg, *a, **kw):
        pass


def _size(monkeypatch, *, balance, risk_pct, entry, stop, symbol="XAGUSD", **cfg):
    """Run the executor's sizing path in isolation and return the lot size."""
    from packages.core import code_sniper_executor as cse

    for key, value in cfg.items():
        monkeypatch.setattr(cse, key, value, raising=False)

    executor = cse.CodeSniperExecutor.__new__(cse.CodeSniperExecutor)
    executor.logger = _StubLogger()

    class _Broker:
        """Minimal broker double. Leverage matters: without it the margin
        branch rejects every CFD at min_lot and the risk gate never runs."""

        def balances(self):
            return {"cash": balance, "margin_free": balance}

        def wallet_info(self):
            return {"leverage": 500}

    class _Risk:
        _broker = _Broker()

        def available_funds(self):
            return balance

        def update_balances(self, _balances):
            pass

    return (
        executor._calculate_position_size(
            entry=entry,
            stop_loss=stop,
            context={"risk_pct": risk_pct},
            risk_service=_Risk(),
            asset=symbol,
            symbol=symbol,
        ),
        executor.logger.warnings,
    )


def test_risk_budget_gate_skips_min_lot_overshoot(monkeypatch):
    """XAG at a $390 balance: min-lot risk ≈ 2.3× budget → refuse the trade.

    Reproduces the 07-23 sizing exactly: 0.01 lots × 5000 oz × $0.28 of stop
    ≈ $14 of real risk against a $5.85 budget.
    """
    lot, warnings = _size(
        monkeypatch,
        balance=390.0, risk_pct=0.015,
        entry=59.68, stop=59.96,          # 0.28 of stop distance, short
        RISK_BUDGET_SKIP_ENABLED=True,
        RISK_BUDGET_MAX_OVERSHOOT_MULT=1.25,
        RISK_BUDGET_WARN_OVERSHOOT_MULT=1.10,
    )

    assert lot == 0.0, "trade should be refused, not silently taken at 2.3× risk"
    assert any("RISK BUDGET" in w for w in warnings)
    # The refusal must say what balance WOULD clear it — that number is the
    # whole point of preferring a risk rule over an asset suspension.
    assert any("Tradeable at this stop from a balance of" in w for w in warnings)


def test_risk_budget_gate_admits_the_same_trade_on_a_bigger_account(monkeypatch):
    """Same instrument, same stop, larger balance → the trade comes back.

    This is the account-size adaptivity that makes the gate a risk rule rather
    than a blacklist: nothing about XAG changed, only what the account can
    afford. No config edit is needed for the re-admission.
    """
    lot, _ = _size(
        monkeypatch,
        balance=1500.0, risk_pct=0.015,
        entry=59.68, stop=59.96,
        RISK_BUDGET_SKIP_ENABLED=True,
        RISK_BUDGET_MAX_OVERSHOOT_MULT=1.25,
        RISK_BUDGET_WARN_OVERSHOOT_MULT=1.10,
    )
    assert lot > 0.0


def test_backtest_switch_does_not_bypass_live_risk_budget(monkeypatch):
    """The live stop-risk budget still applies when replay tolerances are disabled."""
    lot, warnings = _size(
        monkeypatch,
        balance=390.0, risk_pct=0.015,
        entry=59.68, stop=59.96,
        RISK_BUDGET_SKIP_ENABLED=False,
        RISK_BUDGET_MAX_OVERSHOOT_MULT=1.25,
        RISK_BUDGET_WARN_OVERSHOOT_MULT=1.10,
    )
    assert lot == 0.0
    assert any("RISK BUDGET" in w for w in warnings)


def test_risk_budget_gate_ignores_trades_inside_budget(monkeypatch):
    """A position the account can actually afford is untouched and unwarned."""
    lot, warnings = _size(
        monkeypatch,
        balance=10_000.0, risk_pct=0.015,
        entry=59.68, stop=59.96,
        RISK_BUDGET_SKIP_ENABLED=True,
        RISK_BUDGET_MAX_OVERSHOOT_MULT=1.25,
        RISK_BUDGET_WARN_OVERSHOOT_MULT=1.10,
    )
    assert lot > 0.0
    assert warnings == []


# ── 2. Trail arm-on-touch (LP-03 / WP-01) ────────────────────────────────────
#
# Exhibit: 2026-07-16 EURUSD traded to +1.00R intra-bar exactly, closed back
# under it, the 1.0R→0.5R step never armed on close basis, the trade took a
# full −1R, and the thesis then ran +1.20R.

def _long_state(entry=100.0, stop=99.0):
    return {
        "direction": "long",
        "entry": entry,
        "sl": stop,
        "initial_stop": stop,
        "tp1_hit": True,   # past the BE stage so trailing is what's under test
    }


TOUCH_RULES = {
    # (trigger_r, candle_confirm, trail_to_r) — step 0 is the touch-UNSAFE
    # 0.5→0.5 shape, step 1 is the 07-16 case.
    "trail_config": [(0.5, 1, 0.5), (1.0, 1, 0.5)],
}


def test_trail_arms_on_intrabar_touch():
    """+1.00R touched intra-bar, closed below → the step arms and locks +0.5R."""
    state = _long_state()
    state["trail_step"] = 1  # step 0 already consumed
    state["trail_consec"] = 0
    rules = dict(TOUCH_RULES, trail_arm_on_touch=True)

    action = tm.evaluate_trade(
        state, candle_close=100.4, candle_high=101.0, candle_low=100.2, rules=rules
    )

    assert action == tm.TRAIL_SL
    # Stop lands at +0.5R — strictly inside the +1.0R that armed it.
    assert state["_pending_trail_sl"] == pytest.approx(100.5)


def test_close_basis_misses_the_same_bar():
    """The pre-change behaviour, kept as the control for the test above."""
    state = _long_state()
    state["trail_step"] = 1
    state["trail_consec"] = 0
    rules = dict(TOUCH_RULES, trail_arm_on_touch=False)

    action = tm.evaluate_trade(
        state, candle_close=100.4, candle_high=101.0, candle_low=100.2, rules=rules
    )
    assert action is None


def test_touch_arming_never_applies_to_a_step_that_stops_at_its_trigger():
    """Step 0 is 0.5R→0.5R: arming on touch would park the stop at the touch.

    The bar below touches +0.5R and closes back at +0.2R. Close-basis must
    still govern, so no trail fires — otherwise the stop would sit at exactly
    the level the bar has already retraced from.
    """
    state = _long_state()
    rules = dict(TOUCH_RULES, trail_arm_on_touch=True)

    action = tm.evaluate_trade(
        state, candle_close=100.2, candle_high=100.5, candle_low=100.0, rules=rules
    )
    assert action is None
    assert "_pending_trail_sl" not in state


def test_touch_arming_is_direction_correct_for_shorts():
    """Shorts arm off the bar LOW, not the high."""
    state = {
        "direction": "short", "entry": 100.0, "sl": 101.0,
        "initial_stop": 101.0, "tp1_hit": True,
        "trail_step": 1, "trail_consec": 0,
    }
    rules = dict(TOUCH_RULES, trail_arm_on_touch=True)

    action = tm.evaluate_trade(
        state, candle_close=99.6, candle_high=99.8, candle_low=99.0, rules=rules
    )
    assert action == tm.TRAIL_SL
    assert state["_pending_trail_sl"] == pytest.approx(99.5)


def test_malformed_bar_cannot_arm_earlier_than_close_basis():
    """A bar whose 'high' is below its close must not arm a long's step."""
    state = _long_state()
    state["trail_step"] = 1
    state["trail_consec"] = 0
    rules = dict(TOUCH_RULES, trail_arm_on_touch=True)

    # close is at +1.0R but the (bogus) high is below it — the max() guard
    # means we fall back to the close, which arms. The inverse case, a high
    # LOWER than a sub-trigger close, must still not arm:
    action = tm.evaluate_trade(
        state, candle_close=100.4, candle_high=100.3, candle_low=100.1, rules=rules
    )
    assert action is None


# ── 3. Green-TTL extension (SP-03) ───────────────────────────────────────────
#
# Exhibit: 2026-07-17 XAUUSD timed out at −0.17R; the thesis then ran +3.04R.
# Logged in the SP-03 killer standings as "TTL expiry 1".

def _ttl_args(minutes_to_expiry=1, hours_since_entry=4):
    now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
    return {
        "expiry": now + timedelta(minutes=minutes_to_expiry),
        "start_time": now - timedelta(hours=hours_since_entry),
        "now": now,
    }


def test_ttl_extends_for_a_green_risk_free_position():
    state = {"direction": "long", "entry": 100.0, "initial_stop": 99.0, "sl": 100.2}
    args = _ttl_args()
    new_expiry = tm.maybe_extend_expiry(state, price=101.5, **args)
    assert new_expiry > args["expiry"]
    assert new_expiry == args["expiry"] + timedelta(hours=tm.TTL_EXTEND_HOURS)


def test_ttl_does_not_extend_a_losing_position():
    """The 07-17 exhibit only justifies extending WINNERS. A red trade at the
    4h mark is closed exactly as before."""
    state = {"direction": "long", "entry": 100.0, "initial_stop": 99.0, "sl": 100.2}
    args = _ttl_args()
    assert tm.maybe_extend_expiry(state, price=99.5, **args) == args["expiry"]


def test_ttl_does_not_extend_while_the_stop_can_still_lose_money():
    """Green but the stop has not ratcheted to entry → extension would be
    extending exposure, not extending a thesis."""
    state = {"direction": "long", "entry": 100.0, "initial_stop": 99.0, "sl": 99.0}
    args = _ttl_args()
    assert tm.maybe_extend_expiry(state, price=101.5, **args) == args["expiry"]


def test_ttl_does_not_extend_before_the_grace_window():
    """Nothing is silently lengthened mid-life: only a position genuinely about
    to time out is considered."""
    state = {"direction": "long", "entry": 100.0, "initial_stop": 99.0, "sl": 100.2}
    args = _ttl_args(minutes_to_expiry=90)
    assert tm.maybe_extend_expiry(state, price=101.5, **args) == args["expiry"]


def test_ttl_extension_respects_the_hard_cap():
    state = {"direction": "long", "entry": 100.0, "initial_stop": 99.0, "sl": 100.2}
    args = _ttl_args(hours_since_entry=tm.TTL_EXTEND_MAX_HOURS)
    assert tm.maybe_extend_expiry(state, price=101.5, **args) == args["expiry"]


def test_ttl_extension_is_flag_gated(monkeypatch):
    monkeypatch.setattr(tm, "TTL_EXTEND_WHEN_GREEN", False)
    state = {"direction": "long", "entry": 100.0, "initial_stop": 99.0, "sl": 100.2}
    args = _ttl_args()
    assert tm.maybe_extend_expiry(state, price=101.5, **args) == args["expiry"]


def test_unrealised_r_measures_against_the_initial_stop():
    """R must stay comparable for the life of the trade even after trailing."""
    state = {"direction": "long", "entry": 100.0, "initial_stop": 99.0, "sl": 100.5}
    assert tm.unrealised_r(state, 102.0) == pytest.approx(2.0)

    short = {"direction": "short", "entry": 100.0, "initial_stop": 101.0, "sl": 99.5}
    assert tm.unrealised_r(short, 98.0) == pytest.approx(2.0)


# ── 4. Scheduled-release blackout (LP-04) ────────────────────────────────────
#
# Exhibits: 07-07 −$41.17, 07-10 −$31.98, 07-14 −$38.20 (incl. $22.66 of
# slippage straight through an XAUUSD stop on the 12:30 CPI bar).

@pytest.mark.parametrize("hh,mm,blocked", [
    (12, 14, False),   # just before the window
    (12, 15, True),    # inclusive start
    (12, 30, True),    # the CPI print itself (07-14)
    (12, 39, True),
    (12, 40, False),   # exclusive end
    (14, 30, True),    # the 07-10 −$31.98 incident
    (9, 0, False),     # ordinary morning bar
    (20, 45, False),   # maintenance window is LP-10's problem, not this gate's
])
def test_blackout_windows(hh, mm, blocked):
    now = datetime(2026, 8, 6, hh, mm, tzinfo=timezone.utc)
    active, reason = nb.news_blackout_active("XAUUSD", now=now)
    assert active is blocked
    assert bool(reason) is blocked


def test_blackout_exempts_crypto():
    """All three measured exhibits were metals/indices/FX; BTC trades 24/7 and
    has no scheduled-release sensitivity in the record."""
    now = datetime(2026, 8, 6, 12, 30, tzinfo=timezone.utc)
    assert nb.news_blackout_active("BTCUSD", now=now)[0] is False
    assert nb.news_blackout_active("XAUUSD", now=now)[0] is True


def test_blackout_tolerates_the_broker_demo_suffix():
    now = datetime(2026, 8, 6, 12, 30, tzinfo=timezone.utc)
    assert nb.news_blackout_active("BTCUSDm", now=now)[0] is False
    assert nb.news_blackout_active("XAUUSDm", now=now)[0] is True


def test_blackout_reads_naive_datetimes_as_utc():
    naive = datetime(2026, 8, 6, 12, 30)
    assert nb.news_blackout_active("XAUUSD", now=naive)[0] is True


def test_blackout_is_flag_gated(monkeypatch):
    monkeypatch.setattr(nb, "NEWS_BLACKOUT_ENABLED", False)
    now = datetime(2026, 8, 6, 12, 30, tzinfo=timezone.utc)
    assert nb.news_blackout_active("XAUUSD", now=now)[0] is False


def test_blackout_fails_open_on_a_malformed_window(monkeypatch):
    """A bad config entry must never halt trading — every other guard on this
    path fails open and this one matches."""
    monkeypatch.setattr(nb, "NEWS_BLACKOUT_WINDOWS_UTC", ((None, "x", 1, 2),))
    now = datetime(2026, 8, 6, 12, 30, tzinfo=timezone.utc)
    assert nb.news_blackout_active("XAUUSD", now=now)[0] is False


def test_minutes_until_blackout():
    now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
    assert nb.minutes_until_blackout("XAUUSD", now=now) == 15
    # Past every window for the day.
    late = datetime(2026, 8, 6, 23, 0, tzinfo=timezone.utc)
    assert nb.minutes_until_blackout("XAUUSD", now=late) is None
    assert nb.minutes_until_blackout("BTCUSD", now=now) is None


# ── 5. Scope of the stop-geometry flips ──────────────────────────────────────

def test_noise_floor_and_anchor_scopes_match():
    """The 07-18/07-23 divergence — noise floor gated to FX while the anchor
    contract silently ran everywhere — cost the metals control group its
    validity. The two scopes are asserted equal so it cannot recur silently.
    """
    from packages.core import config

    floor_assets = {a for a, on in config.SESSION_NOISE_FLOOR_ENABLED_BY_ASSET.items() if on}
    widen_assets = {a for a, m in config.SL_ANCHOR_MODE_BY_ASSET.items() if m == "widen"}
    assert floor_assets == widen_assets


def test_every_enabled_asset_has_profiler_data():
    """A noise floor with no session statistics behind it silently degrades to
    the old ATR floor, which would make the change a no-op nobody notices."""
    import json
    from packages.core import config

    with open(config.SESSION_STATS_PATH) as fh:
        stats = json.load(fh)

    for asset, enabled in config.SESSION_NOISE_FLOOR_ENABLED_BY_ASSET.items():
        if not enabled:
            continue
        assert asset in stats["assets"], f"{asset} enabled but absent from session_stats.json"
        for session in stats["sessions"]:
            row = stats["assets"][asset].get(session)
            assert row and row.get("tr_pct", {}).get(config.SESSION_NOISE_FLOOR_QUANTILE), (
                f"{asset}/{session} missing {config.SESSION_NOISE_FLOOR_QUANTILE}"
            )
