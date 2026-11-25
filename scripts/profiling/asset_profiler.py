"""Asset profiling engine: derive robust per-asset config values from multi-year data.

Reads ~4 years of MT5 candles (read-only, live Real8 creds — candles are
account-independent but the live terminal has the plain symbol names) and derives
per-asset configuration proposals for:

  1. HTF_REGIME_ATR_PCT_BY_ASSET      low = p25, high = p90 of realised ATR(14,1h)%
                                      (same method as the 2026-06-21 calibration,
                                      now on 4 years with stability gating)
  2. CS_MIN_DISTANCE_TO_TP_PCT_BY_ASSET   p25 of the best-direction 2h envelope move
                                      ("a TP floor must be clearable in a typical
                                      2h hold even on a quiet-quartile day")
  3. CS_MIN_DISTANCE_TO_SL_PCT_BY_ASSET   p25 of 1h true-range% (noise backstop;
                                      the 0.6-0.8×ATR(1h) floor dominates live)
  4. HTF_TREND_MIN_SEPARATION_PCT_BY_ASSET  dead-label check: share of H1 bars whose
                                      |EMA20/EMA50-1|% sits below the threshold
                                      ('range' label) should be meaningfully 10-60%
  5. Registry constraint audit        max_sl_distance vs the effective SL floor at
                                      each slice's median price (the bug class that
                                      zeroed GBPUSD 06-04 and EURUSD 07-10)

Plus informational profiles (NOT config): Kaufman-ER chop/trend base rates,
consecutive-day bounce probabilities (LP-08), UTC-hour volatility curve.

ANTI-OVERFITTING CONTRACT (user mandate 2026-07-11 — do not weaken):
  * No P&L objective anywhere. Values are order statistics of market structure,
    never "what would have made money". There is nothing to curve-fit.
  * The series is split into trailing 365-day slices Y1 (newest) .. Y4 (oldest).
    Each metric is computed per slice. STABILITY: every slice must lie within
    ±STABILITY_TOL of the all-slice median, else the proposal is REJECTED.
  * WALK-FORWARD HOLDOUT: the median of the older slices (Y2..Y4) must agree with
    the unseen newest year Y1 within ±STABILITY_TOL, else REJECTED. A value that
    only describes the past does not ship.
  * DEAD-BAND: if the derived value is within ±DEADBAND of the current live config,
    verdict is KEEP (no churn; current value is not wrong, just differently rounded).
  * Values are rounded to 2 significant figures (regularization by rounding).
  * The engine only PROPOSES. Nothing writes to config.py. A human (or reviewed
    session) applies APPLY verdicts by hand.

Usage:
    python scripts/profiling/asset_profiler.py --pull      # fetch candles to data/profiling/
    python scripts/profiling/asset_profiler.py --profile   # compute + write proposals
    python scripts/profiling/asset_profiler.py             # both
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA_DIR = os.path.join(REPO, "data", "profiling")
SYMBOLS = ["BTCUSD", "ETHUSD", "XAUUSD", "XAGUSD", "GBPUSD", "EURUSD", "USOIL", "USTEC"]
YEARS = 4
STABILITY_TOL = 0.35   # per-slice deviation from median that still counts as stable
DEADBAND = 0.25        # |derived-current|/current below this ⇒ KEEP current value
ER_CHOP, ER_TREND = 0.12, 0.30   # established Kaufman-ER bands (runbook §3)

# Live config values the proposals are diffed against — imported from the repo so
# the engine can never drift out of sync with what is actually deployed.
def load_current() -> dict:
    sys.path.insert(0, REPO)
    from packages.core.config import (
        HTF_REGIME_ATR_PCT_BY_ASSET, HTF_REGIME_LOW_VOL_ATR_PCT, HTF_REGIME_HIGH_VOL_ATR_PCT,
        CS_MIN_DISTANCE_TO_TP_PCT_BY_ASSET, CS_MIN_DISTANCE_TO_TP_PCT,
        CS_MIN_DISTANCE_TO_SL_PCT_BY_ASSET, CS_MIN_DISTANCE_TO_SL_PCT,
        HTF_TREND_MIN_SEPARATION_PCT, ATR_STOP_MULT_BY_ASSET, ATR_STOP_MULT,
    )
    from packages.core.asset_registry import ASSET_PROFILES
    return {
        "regime": {s: (b.get("low", HTF_REGIME_LOW_VOL_ATR_PCT),
                       b.get("high", HTF_REGIME_HIGH_VOL_ATR_PCT))
                   for s, b in HTF_REGIME_ATR_PCT_BY_ASSET.items()},
        "regime_default": (HTF_REGIME_LOW_VOL_ATR_PCT, HTF_REGIME_HIGH_VOL_ATR_PCT),
        "tp_floor": dict(CS_MIN_DISTANCE_TO_TP_PCT_BY_ASSET),
        "sl_floor": dict(CS_MIN_DISTANCE_TO_SL_PCT_BY_ASSET),
        "tp_floor_default": CS_MIN_DISTANCE_TO_TP_PCT,
        "sl_floor_default": CS_MIN_DISTANCE_TO_SL_PCT,
        "trend_sep_default": HTF_TREND_MIN_SEPARATION_PCT,
        "atr_stop_mult": dict(ATR_STOP_MULT_BY_ASSET),
        "atr_stop_mult_default": ATR_STOP_MULT,
        "max_sl_distance": {s: p.max_sl_distance for s, p in ASSET_PROFILES.items()},
    }


# ── data pull ─────────────────────────────────────────────────────────────

def pull() -> None:
    import MetaTrader5 as mt5  # Windows-only; import here so --profile runs anywhere

    creds_path = os.path.join(REPO, "config", "mt5_config.local.json")
    if not os.path.exists(creds_path):
        sys.exit("live creds missing; refusing demo fallback (audit-runbook rule)")
    cfg = json.load(open(creds_path))["mt5"]
    if "real" not in str(cfg.get("server", "")).lower():
        sys.exit(f"config server '{cfg.get('server')}' is not a real account — refusing (demo feeds are not the trading feed)")
    # Initialize WITH the real creds — never attach to whatever account the
    # terminal happens to be sitting on (2026-07-18: ambient terminal state
    # proved unreliable; bare initialize() attached to a non-real session).
    if not mt5.initialize(login=int(cfg["login"]), password=cfg["password"], server=cfg["server"]):
        if not mt5.initialize():
            sys.exit(f"mt5.initialize failed: {mt5.last_error()}")
        if not mt5.login(login=int(cfg["login"]), password=cfg["password"], server=cfg["server"]):
            sys.exit(f"mt5.login failed: {mt5.last_error()}")
    ai = mt5.account_info()
    # HARD verification (2026-07-18): the audit runbook rule — prove the account,
    # never assume it. A running demo terminal must never supply profiling data.
    if ai is None or ai.login != int(cfg["login"]) or "real" not in str(ai.server).lower():
        sys.exit(f"ACCOUNT VERIFICATION FAILED: connected to "
                 f"{getattr(ai, 'login', None)}@{getattr(ai, 'server', None)}, "
                 f"expected {cfg['login']}@{cfg['server']} — refusing to pull")
    print(f"ACTIVE ACCOUNT: {ai.login} {ai.server} balance={ai.balance} equity={ai.equity} {ai.currency}")
    os.makedirs(DATA_DIR, exist_ok=True)
    to = datetime.now(timezone.utc)
    frm = to - timedelta(days=365 * YEARS + 30)
    # M15 history does not survive multi-year range requests on this broker
    # (probe 2026-07-11: returns empty past ~2y). H1 reaches 2020 for all 8
    # symbols, and the 2h envelope extremes used for the TP-floor metric are
    # timeframe-invariant, so H1+D1 are sufficient.
    tfs = [("D1", mt5.TIMEFRAME_D1), ("H1", mt5.TIMEFRAME_H1)]
    for sym in SYMBOLS:
        mt5.symbol_select(sym, True)
        for name, tf in tfs:
            r = mt5.copy_rates_range(sym, tf, frm, to)
            if r is None or len(r) == 0:
                print(f"{sym} {name}: NO DATA")
                continue
            df = pd.DataFrame(r)
            df["dt"] = pd.to_datetime(df["time"], unit="s", utc=True)
            df.to_csv(os.path.join(DATA_DIR, f"{sym}_{name}.csv"), index=False)
            print(f"{sym} {name}: {len(df)} bars from {df['dt'].iloc[0].date()}")
    mt5.shutdown()


# ── metric helpers ────────────────────────────────────────────────────────

def round_sig(x: float, sig: int = 2) -> float:
    if x == 0 or not np.isfinite(x):
        return 0.0
    return round(x, -int(math.floor(math.log10(abs(x)))) + (sig - 1))


def atr_pct(h1: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = h1["close"].shift(1)
    tr = pd.concat([h1["high"] - h1["low"], (h1["high"] - prev_close).abs(),
                    (h1["low"] - prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean() / h1["close"] * 100

def true_range_pct(h1: pd.DataFrame) -> pd.Series:
    return (h1["high"] - h1["low"]) / h1["close"] * 100

def fav_move_2h_pct(h1: pd.DataFrame, bars_ahead: int = 2) -> pd.Series:
    """Best-direction envelope move over the next 2h (same definition as the
    2026-07-10 EURUSD calibration; envelope extremes are timeframe-invariant,
    so 2×H1 bars ≡ 8×M15 bars for the max-high/min-low window)."""
    c = h1["close"].to_numpy()
    hi = h1["high"].rolling(bars_ahead).max().shift(-bars_ahead).to_numpy()
    lo = h1["low"].rolling(bars_ahead).min().shift(-bars_ahead).to_numpy()
    up, dn = (hi - c) / c * 100, (c - lo) / c * 100
    return pd.Series(np.maximum(up, dn), index=h1.index)

def ema_sep_pct(h1: pd.DataFrame) -> pd.Series:
    fast = h1["close"].ewm(span=20, adjust=False).mean()
    slow = h1["close"].ewm(span=50, adjust=False).mean()
    return (fast / slow - 1).abs() * 100

def kaufman_er(h1: pd.DataFrame, window: int = 24) -> pd.Series:
    c = h1["close"]
    return (c - c.shift(window)).abs() / c.diff().abs().rolling(window).sum()


# ── per-session statistics (2026-07-18, user-approved EUR/GBP stop redesign) ──
# UTC sessions on H1 bar-start hours. Matches packages/core/session_stats.py —
# the runtime consumer of the table this emits. Keep the two in sync.
SESSIONS = {"asia": (0, 8), "eu": (8, 13), "ny": (13, 18), "evening": (18, 24)}

def session_label_for_hour(hour: int) -> str:
    for name, (lo, hi) in SESSIONS.items():
        if lo <= hour < hi:
            return name
    return "asia"

def session_stats(now: pd.Timestamp) -> dict:
    """Per-asset × per-session order statistics for the runtime table.

    For each asset and UTC session:
      * tr_pct quantiles (p50/p60/p75) of the H1 true-range% of bars in that
        session — the noise unit for the session-aware SL floor
        (min_sl = max(ATR floor, %-backstop, MULT × chosen quantile × price)).
      * er deciles (p10..p90) of kaufman_er(REGIME_ER_BARS=24) sampled at bars
        in that session — the reference grid that turns a live er_1h reading
        into "Nth percentile for this asset in this session" prompt context.

    Same anti-overfit posture as the proposal engine: pure order statistics per
    365-day slice, median across slices, per-slice worst deviation reported as
    `stability` (context stats, not gates — unstable rows are kept but flagged).
    """
    out: dict = {}
    for sym in SYMBOLS:
        path = os.path.join(DATA_DIR, f"{sym}_H1.csv")
        if not os.path.exists(path):
            continue
        h1 = pd.read_csv(path, parse_dates=["dt"])
        h1["hour"] = h1["dt"].dt.hour
        h1["session"] = h1["hour"].map(session_label_for_hour)
        h1["tr_pct"] = true_range_pct(h1)
        h1["er"] = kaufman_er(h1)
        sym_out: dict = {}
        for sess in SESSIONS:
            per_slice_tr: dict[str, dict] = {}
            per_slice_er: dict[str, list] = {}
            for label, s in slices(h1, now):
                sb = s[s["session"] == sess]
                if len(sb) < 200:
                    continue
                tr = sb["tr_pct"].dropna()
                per_slice_tr[label] = {q: float(tr.quantile(int(q[1:]) / 100))
                                       for q in ("p50", "p60", "p75")}
                er = sb["er"].dropna()
                per_slice_er[label] = [float(er.quantile(d / 10)) for d in range(1, 10)]
            if not per_slice_tr:
                continue
            tr_med = {q: round_sig(float(np.median([v[q] for v in per_slice_tr.values()])), 3)
                      for q in ("p50", "p60", "p75")}
            med60 = tr_med["p60"] or 1.0
            worst = max(abs(v["p60"] / med60 - 1) for v in per_slice_tr.values())
            er_grid = [round(float(np.median(col)), 3)
                       for col in zip(*per_slice_er.values())] if per_slice_er else None
            sym_out[sess] = {
                "tr_pct": tr_med,
                "er_deciles": er_grid,          # p10..p90 of er_1h in this session
                "stability_tr_p60": round(worst, 3),
                "slices": len(per_slice_tr),
            }
        if sym_out:
            out[sym] = sym_out
    return out

def bounce_stats(d1: pd.DataFrame) -> dict:
    """P(next day closes opposite | N consecutive same-direction closes). LP-08 context."""
    sign = np.sign(d1["close"].diff()).to_numpy()
    runs = np.zeros(len(sign), dtype=int)   # consecutive same-sign closes ending at i
    for i in range(1, len(sign)):
        if sign[i] != 0:
            runs[i] = runs[i - 1] + 1 if sign[i] == sign[i - 1] else 1
    out = {}
    for n in (2, 3, 4):
        idx = [i for i in range(1, len(sign) - 1) if runs[i] >= n and sign[i + 1] != 0]
        hits = sum(sign[i + 1] != sign[i] for i in idx)
        out[f"bounce_after_{n}"] = round(hits / len(idx), 3) if len(idx) >= 30 else None
        out[f"bounce_after_{n}_n"] = len(idx)
    return out


# ── slice machinery ───────────────────────────────────────────────────────

def slices(df: pd.DataFrame, now: pd.Timestamp) -> list[tuple[str, pd.DataFrame]]:
    out = []
    for k in range(YEARS):
        hi = now - pd.Timedelta(days=365 * k)
        lo = now - pd.Timedelta(days=365 * (k + 1))
        s = df[(df.dt > lo) & (df.dt <= hi)]
        if len(s) >= 500:  # require a substantive slice (H1: ≥~1 month of bars)
            out.append((f"Y{k+1}", s))
    return out


def judge(name: str, per_slice: dict[str, float], current: float | None):
    """Apply the anti-overfit contract to one metric. Returns (verdict, value, why)."""
    vals = {k: v for k, v in per_slice.items() if v is not None and np.isfinite(v)}
    if len(vals) < 3:
        return "REJECT", None, f"only {len(vals)} usable yearly slices"
    med = float(np.median(list(vals.values())))
    if med <= 0:
        return "REJECT", None, "degenerate median"
    worst = max(abs(v / med - 1) for v in vals.values())
    if worst > STABILITY_TOL:
        return "REJECT", None, f"unstable across years (worst dev {worst:.0%} > {STABILITY_TOL:.0%})"
    older = [v for k, v in vals.items() if k != "Y1"]
    if "Y1" in vals and older:
        hold = abs(vals["Y1"] / float(np.median(older)) - 1)
        if hold > STABILITY_TOL:
            return "REJECT", None, f"holdout failed (Y1 vs Y2-4 dev {hold:.0%})"
    val = round_sig(med)
    if current is not None and current > 0 and abs(val / current - 1) <= DEADBAND:
        return "KEEP", val, f"derived {val} within ±{DEADBAND:.0%} of current {current}"
    return "APPLY", val, f"stable {val} (worst yearly dev {worst:.0%}); current {current}"


# ── profiling ─────────────────────────────────────────────────────────────

def profile() -> None:
    CURRENT = load_current()
    now = pd.Timestamp.now(tz="UTC")
    profiles, proposals = {}, []
    for sym in SYMBOLS:
        paths = {tf: os.path.join(DATA_DIR, f"{sym}_{tf}.csv") for tf in ("D1", "H1")}
        if not os.path.exists(paths["H1"]):
            print(f"{sym}: no H1 data, skipped")
            continue
        h1 = pd.read_csv(paths["H1"], parse_dates=["dt"])
        d1 = pd.read_csv(paths["D1"], parse_dates=["dt"]) if os.path.exists(paths["D1"]) else None
        prof = {"coverage": {"h1_from": str(h1.dt.iloc[0].date()), "h1_bars": len(h1)}}

        # per-slice metrics on H1
        per = {m: {} for m in ("atr_p25", "atr_p90", "tr_p25", "sep_p50", "range_share",
                               "chop_share", "trend_share", "med_price", "atr_med")}
        for label, s in slices(h1, now):
            a = atr_pct(s).dropna()
            per["atr_p25"][label] = float(a.quantile(0.25))
            per["atr_p90"][label] = float(a.quantile(0.90))
            per["atr_med"][label] = float(a.median() / 100 * s["close"].median())  # ATR in price units
            per["tr_p25"][label] = float(true_range_pct(s).quantile(0.25))
            sep = ema_sep_pct(s).dropna()
            per["sep_p50"][label] = float(sep.quantile(0.50))
            per["range_share"][label] = float((sep < CURRENT["trend_sep_default"]).mean())
            er = kaufman_er(s).dropna()
            per["chop_share"][label] = float((er < ER_CHOP).mean())
            per["trend_share"][label] = float((er > ER_TREND).mean())
            per["med_price"][label] = float(s["close"].median())
        # favorable-move metric (H1, 2-bar lookahead ≡ 2h envelope)
        per["fav2h_p25"], per["fav2h_p50"] = {}, {}
        for label, s in slices(h1, now):
            f = fav_move_2h_pct(s).dropna()
            per["fav2h_p25"][label] = float(f.quantile(0.25))
            per["fav2h_p50"][label] = float(f.quantile(0.50))
        prof["slices"] = per
        if d1 is not None and len(d1) > 300:
            prof["bounce"] = bounce_stats(d1)

        # ---- proposals -----------------------------------------------------
        cur_lo, cur_hi = CURRENT["regime"].get(sym, CURRENT["regime_default"])
        v, val, why = judge(f"{sym} regime_low", per["atr_p25"], cur_lo)
        proposals.append(dict(asset=sym, param="HTF_REGIME low (ATR% p25)", current=cur_lo,
                              derived=val, verdict=v, why=why, slices=per["atr_p25"]))
        v, val, why = judge(f"{sym} regime_high", per["atr_p90"], cur_hi)
        if v == "REJECT" and per["atr_p90"]:
            # Dead-label escape hatch: if the current band sits >1.5× above EVERY
            # year's p90, the 'high_volatility' label can never fire. An unstable
            # metric still beats a dead one — use the conservative envelope
            # (max yearly p90) so the label is alive in the extreme tail only.
            mx = max(per["atr_p90"].values())
            if cur_hi > 1.5 * mx:
                v, val = "APPLY", round_sig(mx)
                why = (f"yearly p90s unstable, but current {cur_hi} is DEAD (>1.5× every "
                       f"year's p90, max {mx:.2f}); conservative envelope {val} keeps the label alive")
        proposals.append(dict(asset=sym, param="HTF_REGIME high (ATR% p90)", current=cur_hi,
                              derived=val, verdict=v, why=why, slices=per["atr_p90"]))

        cur_tp = CURRENT["tp_floor"].get(sym, CURRENT["tp_floor_default"])
        if per["fav2h_p25"]:
            v, val, why = judge(f"{sym} tp_floor", per["fav2h_p25"], cur_tp)
            # extra structural test: only flag the CURRENT floor as harmful if it
            # exceeds the MEDIAN reachable 2h move in every slice (GBP/EUR bug class)
            harmful = all(cur_tp > m for m in per["fav2h_p50"].values())
            if v == "APPLY" and val is not None and val < cur_tp and not harmful:
                v, why = "KEEP", (f"derived {val} < current {cur_tp}, but current floor is NOT "
                                  f"harmful (median 2h move clears it in every year) — no churn")
            proposals.append(dict(asset=sym, param="CS_MIN_DISTANCE_TO_TP_PCT", current=cur_tp,
                                  derived=val, verdict=v, why=why, slices=per["fav2h_p25"]))

        cur_sl = CURRENT["sl_floor"].get(sym, CURRENT["sl_floor_default"])
        # The %-SL floor is a DEAD-VOL BACKSTOP, deliberately kept BELOW the
        # dominant ATR_STOP_MULT×ATR(1h) floor (config.py: "only the dead-vol
        # backstop; the 0.6×ATR(1h) floor dominates in normal volatility").
        # Widening stops was evaluated 2026-06-11 and deliberately NOT shipped
        # (LP-03 standing decision), so the engine must never propose raising
        # this toward noise level. It AUDITS instead: flag only if the backstop
        # would DOMINATE the ATR floor in a typical hour (mis-set, EURUSD class).
        if per["atr_p25"]:
            ratio = {k: round(cur_sl / v, 2) for k, v in per["atr_p25"].items()}
            worst = max(ratio.values())
            proposals.append(dict(asset=sym, param="CS_MIN_DISTANCE_TO_SL_PCT (backstop audit)",
                                  current=cur_sl, derived=None,
                                  verdict="FLAG" if worst >= 1.0 else "OK",
                                  why=f"backstop/p25(ATR1h%) per year {ratio} "
                                      f"(≥1.0 ⇒ backstop would dominate the ATR floor)",
                                  slices=ratio))

        # trend-separation dead-label check (informational unless label is dead)
        rng = per["range_share"]
        if rng:
            share_med = float(np.median(list(rng.values())))
            dead = all(r < 0.05 or r > 0.95 for r in rng.values())
            v2, val2, why2 = judge(f"{sym} trend_sep", per["sep_p50"], CURRENT["trend_sep_default"])
            if dead and v2 != "REJECT":
                proposals.append(dict(asset=sym, param="HTF_TREND_MIN_SEPARATION_PCT",
                                      current=CURRENT["trend_sep_default"], derived=val2, verdict="APPLY",
                                      why=f"'range' label dead (share {share_med:.1%}); p50 separation {val2}",
                                      slices=per["sep_p50"]))
            else:
                proposals.append(dict(asset=sym, param="HTF_TREND_MIN_SEPARATION_PCT",
                                      current=CURRENT["trend_sep_default"], derived=None, verdict="KEEP",
                                      why=f"'range' label alive (median share {share_med:.1%}) — default fine",
                                      slices=rng))

        # registry cap-vs-floor contradiction audit (EURUSD/GBPUSD bug class)
        cap = CURRENT["max_sl_distance"].get(sym)
        mult = CURRENT["atr_stop_mult"].get(sym, CURRENT["atr_stop_mult_default"])
        if cap and per["atr_med"] and per["med_price"]:
            viol = {}
            for label in per["atr_med"]:
                floor_price = max(cur_sl / 100 * per["med_price"][label],
                                  mult * per["atr_med"][label])
                viol[label] = round(floor_price / cap, 2)   # >1 ⇒ cap binds below floor
            worst = max(viol.values())
            proposals.append(dict(asset=sym, param="registry max_sl_distance sanity",
                                  current=cap, derived=None,
                                  verdict="FLAG" if worst >= 1.0 else "OK",
                                  why=f"SL-floor/cap ratio per year {viol} (≥1.0 = EURUSD bug class)",
                                  slices=viol))
        profiles[sym] = prof

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, "asset_profiles.json"), "w") as f:
        json.dump({"generated": str(now), "years": YEARS, "stability_tol": STABILITY_TOL,
                   "deadband": DEADBAND, "profiles": profiles, "proposals": proposals}, f, indent=1)

    print(f"\n{'asset':7} {'param':34} {'current':>9} {'derived':>8} verdict  why")
    for p in proposals:
        print(f"{p['asset']:7} {p['param']:34} {str(p['current']):>9} "
              f"{str(p['derived']):>8} {p['verdict']:7} {p['why']}")
    print(f"\nwrote {os.path.join(DATA_DIR, 'asset_profiles.json')}")

    # session table for the runtime session-aware SL floor + ER-percentile context
    sess = session_stats(now)
    sess_path = os.path.join(DATA_DIR, "session_stats.json")
    with open(sess_path, "w") as f:
        json.dump({"generated": str(now), "years": YEARS,
                   "sessions": {k: list(v) for k, v in SESSIONS.items()},
                   "definition": ("tr_pct: quantiles of H1 true-range%% per UTC session; "
                                  "er_deciles: p10..p90 of kaufman_er(24) per session"),
                   "assets": sess}, f, indent=1)
    print(f"\n{'asset':7} {'session':8} {'TR% p50':>8} {'p60':>7} {'p75':>7} {'stab':>6}  er p10/p50/p90")
    for sym, per in sess.items():
        for s, v in per.items():
            er = v["er_deciles"] or []
            er_s = f"{er[0]:.2f}/{er[4]:.2f}/{er[8]:.2f}" if len(er) == 9 else "-"
            print(f"{sym:7} {s:8} {v['tr_pct']['p50']:8.3f} {v['tr_pct']['p60']:7.3f} "
                  f"{v['tr_pct']['p75']:7.3f} {v['stability_tr_p60']:6.2f}  {er_s}")
    print(f"wrote {sess_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pull", action="store_true")
    ap.add_argument("--profile", action="store_true")
    a = ap.parse_args()
    if a.pull or not a.profile:
        pull()
    if a.profile or not a.pull:
        profile()
