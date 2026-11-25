"""Project-wide constants & paths"""
from pathlib import Path
import os
from .asset_registry import (
    guard_dict,
    instrument_config_dict,
    DEFAULT_MIN_SL_DISTANCE,
    DEFAULT_MIN_TARGET_DISTANCE,
    DEFAULT_MAX_SL_DISTANCE,
    DEFAULT_MAX_ENTRY_DRIFT,
    DEFAULT_FILTER_MIN_TARGET_POINTS,
    DEFAULT_FILTER_SLIPPAGE,
)
ROOT        = Path(__file__).resolve().parents[2]
DATA_DIR    = ROOT / "data" / "processed"
RESULTS_DIR = ROOT / "results"
# Centralized logs directory under results; individual runs will
# write unique files here (see packages/core/logger.py)
LOGS_DIR    = RESULTS_DIR / "logs"
MODEL_3     = "gpt-3.5-turbo"
MODEL_4     = "gpt-4o"        # or 'gpt-4-turbo'
MODEL_4O_MINI = "gpt-4o-mini"

# Gemini model constants
GEMINI_FLASH = "gemini-2.5-flash"

# Anthropic model constants
CLAUDE_HAIKU = "claude-haiku-4-5-20251001"
CLAUDE_SONNET = "claude-sonnet-4-6"
CLAUDE_SONNET_5 = "claude-sonnet-5"
CLAUDE_SONNET_55 = "claude-sonnet-5-5"

# ── Prompt caching (2026-08-14) ───────────────────────────────────────────
# Measured on the 2026-07 logs (2,176 calls / 20.48M tokens / 20 days): the
# stable prefix of every prompt is the system block — 31-36% of an expert
# prompt, 51-55% of a judge prompt, ~39% call-mix weighted. The remainder is
# candles and live context, which change every cycle and cannot cache.
# Expected saving ~15-19% of API spend. Mechanics live in prompt_cache.py.
#
# TTL is NOT cosmetic here. Live, this system runs ~1.15 cycles per asset per
# hour, so a 5-minute cache is cold at every call and would pay the 1.25x write
# premium for zero reads. 1h costs 2x to write but actually gets read.
# Backtests replay cycles back-to-back, so 5m is both sufficient and cheaper.
#
# Failure posture: prompt_cache.apply_cache_control() is total — on any error it
# returns the uncached kwargs and logs a WARNING. Setting this False restores
# byte-identical pre-08-14 request construction.
PROMPT_CACHE_ENABLED = os.getenv("PROMPT_CACHE_ENABLED", "true").lower() != "false"
PROMPT_CACHE_TTL = os.getenv("PROMPT_CACHE_TTL", "1h")  # "1h" live | "5m" backtest

# Amazon Bedrock model IDs (Converse API)
BEDROCK_NOVA_PRO   = "us.amazon.nova-pro-v1:0"
BEDROCK_NOVA_LITE  = "us.amazon.nova-lite-v1:0"
BEDROCK_NOVA_MICRO = "us.amazon.nova-micro-v1:0"
BEDROCK_LLAMA_70B  = "us.meta.llama3-1-70b-instruct-v1:0"
BEDROCK_CLAUDE_37_SONNET = "us.anthropic.claude-3-7-sonnet-20250219-v1:0"
BEDROCK_CLAUDE_35_HAIKU  = "us.anthropic.claude-3-5-haiku-20241022-v1:0"

# Claude Sonnet 4.6 via Bedrock EU inference profile
# Override with BEDROCK_CLAUDE_SONNET_46_ARN env var if the account ID changes.
BEDROCK_CLAUDE_SONNET_46 = os.getenv(
    "BEDROCK_CLAUDE_SONNET_46_ARN",
    "arn:aws:bedrock:eu-north-1:713939170911:inference-profile/eu.anthropic.claude-sonnet-4-6",
)

# AWS credentials for Bedrock (resolved by boto3 credential chain if empty)
AWS_ACCESS_KEY_ID     = os.getenv("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "")
AWS_SESSION_TOKEN     = os.getenv("AWS_SESSION_TOKEN", "")
AWS_REGION            = os.getenv("AWS_REGION", "eu-north-1")  # default: EU region for Bedrock

# AI provider selection: "openai", "gemini", "anthropic", or "bedrock"
# Switch between providers via the AI_PROVIDER env var or gpt_cfg["provider"].
AI_PROVIDER_ENV = "AI_PROVIDER"
AI_PROVIDER = os.getenv(AI_PROVIDER_ENV, "openai")  # default to openai

# Environment variable name for the OpenAI API key. Tests rely on this being
# configurable so we expose both the variable name and the loaded value.
OPENAI_ENV = "OPENAI_API_KEY"
# Load the API key once at import time so modules can simply import
# ``OPENAI_API_KEY`` without repeating environment lookups.
OPENAI_API_KEY = os.getenv(OPENAI_ENV, "")

# Google Gemini API key
GEMINI_API_KEY_ENV = "GEMINI_API_KEY"
GEMINI_API_KEY = os.getenv(GEMINI_API_KEY_ENV, "")

# Anthropic API key
ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"
ANTHROPIC_API_KEY = os.getenv(ANTHROPIC_API_KEY_ENV, "")

# ── Anthropic API resilience (network/transport reliability) ────────────
# The Anthropic SDK natively retries APIConnectionError, 408/409/429, 5xx and
# 529 (overloaded) using exponential backoff + jitter and honours any
# ``retry-after`` header.  Raising max_retries from the SDK default of 2 lets
# transient connection blips recover *within a single call* instead of
# surfacing as hard failures (these were ~9% of calls in production logs).
ANTHROPIC_MAX_RETRIES = int(os.getenv("ANTHROPIC_MAX_RETRIES", "5"))
# Per-request timeouts handed to the SDK's httpx client.
ANTHROPIC_TIMEOUT_SECONDS = float(os.getenv("ANTHROPIC_TIMEOUT_SECONDS", "60"))
ANTHROPIC_CONNECT_TIMEOUT_SECONDS = float(os.getenv("ANTHROPIC_CONNECT_TIMEOUT_SECONDS", "10"))

# Legacy assistant ID - no longer used with chat completion API
ASSISTANT_ID = os.getenv("OPENAI_ASSISTANT_ID", "")


# MongoDB configuration
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB_NAME = "nifty_backtest"
MONGO_COLLECTION = "results"
MONGO_REPORTS_COLLECTION = "reports"
MONGO_TRADES_COLLECTION = "trades"

# ── Analytics persistence (log-only mode) ────────────────────────────────────
# When False (the default deployment posture), the high-volume *analytics*
# collections — multi_expert_cycles, comprehensive_trades (+exits),
# gpt_responses, slot_candle_matches, hourly_candles, strategy_expiries — are
# NOT written to MongoDB. Every such record is instead emitted once to the
# structured JSON event log (src.common.logger, ``event:<name>``) so the full
# lifecycle is reconstructable offline (scripts/logs_to_report.py).
#
# Operational collections are NEVER gated by this flag: monitor_state (crash
# recovery), account_risk baseline, the candle cache, configs/agents/prompts/
# wallets and broker sessions all keep persisting normally.
#
# Default True preserves current PRODUCTION behaviour (analytics persisted to
# Mongo). Set PERSIST_ANALYTICS_TO_MONGO=0 to opt into log-only mode. The env var
# overrides this constant at runtime (see packages/db/persistence_gate.py).
#
# WARNING — do NOT flip this default to log-only until the readers below are made
# log-aware, or live business logic is silently disabled:
#   * comprehensive_trades is load-bearing, not pure analytics — the live
#     over-trading throttle (packages/core/risk/trade_throttle.py) derives the
#     daily trade cap + consecutive-SL cooldown by reading it back; with no docs
#     written, both brakes never fire. The recent-trades prompt block also reads
#     it (mitigated by an in-process registry, but only for legs that opened in
#     the same process).
# See docs/engineering/log-only-persistence-risks.md for the full audit.
PERSIST_ANALYTICS_TO_MONGO = os.getenv(
    "PERSIST_ANALYTICS_TO_MONGO", "true"
).strip().lower() in {"1", "true", "yes", "y", "on"}

# Frontend redirect after OAuth
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")

# Agent configuration
USE_CODE_SNIPER = True
USE_4O_MINI_RESPONSE_FORMAT = False

# Backtest: minimum |entry − stop| as % of entry for non-equity (e.g. crypto) runs.
# Prevents microscopic stops that sit inside a single 5m bar's range.
BACKTEST_MIN_SL_PCT_CRYPTO = 0.15  # 0.15% — was 0.10%, raised to push stops behind real structure

# ── Trade guards ──────────────────────────────────────────────────────────
# Minimum risk-reward ratio required to accept a trade.  Anything below is
# considered a poor-edge setup and is rejected before simulation.
BACKTEST_MIN_RR = 1.5

# Maximum number of trades allowed per calendar day (UTC).  After this cap
# is reached the runner skips new GPT calls until the next day.
BACKTEST_MAX_TRADES_PER_DAY = 8

# Number of iterations to cool-down (skip) after consecutive stop-loss results.
# Triggers after BACKTEST_COOLDOWN_SL_THRESHOLD consecutive SLs.
BACKTEST_COOLDOWN_AFTER_SL = 1
BACKTEST_COOLDOWN_SL_THRESHOLD = 3  # require 3 consecutive SLs before cooldown

# Anti-scalp: minimum absolute distance between entry and SL in *price*
# units.  Per-asset defaults.  Trades with risk smaller than this are
# rejected as noise / scalp-level setups.
BACKTEST_MIN_SL_DISTANCE: dict = guard_dict("min_sl_distance", DEFAULT_MIN_SL_DISTANCE)

# Anti-scalp: minimum target distance (entry → TP) in price units.
# Rejects tiny-target setups that don't justify the GPT cost + spread.
BACKTEST_MIN_TARGET_DISTANCE: dict = guard_dict("min_target_distance", DEFAULT_MIN_TARGET_DISTANCE)
 
# Anti-runaway: maximum absolute SL distance in price units.
# Rejects catastrophically wide stops (e.g. iter 1267 held 49 hrs / -$452).
# Works for all assets: forex, crypto, indices.
BACKTEST_MAX_SL_DISTANCE: dict = guard_dict("max_sl_distance", DEFAULT_MAX_SL_DISTANCE)

# Entry freshness: max allowed drift between GPT's quoted entry price and
# the entry candle's open.  Rejects stale / ghost prices (21.7% issue in
# GBPUSD validation report).  None = skip check for that asset.
BACKTEST_MAX_ENTRY_DRIFT: dict = guard_dict("max_entry_drift", DEFAULT_MAX_ENTRY_DRIFT)

# ── Instrument specs for backtest PnL ────────────────────────────────────
# pip_size          : price movement per pip (price-unit, not money)
# pip_value_per_lot : USD value of one pip on one standard lot
# default_lot       : default lot size used when not specified by the caller
#
# Formula: profit = (price_diff / pip_size) * pip_value_per_lot * lot_size
#
# For BTCUSD/XAUUSD/ETHUSD: pip_size=1.0, pip_value=1.0 → profit = price_diff
# (preserves legacy behaviour — no change to existing results)
# For GBPUSD/EURUSD std lot: 1 pip = 0.0001 price = $10 profit
BACKTEST_INSTRUMENT_CONFIG: dict = instrument_config_dict()

# Multi-expert orchestrator configuration (flipped to default after soak testing)
USE_MULTI_EXPERT = True  # Default to multi-expert orchestrator (was False during development)

# Expert response validation configuration
VALIDATE_EXPERT_RESPONSES = False  # If False, skip validation and pass responses directly to judge


# ═══════════════════════════════════════════════════════════════════════════
# CENTRALIZED PRODUCTION CONFIGURATION
# All runtime constants consolidated here.  Change values in one place.
# ═══════════════════════════════════════════════════════════════════════════


# ── Multi-Expert Orchestrator ─────────────────────────────────────────────
EXPERT_TIMEOUT_SECONDS = 90.0
EXPERT_KINDS: list[str] = ["trend"]
JUDGE_MAX_PROPOSALS = 2
JUDGE_SAFETY_BUFFER_SECONDS = 2.0
JUDGE_CANDLE_RANGE: tuple[int, int] = (10, 30)
SCORE_RR_WEIGHT = 1.0
SCORE_CONFIDENCE_WEIGHT = 1.0
# ── TTL horizon (2026-08-06) ──────────────────────────────────────────────
# Was {60, 90, 120}. The reason for the change is the definition of the
# system's own headline edge metric.
#
# WP-04 — "direction-finding quality is real", 62% pooled, the number that says
# the prompts are not the limiter — counts a trade as a directional success when
# price moves ≥1R in the thesis direction **during the trade OR WITHIN 3 HOURS
# AFTER EXIT**. The maximum holding period was 120 minutes. So the metric that
# proves the edge exists is measured over a window the position was never
# allowed to survive into: a material share of the 70-ish percent is, by
# construction, moves the system correctly predicted and structurally could not
# be present for.
#
# The ledger says the same thing in dollars. 07-27: XAG entered 05:20, stopped
# out, TP1 reached 12:10 = +2.29R — seven hours later. 07-17: XAU timed out at
# −0.17R, then ran +3.04R. 07-15: GBP +5.17R and EUR +2.50R, both post-exit.
# These are not near-misses on a 2h clock; they are a different time constant.
#
# 90/180/240 spans the observed horizon while staying intraday — the 20:55
# maintenance pre-close and the weekend-gap guard still force-close over every
# gap, so the longest TTL cannot carry a position through a session break.
TTL_MINUTES_ALLOWED: tuple[int, ...] = (90, 180, 240)

# Default used by every fallback path (flat signals, "wait" returns, function
# defaults). Before 2026-08-06 the value 60 was hardcoded at six separate call
# sites across multi_expert.py, market_structure.py and agents/runner.py, all of
# which silently became ILLEGAL the moment the enum moved — a flat signal built
# with the old default now fails schema validation and takes the whole cycle
# down. That latent duplication is the actual defect; this constant and
# ``coerce_ttl`` below are the fix, and tests pin that no literal survives.
TTL_DEFAULT_MINUTES = 180


def coerce_ttl(value: object, default: int | None = None) -> int:
    """Snap *value* to the nearest allowed TTL.

    Used where a TTL arrives from outside the schema (a model's dynamic_config,
    a legacy document) and must be made legal rather than rejected. Clamping to
    a numeric range is NOT enough — the schema is an enum, so ``min(180, x)``
    happily produces 47, which then fails validation. Snapping to the nearest
    allowed rung is the only coercion that always yields a valid signal.
    """
    fallback = TTL_DEFAULT_MINUTES if default is None else default
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return min(TTL_MINUTES_ALLOWED, key=lambda t: (abs(t - v), t))

# Scoring nudge across the allowed TTLs (subtracted from a proposal's score).
# Re-centred on the new enum: the middle rung is cheapest, the shortest is
# penalised more than before because a 90-minute thesis on a 3-hour edge is now
# the exceptional choice rather than the default one.
SCORE_TTL_PENALTIES: dict[int, float] = {90: 0.15, 180: 0.05, 240: 0.12}
WINDOW_EXTENSION_FACTOR = 0.50
DEFAULT_MAX_TOTAL_TIME_SECONDS = 120
DEFAULT_JUDGE_TIMEOUT_SECONDS = 60
DEFAULT_EXPERT_TIMEOUT_SECONDS = 90
MIN_CANDLES_FOR_JUDGE = 10
MAX_CANDLES_FOR_JUDGE = 30
DEFAULT_CANDLES_FOR_JUDGE = 20
MIN_CANDLES_FOR_CODE_EXECUTION = 41
HISTORICAL_CANDLES_COUNT = 40


# ── Code Sniper Executor ─────────────────────────────────────────────────
# Minimum entry→target distance ("large candle protection") floors, as a
# percentage of entry price. These are the fall-back defaults for any asset
# not listed in the per-asset overrides below.
CS_MIN_DISTANCE_TO_TP_PCT = 0.2
CS_MIN_DISTANCE_TO_SL_PCT = 0.1

# Per-asset overrides. FX majors move far less in percentage terms than crypto,
# so the flat 0.3%/0.1% floor rejected almost every valid GBPUSD scalp setup
# (targets of ~6-11 pips ≈ 0.06-0.08% — zero GBP trades in a full day).
#
# Deliberately scoped to GBPUSD only: BTCUSD and XAUUSD give good entry/exit
# points on the default floor today, so they are intentionally left on the
# defaults above to avoid regressing them.
#
# Keys are the clean asset id (case-insensitive; the broker's demo 'm' suffix
# is tolerated). Unlisted assets use the defaults above.
CS_MIN_DISTANCE_TO_TP_PCT_BY_ASSET: dict[str, float] = {}
CS_MIN_DISTANCE_TO_SL_PCT_BY_ASSET: dict[str, float] = {}

# RR gates (2026-06-11): the minimum gate moved from TP2 to TP1 so a stretchy
# TP2 can no longer rescue a setup whose realistic first target pays < 1.2R
# (report §6: executor used to enforce ≥1.5 on TP2 while prompts promised ≥1.5
# on TP1 — trades could execute with RR-to-TP1 < 1). The max clamp stays on TP2.
CS_MIN_RR_RATIO = 1.5          # legacy TP2 gate (no longer the reject gate)
CS_MIN_RR_RATIO_TP1 = 1.2      # hard reject below this, measured entry→TP1
CS_MAX_RR_RATIO = 6.0
# Candle window for generated-code execution. MUST match the judge's template
# (41 = 40 historical + 1 current — MIN_CANDLES_FOR_CODE_EXECUTION); the
# monitor used to run the code with 30 bars while the judge was told 41.
CS_EXPECTED_HISTORICAL_CANDLES = 40
CS_EXPECTED_TOTAL_CANDLES = 41
# Risk per trade. 2026-08-07: 1.5% → 2.5% on the account owner's instruction.
#
# Why it moves: 10%/day is arithmetically unreachable at 1.5%. Even at perfect
# 1R capture of the full WP-04 70%, `0.40R × 9.9 trades × $15 = $59/day` ≈ 6% on
# $1,000 — and that is the ceiling, not the expectation. The target needs ~2.5%.
#
# What is being skipped, stated plainly: the PLAN_2026-08-06 §4 ladder gates
# 2.0% behind "20+ closed trades, gross positive, average winner ≥ 2.0R" and
# 2.5% behind 20 more at "W ≥ 2.5R + rolling-5-day net positive". Neither gate
# has been met — gross over the 07-13→07-27 freeze was −$36.96 on 109 positions.
# 2.5% multiplies whatever the true expectancy is; if it is still negative, this
# drains ~1.67× faster. That is the owner's call and it is taken deliberately.
#
# 2.5% remains the ladder's HARD CEILING — §4's "never above 3%" is unchanged
# and this does not authorise a further rung.
#
# Two coupled consequences, neither of which is a separate config change:
#   · Drawdown halt: ACCOUNT_MAX_DAILY_DD_PCT (10%) now trips after ~4 full-R
#     losses instead of ~6.7. The halt is nominally unchanged but bites sooner.
#   · Risk-budget gate: RISK_BUDGET_MAX_OVERSHOOT_MULT measures min-lot
#     overshoot against `balance × risk_pct`, so a larger budget lowers every
#     overshoot multiple by 1.5/2.5. Setups that scored 1.25×–2.08× under the
#     old budget now land ≤1.25× and are ADMITTED rather than skipped. Their
#     dollar risk is unchanged (min_lot is min_lot) — what moved is the budget
#     they are measured against, which is the gate behaving as designed. The
#     measured XAG stop-outs (2.12–2.34×) still skip, but at 1.27–1.40× the
#     margin is now thin.
CS_DEFAULT_RISK_PCT = 0.01
CS_LIVE_RISK_PCT = 0.01

# ── Replay risk-budget gate / LP-01 min-lot overshoot (2026-08-06) ────────────
# The live executor skips any lot whose modeled stop loss exceeds its budget.
# LP-01 is the largest single distortion in the live record and it is NOT a
# strategy defect — it is arithmetic. When the risk-correct lot falls below the
# broker's min_lot the order is forced UP to min_lot, so the realised risk is
# whatever min_lot happens to imply. Measured consequences over the freeze:
#   · week 1 (+$46 net) decomposed to +$4 of price edge + ~$42 of favourable
#     min-lot beta — i.e. 91% of a "good week" was the clamp landing well;
#   · week 2 was the same beta with the sign flipped: XAG −$64.55 while every
#     other asset combined was +$0.04;
#   · individual XAG stop-outs ran 2.12–2.34× intended risk (3.5% of account
#     per trade at a $390 balance), against an intended 1.5%.
# Until this is closed, every expectancy reading the system produces is
# corrupted by a random ±2–3× multiplier, so no other change can be measured.
#
# The gate: when the floored lot's implied risk exceeds the intended risk budget
# by more than RISK_BUDGET_MAX_OVERSHOOT_MULT, SKIP the trade instead of taking
# it at an unrequested size.
#
# This is deliberately NOT an asset blacklist. The 2026-07-09 decision declining
# XAG/metal suspension stands and is not re-litigated here — this rule never
# names an asset. It is account-size-adaptive in both directions: at a ~$400
# balance it refuses most min-lot silver (whose noise-proof stop implies ~$15 of
# risk against a $6 budget), and it re-admits that same silver automatically,
# with no config change, once the balance can afford it. The refusals are the
# signal, not a malfunction: an asset that cannot be traded inside the risk
# budget is an asset this account cannot yet trade.
#
# Set RISK_BUDGET_SKIP_ENABLED = False to fall back to the pre-08-06 behaviour
# (log the overshoot, take the trade anyway).
RISK_BUDGET_SKIP_ENABLED = True
RISK_BUDGET_MAX_OVERSHOOT_MULT = 1.25   # skip above 1.25× the intended risk
RISK_BUDGET_WARN_OVERSHOOT_MULT = 1.10  # log-only warning threshold (unchanged)
# Fill-fidelity gate (2026-07-02 audit F2): orders are MARKET, so the real fill
# is the triggering bar's close, not the code's ENTRY constant. Reject when the
# trigger close sits further from ENTRY than this fraction of the stop distance
# — beyond it the judged RR no longer describes the actual trade.
CS_MAX_ENTRY_DEVIATION_RISK_MULT = 0.4

# ── Scheduled-release blackout / LP-04 (2026-08-06) ───────────────────────────
# LP-04 was promoted to CONFIRMED at the 2026-07-18 weekly review on three
# day-occurrences: 07-07 −$41.17, 07-10 −$31.98, and 07-14 −$38.20 (of which
# $22.66 was pure slippage straight THROUGH an XAUUSD stop on the 12:30 CPI
# bar). ≈ $111 across three incidents on a ~$450 account.
#
# Root cause is not judgement: the judge has no calendar, so a release bar is
# indistinguishable to it from any other bar. Stops do not help — the 07-14
# exhibit gapped through one — so the only defence is not being in the market.
#
# This is the static version: fixed UTC windows covering the 12:30 and 14:00
# US release slots (CPI/PPI/NFP/claims and the 14:00 block respectively), which
# is where all three measured incidents happened. It blocks NEW entries only and
# never touches an open position — a position already working through a release
# is a management decision the trail engine already handles.
#
# The proper fix is an economic-calendar feed keyed on actual high-impact events;
# this is the 90%-of-the-value version that needs no new dependency. Windows are
# (start_hh, start_mm, end_hh, end_mm) in UTC, inclusive of start, exclusive of
# end. Note these are wall-clock UTC: US releases shift by an hour across US DST
# transitions, so the windows are padded rather than exact.
NEWS_BLACKOUT_ENABLED = False
NEWS_BLACKOUT_WINDOWS_UTC: tuple[tuple[int, int, int, int], ...] = (
    (12, 15, 12, 40),   # 12:30 ET-morning block (CPI/PPI/NFP/jobless claims)
    (13, 15, 13, 40),   # same block, US-DST-shifted
    (14, 15, 14, 40),   # 14:00/14:30 block (the 07-10 −$31.98 incident)
)
# Assets exempt from the blackout. Crypto trades 24/7 and has no scheduled
# US-release sensitivity in the measured record — all three LP-04 exhibits were
# metals/indices/FX. Empty tuple = blackout applies to everything.
NEWS_BLACKOUT_EXEMPT_ASSETS: tuple[str, ...] = ("BTCUSD", "ETHUSD")


# ── Correlated-instrument coherence (2026-07-22) ──────────────────────────────
# Assets that move together. An open position in one member BLOCKS an OPPOSING
# entry in another (the guard lives in services/code_sniper_monitor.py).
#
# Why: on 2026-07-21 the app held XAU sell + XAG buy twice in one session and the
# user closed the silver leg manually both times — gold won both (+$3.82/+$3.95).
# The per-asset guard could not see it: it only ever looked for positions in the
# SAME symbol, so a cross-symbol contradiction sailed through.
#
# Validated on 3 years of live Real8 data (scripts/research/metals_correlation.py):
# XAU/XAG returns correlate r = +0.77…+0.80 at EVERY horizon (D1/H4/H1/M15/M5),
# same-direction on 77–80% of bars and 96.8–100% of the largest 5% of moves;
# rolling 60d correlation never fell below +0.396 and was never negative.
# Disagreement is confined to sub-noise moves (smallest quintile ≈58% = coin flip,
# median gold move 0.009%), which sit BELOW XAG's own commission (~$0.95 per 0.01
# lot ≈ 0.033% of price) — so blocking opposing pairs forfeits no tradeable edge.
#
# Deliberately NOT blocked: SAME-side entries in a group. That is a concentration
# question (double LP-01 min-lot beta), unmeasured, and needs its own decision.
# Membership is matched suffix-safely (XAUUSDm / XAUUSD.raw → XAUUSD).
CORRELATION_GROUPS: dict[str, tuple[str, ...]] = {
    "metals": ("XAUUSD", "XAGUSD"),
}


# ═══════════════════════════════════════════════════════════════════════════
# HIGHER-TIMEFRAME CONTEXT, REGIME & RISK  (config scaffolding — task 1)
# ───────────────────────────────────────────────────────────────────────────
# Every value below is a *config knob* so nothing downstream is hardcoded, and
# every per-asset override falls back to a portable default computed from the
# asset's OWN data (so this works for BTC/XAU/GBP/… without magic numbers).
#
# IMPORTANT: all feature-flags here default to the CURRENT behavior, so adding
# this block alone changes nothing. Each flag is turned on by its own task as
# it is wired and tested.
# ═══════════════════════════════════════════════════════════════════════════

# ── HTF context master switch + candle counts (task 3/4) ─────────────────
HTF_CONTEXT_ENABLED = True  # ON: fetch HTF + inject trend/regime/ATR into expert+judge context

# Candles per higher timeframe. Kept modest to bound token cost. Per-asset
# overrides may narrow/widen this for specific instruments.
HTF_CANDLE_COUNTS: dict[str, int] = {"1h": 120, "4h": 90, "1d": 60}
HTF_CANDLE_COUNTS_BY_ASSET: dict[str, dict[str, int]] = {}

# ── HTF trend classification (task 4) ────────────────────────────────────
# EMA periods are scale-free, so the same values port across assets. The
# separation threshold is expressed as a percent of price (relative), and is
# per-asset overridable for instruments that trend more tightly.
HTF_TREND_EMA_FAST = 20
HTF_TREND_EMA_SLOW = 50
HTF_TREND_MIN_SEPARATION_PCT = 0.05  # |fast/slow-1|*100 below this ⇒ 'range'
HTF_TREND_MIN_SEPARATION_PCT_BY_ASSET: dict[str, float] = {}

# ── Regime classification via ATR% (task 4) ──────────────────────────────
# Bands on ATR/price*100. Computed from each asset's own ATR, so the relative
# classification is portable; per-asset overrides available where vol regimes
# differ structurally (crypto vs FX).
HTF_REGIME_HIGH_VOL_ATR_PCT = 2.0
HTF_REGIME_LOW_VOL_ATR_PCT = 0.2
HTF_REGIME_ATR_PCT_BY_ASSET: dict[str, dict[str, float]] = {}

# Stop guidance uses one 14-period hourly ATR before applying distance bounds.
ATR_STOP_ENABLED = True
ATR_STOP_MULT = 1.0
ATR_STOP_MULT_BY_ASSET: dict[str, float] = {}
ATR_STOP_REFERENCE_TF = "1h"
ATR_STOP_PERIOD = 14

# Optional session statistics are configured per symbol.
SESSION_NOISE_FLOOR_ENABLED_BY_ASSET: dict[str, bool] = {}

SESSION_PCTILE_CONTEXT_ENABLED_BY_ASSET: dict[str, bool] = {}
SESSION_NOISE_FLOOR_QUANTILE = "p60"  # "the stop survives a busier-than-median hour"
SESSION_NOISE_FLOOR_MULT = 1.0
SESSION_STATS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "profiling", "session_stats.json",
)

SL_ANCHOR_MODE = "log"            # "off" | "log" | "widen" — DEFAULT for any asset
SL_ANCHOR_MODE_BY_ASSET: dict[str, str] = {}
SL_ANCHOR_PAD_ATR_FRAC = 0.1      # pad beyond the anchor, as a fraction of ATR(1h)


def sl_anchor_mode_for(asset: str | None, default: str | None = None) -> str:
    """Resolve the sl_anchor mode for *asset*.

    Mirrors SESSION_NOISE_FLOOR_ENABLED_BY_ASSET: an explicit per-asset entry
    wins, everything else falls back to *default* (or SL_ANCHOR_MODE). Callers
    pass their own module-level SL_ANCHOR_MODE as *default* so tests can
    monkeypatch it on the importing module — see the dual-import-path note in
    tests/.

    Key normalisation is deliberately IDENTICAL to session_stats._asset_key
    (strip, upper, drop a trailing broker 'm' suffix on names longer than 4) so
    the two per-asset gates of the same redesign can never disagree about what
    'EURUSD' means. Not imported from there because session_stats imports this
    module — replicated with this note instead of creating a cycle.
    """
    fallback = SL_ANCHOR_MODE if default is None else default
    key = (asset or "").strip().upper()
    if not key:
        return fallback
    if key.endswith("M") and len(key) > 4:
        key = key[:-1]
    return SL_ANCHOR_MODE_BY_ASSET.get(key, fallback)


# ── Session-tape observation (2026-07-25, LP-08 Phase 0 — measurement only) ──
# LP-08 (CONFIRMED, 4 days: 07-07/09/16/23): htf_trend is computed from 1h/4h/1d
# lookbacks, so after an intraday reversal it stays stale for hours and
# "with-trend" entries fire counter-trend against the live session (07-23: four
# htf_up buys into a −1.97% EU downtrend = the entire −$37.68 day).
# session_tape measures what the CURRENT session is actually doing — direction
# since session open, elapsed-session Kaufman ER, and LP-14's trailing-2h ER as
# the spike-vs-sustained persistence check — so the label-vs-tape conflict
# becomes observable per cycle instead of only in post-day audits.
# Modes: "off" = inert · "log" = compute + emit a label_tape_conflict log line,
# nothing else changes (freeze-safe measurement; the default) · "context" =
# additionally inject session_tape into the prompt HTF_CONTEXT (Phase 2a —
# requires explicit user approval per asset; NOBODY is enabled by default and
# the prompt glossary for it does not exist yet).
SESSION_TAPE_MODE = "log"
# Phase 2a enable (2026-07-27, USER-approved despite gate n=12<25 — P&L now
# prioritised over sample purity; decision recorded). Scope follows the replay
# evidence EXACTLY (LABEL_TAPE_CALIBRATION_2026-07-25.md): every deep-conflict
# LOSS was XAG/USTEC/XAU (1W-4L −$34.63 at ≥1% session move, nothing that
# reached TP1 given up) → those three get the context + glossary hint.
# BTCUSD stays "log": its only conflict trade was the +2.2R WINNER (shallow) —
# do not risk the winner class. EUR/GBP stay "log": their conflicts were
# shallow/breakeven and FX already has the session-floor redesign.
# ⚠️ This deliberately ends the byte-identical non-FX control group for
# XAU/XAG/USTEC as of the next restart — the n≈200 review must split those
# assets' samples at this boundary. LP-09 parse re-check REQUIRED next audit
# (prompt text changed).
SESSION_TAPE_MODE_BY_ASSET: dict[str, str] = {}
SESSION_TAPE_MIN_BARS = 12     # min elapsed 5m bars in-session before the signal exists
# Calibrated 2026-07-25 by scripts/research/label_tape_replay.py on the 99
# broker-matched freeze trades: at 0.30 the flag misses the 07-23 flagship
# entries (session ER read 0.21-0.26 AT ENTRY; the audit's 0.348 was the
# full-session number, known only after the fact) and the pool collapses to
# n=6. At 0.20/0.15 the pool is n=12, WR 33.3%, −$49.69 — and label-side
# entries into a DEEP conflict (|session move| ≥ ~0.5%) are 1W-5L, avgR −0.59.
# See docs/trading_insights/design/LABEL_TAPE_CALIBRATION_2026-07-25.md.
SESSION_TAPE_MIN_ER = 0.20     # session ER at/above ⇒ the session is genuinely directional
SESSION_TAPE_MIN_ER_2H = 0.15  # LP-14 persistence floor: trailing-2h ER below ⇒ spike, not a move


def session_tape_mode_for(asset: str | None, default: str | None = None) -> str:
    """Resolve the session-tape mode for *asset*.

    Mirrors ``sl_anchor_mode_for`` exactly (same fallback contract, same key
    normalisation — see that docstring for why the normalisation is replicated
    instead of imported).
    """
    fallback = SESSION_TAPE_MODE if default is None else default
    key = (asset or "").strip().upper()
    if not key:
        return fallback
    if key.endswith("M") and len(key) > 4:
        key = key[:-1]
    return SESSION_TAPE_MODE_BY_ASSET.get(key, fallback)


# ── Trend agreement + aligned touch-fill (2026-07-29) ────────────────────
# TREND AGREEMENT is the strongest discriminator found in the freeze sample,
# and it is the only one that reproduced in two INDEPENDENT samples:
#
#   (1) the 109 broker-true trades that fired
#         1h=4h=1d unanimous   n= 33   61% WR   net  +$64.63
#         not unanimous        n= 71   43% WR   net -$136.63
#   (2) the strategies that armed, watched price TOUCH their entry, and still
#       never triggered — replayed on real M5 candles (n=79 of 259 expiries
#       that logged usable levels; see the proposed_levels note below)
#         aligned, with the agreement   n= 20   55% TP1   +6.4R left on table
#         not aligned                   n= 29   31% TP1   -8.2R correctly dodged
#
# Same verdict from the trades we took and from the trades we missed: the
# unanimous-HTF class is where this system's edge lives, and roughly 38% of
# that class is currently forfeited to a trigger that never fires.
#
# ⚠️ HONEST LIMIT on (2): the +6.4R is concentrated — USTEC n=4 (+5.7R) and
# XAGUSD n=5 (+1.8R) carry it, while XAUUSD is the largest single cell and is
# NEGATIVE (n=8, −2.5R: gold's trigger caution is earning its keep). Per-asset
# cells are 1–9 samples, far too thin to scope on, so the class-level rule is
# what ships and XAUUSD — the one asset with a real contrary sample — observes
# in "log" mode instead of filling.
#
# htf_trend (the existing consensus label) is NOT a substitute: it marked 95 of
# 104 trades "with-trend", so it separates nothing (−$0.40/trade either side).
# Agreement is computed from trend_1h/trend_4h/trend_1d, which compute_htf_context
# already returns — this adds no new market maths, only a reading of it.
TREND_AGREEMENT_ENABLED = True   # compute + log + expose in context (inert on its own)

# Touch-fill: when a strategy is armed on a unanimous-HTF setup and trades WITH
# that agreement, a 5m bar whose range contains the judged entry is treated as
# the trigger, even if the judge's generated trigger code returned 'hold'.
# NOTHING downstream is skipped — the promoted decision still passes the
# fill-fidelity gate (so the bar's close must be within
# CS_MAX_ENTRY_DEVIATION_RISK_MULT of the judged entry), the volatility SL
# floor, the sl_anchor contract, the RR-to-TP1 gate and normal sizing. It only
# removes the requirement that the code's own pattern condition also be true.
# Modes: "off" = inert · "log" = would-have-filled events only (measurement)
#        · "fill" = promote to a real trade.
ALIGNED_TOUCH_FILL_MODE = "log"
ALIGNED_TOUCH_FILL_MODE_BY_ASSET: dict[str, str] = {}


def trend_agreement_from(htf: dict | None) -> str | None:
    """'up'/'down' when trend_1h, trend_4h and trend_1d all agree, else None.

    A missing timeframe means no agreement — silence is not consensus.
    """
    if not htf:
        return None
    t1, t4, td = htf.get("trend_1h"), htf.get("trend_4h"), htf.get("trend_1d")
    if t1 and t1 == t4 == td and t1 in ("up", "down"):
        return t1
    return None


def aligned_touch_fill_mode_for(asset: str | None, default: str | None = None) -> str:
    """Resolve the touch-fill mode for *asset*.

    Mirrors ``sl_anchor_mode_for`` / ``session_tape_mode_for`` exactly (same
    fallback contract, same key normalisation).
    """
    fallback = ALIGNED_TOUCH_FILL_MODE if default is None else default
    key = (asset or "").strip().upper()
    if not key:
        return fallback
    if key.endswith("M") and len(key) > 4:
        key = key[:-1]
    return ALIGNED_TOUCH_FILL_MODE_BY_ASSET.get(key, fallback)


# ── Soft counter-trend weighting (task 6) ────────────────────────────────
# SOFT preference only by default — never a hard block, so good reversal/range
# setups survive. "off" = no effect; "soft" = confidence haircut; "gate" =
# require min confidence (opt-in). Per-asset overridable.
COUNTER_TREND_MODE = "soft"           # "off" | "soft" | "gate" (soft never drops trades)
COUNTER_TREND_CONFIDENCE_PENALTY = 0.15  # used when mode == "soft"
# Confidence bar for "gate" mode. 0.0 made the gate a documented no-op (every
# proposal has confidence ≥ 0.0 — 2026-07-03 analysis §4.5): BTC's protection
# against counter-trend calls never existed. 0.6 sits above the judge's
# observed mean/median confidence (0.40/0.45) so only genuinely high-conviction
# reversals pass, which is exactly what the comment below always promised.
COUNTER_TREND_MIN_CONFIDENCE = 0.6       # used when mode == "gate"
COUNTER_TREND_MODE_BY_ASSET: dict[str, str] = {}

# ── Judge CONTEXT noise toggles (task 2) ─────────────────────────────────
# DEFAULT = current behavior (keep all fields, legacy MACD) so the judge's
# CONTEXT — and therefore live trade-taking — is UNCHANGED out of the box.
#
# These fields are informational CONTEXT only; they are NOT trade gates (no
# downstream code reads them, nothing flips to "flat" because they are absent),
# so toggling them cannot stop the app from taking trades.
# LEAN since 2026-06-11 (docs/engineering/2026-06-11_performance-limiters.md §5): single-5m-candle
# pivots, 100-minute fibonacci and single-asset "breadth" were measured to be
# noise/actively misleading, so they are OFF. Real EMA-MACD stays ON.
CONTEXT_INCLUDE_MARKET_BREADTH = False
CONTEXT_INCLUDE_PIVOT_POINTS = False
CONTEXT_INCLUDE_FIBONACCI = False
CONTEXT_USE_REAL_MACD = True

# ── Regime hint via 1h efficiency ratio (2026-06-11) ─────────────────────
# ER = |net move| / sum(|bar moves|) over REGIME_ER_BARS 1h closes.
# Injected into HTF_CONTEXT as er_1h + regime_hint; prompts use it to switch
# entry style. A closed 15m break can qualify for a conditional retest in chop.
# Keep the 24-bar scale used by the historical session percentiles.
REGIME_ER_BARS = 24
REGIME_ER_CHOP_MAX = 0.18      # ER below ⇒ "chop"
REGIME_ER_TREND_MIN = 0.40     # ER at/above ⇒ "trending" (between ⇒ "mixed")

# Per-asset ER thresholds. GBP intraday ER is structurally lower (mean-reverting
# tape); raising the chop ceiling means the "chop" branch (fade range edges)
# fires more often — which IS the correct GBP playbook.
REGIME_ER_CHOP_MAX_BY_ASSET: dict[str, float] = {}
REGIME_ER_TREND_MIN_BY_ASSET: dict[str, float] = {}

# BTC volatility gate: only trade BTC when the 1h ATR% exceeds this floor.
# Dead BTC hours produce ER in the 0.25–0.45 "mixed" no-man's-land — entries
# there netted −$140 in the Jun-15-19 dataset; active hours (ATR%≥0.5%) netted +$130.
BTC_ATR_PCT_GATE = 0.25
BTC_ATR_GATE_ENABLED = False

# ── TP2 runner (2026-06-11) ───────────────────────────────────────────────
# Old behavior: the native bracket closed 100% at TP2 — but the 58 trades that
# reached TP2 continued a median +3.3R beyond it (report §10 pool 2).
# New behavior (TP2_RUNNER_ENABLED):
#   - the native order carries SL only (TP is managed by the monitor);
#   - at TP2 touch: close TP2_PARTIAL_FRACTION of the remaining position,
#     keep the rest as a runner trailed by RUNNER_TRAIL_ATR_MULT × ATR(1h)
#     (chandelier from the post-TP2 extreme, ratcheted onto the native SL);
#   - monitoring expiry extends to RUNNER_MAX_HOURS once the runner is
#     risk-free (SL locked at/beyond entry); pre-close windows still apply.
# If the position cannot be split (broker min-lot), falls back to the old
# full close at TP2.
TP2_RUNNER_ENABLED = True
# 0.5 → 0.34 (2026-08-06). SP-03's measurement is the reason: on 07-22 a +1.14R
# TP2 banked 23% of a 4.93R thesis with +3.80R still running after the exit, and
# the weekly SP-03 standings show the system correctly identifying ≥2R theses
# 4–7× a week while realising a fraction of each. Direction quality (WP-04,
# 62% pooled) is not the limiter; capture is. Booking a third at TP2 instead of
# a half leaves two thirds on the ratcheted runner, which is where every
# multi-R day in the ledger was actually made.
TP2_PARTIAL_FRACTION = 0.5
RUNNER_TRAIL_ATR_MULT = 1.0    # chandelier distance in ATR(1h) units
RUNNER_TRAIL_FALLBACK_RISK_MULT = 1.5  # trail = this × |entry−SL| if no ATR
RUNNER_MAX_HOURS = 24          # hard cap on runner monitoring

# ── Recent-trades prompt context (2026-06-11) ─────────────────────────────
# Inject the last few CLOSED trades for the asset into expert+judge prompts so
# the model can see what it just tried (anti-churn, soft — context not a gate).
# One indexed Mongo query per cycle (~ms); failure-safe (inert when DB absent).
RECENT_TRADES_IN_PROMPT = True
RECENT_TRADES_COUNT = 6
RECENT_TRADES_LOOKBACK_HOURS = 24
# Hard request-time freshness cap: drop any closed trade whose close is more
# than this many 5-min bars before "now" at query time. This is what keeps
# pre-outage trades out of the prompt after a restart — RECENT_TRADES_LOOKBACK_HOURS
# is only a coarse indexed DB filter; this is the precise, request-aligned gate.
# 24 bars = 2h. Set to 0/None to disable the cap.
# NOTE: only governs the cold-start Mongo *backfill* now — live context is served
# from the in-memory ring buffer (recent_trades_memory) with no freshness cap.
RECENT_TRADES_MAX_BARS = 24
# In-memory recent-trades ring buffer: keep the last N CLOSED trades per asset,
# process-global, dropped oldest-first. This is the live source for the prompt's
# RECENT_TRADES context (always populated, no DB round-trip, no freshness cap),
# so every AI call sees its own recent outcomes. ~6 assets x 3 = ~18 resident.
RECENT_TRADES_MEMORY_COUNT = 3

# ── Re-entry quality gate ──────────────────────────────────────────────────
# Adaptive confidence floor that holds at full REENTRY_MIN_CONFIDENCE for the
# first REENTRY_COOLDOWN_BARS after the last close (the plateau / cooldown
# window), then decays linearly to 0.0 at REENTRY_EXEMPT_BARS.
# This backstops the LLM: even if the judge approves a re-entry, the signal
# is suppressed if it is below the bar for how recently the last trade closed.
# Set REENTRY_COOLDOWN_BARS = 0 to disable.
REENTRY_COOLDOWN_BARS = 6        # bars (5 min each) = 30 min full-strength cooldown plateau
REENTRY_EXEMPT_BARS = 12         # after this many bars (60 min) the floor reaches 0.0
REENTRY_MIN_CONFIDENCE = 0.78    # confidence required during the cooldown plateau

# Same-direction re-entry settings after a stopped position.
SWEEP_REENTRY_CARVEOUT_ENABLED = True
# Floor still applied to a same-side re-entry inside the cooldown. Lower than
# REENTRY_MIN_CONFIDENCE but not zero: the judge must still be more confident
# than a coin, it just no longer needs near-certainty to act on its own rule.
SWEEP_REENTRY_MIN_CONFIDENCE = 0.60

# ── Prompt float hygiene (2026-06-12) ─────────────────────────────────────
# Round every numeric value serialized into LLM prompts to this many decimals.
# Float-repr artifacts (1.3412899999999999 instead of 1.34129) were burning
# ~1KB/prompt on GBPUSD and appeared in every judge CONTEXT block. 6 dp keeps
# full precision for all traded symbols (max broker digits is 5) and for
# derived stats (ATR, ER, slopes). Applied at the prompt-serialization
# boundary only — trading math is untouched.
PROMPT_FLOAT_DECIMALS = 6


def resolve_for_asset(mapping: dict, asset: str | None, default=None):
    """Resolve a per-asset override value with case/suffix tolerance.

    Mirrors the matching used by the code-sniper distance floors: matching is
    case-insensitive and tolerant of the broker's demo ``m`` suffix
    (e.g. ``"BTCUSDm"`` → ``"BTCUSD"``). Falls back to a ``"default"`` key in
    the mapping if present, otherwise to the supplied ``default``.
    """
    key = (asset or "").upper()
    if mapping and key not in mapping and key.endswith("M"):
        key = key[:-1]  # tolerate demo-server suffix
    if mapping and key in mapping:
        return mapping[key]
    if mapping and "default" in mapping:
        return mapping["default"]
    return default


# ── Prompt Builders ──────────────────────────────────────────────────────
ANTI_SCALP_K = 1.5
# RR floor quoted in prompts — measured entry→TP1 with the FINAL (floored)
# stop. 1.2 (was 1.5) so that volatility-correct stops don't choke trade flow;
# quality is carried by the stop floor + regime style rules, not a high RR bar.
PROMPT_MIN_RR = 1.2
# Derived from TTL_MINUTES_ALLOWED so the prompt text and the schema can never
# disagree about what the judge is allowed to emit (a mismatch here is an
# instant LP-09-class parse failure).
TTL_ENUM = "{" + ", ".join(str(t) for t in TTL_MINUTES_ALLOWED) + "}"


# ── Trade Monitor (trailing stops) ───────────────────────────────────────
DEFAULT_TRAIL_STEPS: list[tuple[float, int, float]] = [
    (0.5, 3, 0.5),
    (1.0, 1, 0.5),
    (1.5, 1, 1.0),
    (2.0, 1, 1.5),
    (2.5, 1, 2.0),
    (3.0, 1, 2.5),
    (4.0, 1, 3.5),
    (5.0, 1, 4.5),
]
CONTINUOUS_TRAIL_STEP_R = 0.5
CONTINUOUS_TRAIL_CONFIRM = 1

# ── Trail arming basis (2026-08-06) ───────────────────────────────────────
# Trail steps have always triggered on the candle CLOSE. The failure mode is
# recorded twice in the library: 2026-07-16 EURUSD traded to +1.00R intra-bar
# EXACTLY, closed back under it, the +1.0R→+0.5R step never armed, the trade
# took a full −1R, and the thesis then ran +1.20R. Close-basis arming asks the
# market to both reach the level and hold it for five minutes; the position was
# genuinely worth +1R at the moment of the touch.
#
# With this on, a step arms when the bar's favourable EXTREME reaches the
# trigger. The safety condition is that the step only ever moves the SL to
# trail_to_r, which is STRICTLY less than trigger_r — so a touch-armed step can
# never place a stop at or beyond the price that armed it. Steps where
# trail_to_r == trigger_r (the opening 0.5R→0.5R step) are excluded and keep
# close-basis arming, because arming those on a touch would park the stop
# exactly at a level the bar has already retraced from.
TRAIL_ARM_ON_TOUCH = True

# Number of candles after entry where SL evaluation is skipped
# (prevents same-bar knockout, matches backtest behaviour)
SETTLEMENT_CANDLES = 2

# Maximum monitoring duration for live trades (hours).
# 4 → 6 (2026-08-06): this is the ceiling the TTL sits under, so leaving it at 4
# would have silently capped the new 240-minute TTL at 4h anyway and made the
# enum change cosmetic. 6h covers the 240-minute TTL plus the post-TP1 trail
# life that the WP-04 window implies. Pre-close and maintenance force-closes are
# evaluated first and still bound this absolutely — nothing rides a gap.
TRADE_MONITOR_EXPIRY_HOURS = 6

# ── Green-trade TTL extension (2026-08-06) ────────────────────────────────
# The 4h monitor expiry closes at market whatever it finds, winner or loser.
# On 2026-07-17 that timed out an XAUUSD position at −0.17R whose thesis then
# ran +3.04R — logged in the SP-03 killer standings as "TTL expiry 1", and the
# weekly review raised the exact question this answers: *should TTL extend when
# the position is green and with-trend?*
#
# A clock is not a thesis. When the expiry arrives on a position that is both
# (a) at least TTL_EXTEND_MIN_R in profit and (b) risk-free — its stop already
# ratcheted to entry or better, so the worst case is a scratch — the monitor
# grants another TTL_EXTEND_HOURS instead of closing, up to TTL_EXTEND_MAX_HOURS
# total. Losers, break-even trades and anything whose stop has not moved are
# closed on time exactly as before, so this can only ever extend the life of a
# position that cannot lose money.
#
# Pre-close/maintenance windows still force-close over gaps: extension defers to
# them, it never overrides them.
TTL_EXTEND_WHEN_GREEN = True
TTL_EXTEND_MIN_R = 0.5         # unrealised R required to earn an extension
TTL_EXTEND_HOURS = 2           # granted per extension
TTL_EXTEND_MAX_HOURS = 24      # hard cap on total monitoring, matches RUNNER_MAX_HOURS


# ── Live recovery & account safety (production hardening) ─────────────────
# Magic number stamped on every order this system places. Recovery and
# reconciliation filter broker positions by this so manual trades and other
# bots sharing the account are never adopted or touched.
MT5_MAGIC_NUMBER = 234000

# ── Ghost-position guardian (manual-trade kill switch) ────────────────────
# Force-close any open broker position this system did NOT place. Ownership is
# decided by MT5_MAGIC_NUMBER above: MetaTrader stamps a position's `magic`
# atomically at fill, so a position whose magic != MT5_MAGIC_NUMBER was opened by
# a human (manual trade = magic 0) or another bot — a *ghost* — and is closed at
# market on the next sweep. This is the same ownership signal the live
# TradeMonitor already trusts, so the guardian can never disagree with the
# manager about what is "ours". Protects a live account from out-of-band manual
# risk (the classic account-blow-up). Only acts on an ARMED LIVE account
# (live_trading_allowed()); fully inert when unarmed.
# Implementation: packages/core/risk/ghost_guard.py.
MT5_GHOST_KILL_ENABLED = os.getenv("MT5_GHOST_KILL_ENABLED", "true").strip().lower() in (
    "1", "true", "yes", "on",
)
# Dry-run: detect + log ghosts every sweep but do NOT close them. Default OFF
# (armed). Set MT5_GHOST_KILL_DRY_RUN=1 to verify detection before arming.
MT5_GHOST_KILL_DRY_RUN = os.getenv("MT5_GHOST_KILL_DRY_RUN", "false").strip().lower() in (
    "1", "true", "yes", "on",
)
# Poll cadence. The MT5 Python API is request/response (no push stream), so a
# ghost is detected within one interval. Default 0.05s (≈20 checks/sec) =
# effectively-instant detection. A LITERAL 0 would busy-loop a thread and flood
# the terminal with IPC, contending with — and slowing — the real trading calls;
# the floor in ghost_guard.py prevents that. Push lower (down to the floor) only
# if you accept that CPU/IPC cost.
MT5_GHOST_KILL_INTERVAL_SECONDS = float(os.getenv("MT5_GHOST_KILL_INTERVAL_SECONDS", "0.05"))
# Grace delay before the FIRST sweep so startup adoption/reconciliation settles
# (adoption only ever re-adopts OUR magic, so this is belt-and-braces).
MT5_GHOST_KILL_STARTUP_GRACE_SECONDS = float(
    os.getenv("MT5_GHOST_KILL_STARTUP_GRACE_SECONDS", "5.0")
)
# Optional symbol scope: comma-separated symbols to police. Empty = ALL symbols
# (kill any foreign position anywhere — "anything not taken from this app").
MT5_GHOST_KILL_SYMBOLS = [
    s.strip().upper() for s in os.getenv("MT5_GHOST_KILL_SYMBOLS", "").split(",") if s.strip()
]
# Extra magic numbers to treat as "ours" and never kill (legacy deployments /
# sister bots). MT5_MAGIC_NUMBER is always included automatically.
MT5_GHOST_KILL_ALLOWED_MAGICS = [
    int(m.strip())
    for m in os.getenv("MT5_GHOST_KILL_ALLOWED_MAGICS", "").split(",")
    if m.strip().lstrip("-").isdigit()
]

# On process start the TradeMonitor re-adopts any still-open broker positions
# for its asset. Broker positions survive a crash/restart; the in-memory
# monitoring loop does not — without this, a recovered position keeps only its
# native SL/TP and loses TP1/TP2/BE/trail management.
MONITOR_RECOVERY_ENABLED = True
# LP-13 fix (2026-07-27): recovery now also fires eagerly at the first cycle
# that sees an open position with no monitor (runner), so it can run right at
# process boot while the MT5 terminal is still warming up — give it a real
# window (30 × 10s = 5 min) instead of 50s, and the recovery task un-marks the
# asset on give-up so a later monitor init retries.
MONITOR_RECOVERY_MAX_ATTEMPTS = 30     # retries while the broker terminal warms up
MONITOR_RECOVERY_RETRY_SECONDS = 10    # delay between readiness attempts

# Account-wide daily drawdown kill switch. Purely additive: it blocks NEW
# entries once equity falls this far below the day's starting equity. It never
# modifies or closes existing positions. Baseline is persisted and reset at
# UTC midnight so a crash-loop cannot wipe the day's loss memory.
ACCOUNT_DAILY_DD_GUARD_ENABLED = True
ACCOUNT_MAX_DAILY_DD_PCT = 10.0        # halt new entries at 10% account drawdown (go-live 2026-06-21; was 12%)
# Sanity backstop: the day-start baseline is keyed by account fingerprint so a
# paper ($10M) and a live (~$700) account can never share a baseline. As a
# second line of defence, if live equity is implausibly far below the baseline
# (a bigger drop than any real intraday DD could be — i.e. a stale/foreign
# baseline like a paper $10M seed vs a $700 live equity), the guard RE-SEEDS
# from the real equity and fails open instead of halting on a phantom drawdown.
ACCOUNT_DD_BASELINE_SANITY_DROP_PCT = 50.0  # >this implied drop ⇒ baseline is bogus, re-seed


# ── Live-path trade throttle (2026-06-20) ──────────────────────────────────
# Restart-safe per-asset daily trade-count cap + consecutive-SL cooldown for
# the LIVE code-sniper path. The legacy in-memory DailyGuards lives on the
# AgentRunner and is never consulted by code_sniper_monitor (which places
# orders directly), so without this the live path had no over-trading guard
# beyond the account DD kill switch. Derived READ-ONLY from the durable
# comprehensive_trades record (no hot-path writes; inherently restart-safe)
# and fail-open like account_guard — a Mongo hiccup never wedges trading.
# Reuses the BACKTEST_* count/streak thresholds for parity.
TRADE_THROTTLE_ENABLED = True
# After BACKTEST_COOLDOWN_SL_THRESHOLD consecutive SL/loss closes, block new
# entries for this many minutes (time-based so it self-clears without needing a
# trade to break the streak — avoids a dead-lock).
TRADE_COOLDOWN_MINUTES = 30


# ── Real-account arming flag (2026-06-19) ──────────────────────────────────
# Opt-in switch consulted by live_trading_allowed(). Armed via this flag OR the
# `allow_live` field in the local cred cache (config/mt5_config.local.json), and
# defaults OFF. Since the always-live MT5 refactor it gates the ghost-position
# guardian only (auto-closing foreign positions); order execution itself —
# agent and manual dashboard orders — is always live on MT5. See
# packages.core.services.mt5_credentials.live_trading_allowed().
MT5_ALLOW_LIVE = os.getenv("MT5_ALLOW_LIVE", "false").strip().lower() in ("1", "true", "yes", "on")

# Dashboard MT5 keepalive cadence. The router holds one process-wide MT5 login
# (the MetaTrader5 module is process-global) shared by the wallet panel and the
# trading loop. A background task re-checks the link every N seconds and
# reconnects via MT5Adapter.ensure_connection (MT5_RETRY_BASE_DELAY backoff),
# re-minting the session token so a healthy link never shows "Disconnected".
MT5_KEEPALIVE_INTERVAL_SECONDS = 30

# Optional per-ORDER sanity ceilings for the real account — a single-trade
# blow-up guard complementing the account-wide daily-DD kill switch
# (account_guard). Inert at 0; wiring is left to the risk service.
MT5_MAX_LOT = 0.0
MT5_MAX_RISK_USD = 0.0


# ── Agent Runner ─────────────────────────────────────────────────────────
WRITE_TIMEOUT_SECONDS = 0.5
PREDICTION_TIMEOUT_MINUTES = 60
RUNNER_CLEANUP_INTERVAL = 7200
RUNNER_TRIGGER_CHECK_INTERVAL = 300
RUNNER_CACHE_ROLL_INTERVAL = 300
RUNNER_MONITORING_DURATION_MINUTES = 60


# ── Supervisor ───────────────────────────────────────────────────────────
SUPERVISOR_MAX_RESTART_ATTEMPTS = 5
SUPERVISOR_RESTART_COOLDOWN_MINUTES = 5
SUPERVISOR_HEALTH_CHECK_INTERVAL_MINUTES = 2
SUPERVISOR_MAX_CONSECUTIVE_FAILURES = 3


# ── Candle Cache & Service ───────────────────────────────────────────────
CANDLE_CACHE_MAX_CANDLES = 50
CANDLE_SERVICE_DEFAULT_5M = 20
CANDLE_SERVICE_DEFAULT_15M = 30
CANDLE_SERVICE_CACHE_SIZE_5M = 150
CANDLE_SERVICE_CACHE_SIZE_15M = 80


# ── Realtime Candle Monitor ──────────────────────────────────────────────
REALTIME_POLL_INTERVAL = 30
REALTIME_CANDLE_TIMEOUT = 300


# ── Code Sniper Monitor ─────────────────────────────────────────────────
CSM_CANDLE_BUFFER_SEC = 5
CSM_ERROR_RETRY_SEC = 30
CSM_PAUSE_CHECK_SEC = 60
# 41 = 40 historical + 1 current — keep in lockstep with
# MIN_CANDLES_FOR_CODE_EXECUTION / CS_EXPECTED_TOTAL_CANDLES (judge template).
CSM_CANDLES_REQUIRED = 41
CSM_HISTORICAL_COUNT = 40
CSM_TRADE_EXPIRY_HOURS = 4
CSM_MAX_CONCURRENT_STRATEGIES = 5
CSM_STRATEGY_TIMEOUT_SEC = 10
CSM_CIRCUIT_BREAKER_THRESHOLD = 3
CSM_CANDLE_STALENESS_THRESHOLD_MIN = 10
CSM_HEALTH_CHECK_INTERVAL_SEC = 300


# ── Sandbox ──────────────────────────────────────────────────────────────
SANDBOX_TIMEOUT = 5
SANDBOX_MEMORY = 256 * 1024 * 1024
SANDBOX_MAX_WORKER_RESTARTS = 5
SANDBOX_POOL_SIZE = 3


# ── GPT Client HTTP ─────────────────────────────────────────────────────
GPT_HTTP_CONNECT_TIMEOUT = 10.0
GPT_HTTP_WRITE_TIMEOUT = 10.0
GPT_HTTP_POOL_TIMEOUT = 5.0
GPT_HTTP_MAX_CONNECTIONS = 20
GPT_HTTP_MAX_KEEPALIVE = 10
GPT_HTTP_KEEPALIVE_EXPIRY = 60.0


# ── Trading Filters (per-asset) ─────────────────────────────────────────
TRADING_FILTER_MIN_RR = 2.0
TRADING_FILTER_MIN_TARGET_POINTS: dict = guard_dict(
    "filter_min_target_points", DEFAULT_FILTER_MIN_TARGET_POINTS
)
TRADING_FILTER_SLIPPAGE: dict = guard_dict("filter_slippage", DEFAULT_FILTER_SLIPPAGE)


# ── Backtest Runner ─────────────────────────────────────────────────────
BT_INTERVAL_5M_SECONDS = 300
BT_INTERVAL_15M_SECONDS = 900
BT_HISTORICAL_WINDOW = 40
BT_MIN_FUTURE_CANDLES = 12
BT_ALLOWED_COUNTS: set[int] = {30, 50, 80, 100}

# ── Backtest ⇄ live parity (2026-08-07, corrected 2026-08-15) ────────────
# Everything below used to be a SECOND copy of a live number that had since
# moved. A backtest run on stale copies is not a test of the live system, so
# each of these is now DERIVED from the live constant rather than restated.
#
#   BT_TTL_DEFAULT_MINUTES   was 60  — not even a legal TTL after the
#                                      2026-08-06 enum move to {90,180,240};
#                                      a signal built with it fails schema
#                                      validation (see coerce_ttl).
#   BT_TRADE_TIMEOUT_MINUTES was derived from TRADE_MONITOR_EXPIRY_HOURS (6h)
#                                      on the mistaken belief that that IS the
#                                      live monitor expiry. It is not: it's
#                                      only the crash-recovery fallback used
#                                      when a persisted monitor doc has no
#                                      expiry of its own (services/trade_monitor.py
#                                      :435-439,489). The expiry a FRESH live
#                                      trade actually gets is
#                                      CSM_TRADE_EXPIRY_HOURS (4h), set at
#                                      services/code_sniper_monitor.py:1085
#                                      via MonitorConfig.trade_expiry_hours.
#                                      A 6h backtest window let trades ride
#                                      2h longer than live ever would,
#                                      inflating every timed-out trade's P&L.
#   BT_CANDLES_PER_4H_5M     — the "4h" in the name is CSM_TRADE_EXPIRY_HOURS,
#                                      so it tracks that, not the recovery
#                                      fallback.
BT_TTL_DEFAULT_MINUTES = TTL_DEFAULT_MINUTES
BT_TRADE_TIMEOUT_MINUTES = CSM_TRADE_EXPIRY_HOURS * 60
BT_MONITOR_CANDLES_5M = (CSM_TRADE_EXPIRY_HOURS * 60) // 5
# Legacy alias — same value, kept because external scripts import it.
BT_CANDLES_PER_4H_5M = BT_MONITOR_CANDLES_5M

# ── Backtest account model (2026-08-07) ──────────────────────────────────
# Before this the backtest had NO account: `simulate_trade` multiplied a price
# move by a fixed `lot_size` (default 1.0 standard lot) regardless of stop
# distance, balance or the broker's min_lot. Live has sized by
# `balance × CS_DEFAULT_RISK_PCT`, floored to min_lot, since go-live — which is
# also where LP-01 (the min-lot overshoot that dominates the live record) comes
# from. A backtest that cannot express LP-01 cannot reproduce the live P&L
# distribution at all, so the account is now modelled explicitly.
#
# BT_INITIAL_CAPITAL is the starting balance; equity compounds across the run
# exactly as the live balance does, so sizing shrinks after a drawdown and the
# risk-budget gate (RISK_BUDGET_SKIP_ENABLED) starts refusing instruments the
# account can no longer afford — the same account-size-adaptive behaviour the
# live gate has.
BT_INITIAL_CAPITAL = 1000.0
BT_RISK_PCT = CS_DEFAULT_RISK_PCT
# False falls back to the pre-2026-08-07 fixed-lot behaviour (BacktestRunner's
# `lot_size` argument), which is only meaningful for price-R studies.
BT_RISK_SIZING_ENABLED = True

# ── Backtest commission model (2026-08-15) ────────────────────────────────
# The backtest charged zero trading cost — `grep -i commission` over
# backtest_runner.py/backtest_account.py/trade_sim.py returned nothing — so
# every backtest P&L was frictionless while live pays real, broker-charged
# commission. That overstates the edge, worst for the thinner-margin setups a
# cost-aware system should be most skeptical of.
#
# Values are measured, not estimated: every app-magic (234000) MT5 deal from
# 2026-01-01 to now, commission summed per symbol and divided by total entry
# volume. Two things this data showed and the model below relies on:
#   1. Commission is charged ONCE, on the ENTRY (IN) deal only — every one of
#      355 IN deals carried nonzero commission, all 405 OUT deals (including
#      every TP1/TP2 partial) carried exactly zero. A partial close is not a
#      second commission event.
#   2. Swap was zero on every deal, in or out — these trades don't cross a
#      rollover, so swap is not modelled.
# Confirms the existing XAG figure elsewhere in this file (~$0.95/0.01 lot)
# and replaces the flat $0.378/trade blended average (docs/runbooks/backtest-sweep.md,
# scripts/audit/exit_config_sweep.py) with real per-asset numbers — XAG is
# ~77x USTEC's rate; a single blended average hid that entirely.
COMMISSION_USD_PER_0_01_LOT: dict[str, float] = {}
# Fallback for an asset with no measured rate: the blended per-0.01-lot
# average across all 355 measured entries (NOT the $0.378/trade figure quoted
# elsewhere in this file and in exit_config_sweep.py — that number blends
# trades of different sizes into a per-TRADE average, which is a different,
# incompatible unit from the per-lot table above).
COMMISSION_USD_PER_0_01_LOT_DEFAULT = 0.10


# ── Trade Simulation ────────────────────────────────────────────────────
# Tracks the live per-trade expiry (CSM_TRADE_EXPIRY_HOURS) — see the BT_*
# parity note above; TRADE_MONITOR_EXPIRY_HOURS is a recovery-path fallback,
# not the value a fresh live trade is actually given.
TRADE_SIM_MAX_DURATION_HOURS = CSM_TRADE_EXPIRY_HOURS


# ── MT5 Adapter ─────────────────────────────────────────────────────────
MT5_DEFAULT_MAX_RETRIES = 2
MT5_RETRY_BASE_DELAY = 1.5
# Give the terminal time to finalize the just-closed M5/M15 bar before reading.
MT5_CANDLE_FINALIZATION_BUFFER_SEC = 5
MT5_MIN_BAR_RATIO_THRESHOLD = 0.95
MT5_GAP_MIN_BAR_RATIO = 0.70
MT5_INTERVAL_SECONDS: dict[str, int] = {"M5": 300, "M15": 900, "M1": 60}

# ── Frozen-feed → market-closed detection ───────────────────────────────
# The broker stops emitting new bars during ANY market halt, not just the
# scheduled weekend/maintenance windows the calendar guard (_is_market_closed)
# knows about.  Holidays and early closes (e.g. the US July-4 half day) leave
# the feed frozen hours behind while the guard still reports "open", so every
# fetch comes back stale.  Treating that as a *retryable* fetch error burns the
# whole multi-layer retry budget — with reconnects — against a market that is
# simply shut (the source of the 168-error / 744-warning "failed after all
# attempts" storms in production logs).
#
# When the newest candle is more than this many intervals behind the requested
# end time, the feed is frozen (halted), not merely lagging: normal broker
# latency is ≤1-2 intervals, so 6 never trips in live trading but catches a real
# halt fast.  On detection we raise MarketClosedError so callers skip the cycle
# and back off instead of hammering a closed market.
MT5_STALE_MARKET_CLOSED_INTERVALS = 6
# How long to tell callers to back off when a frozen feed is detected.  The true
# reopen time of an unscheduled halt is unknown, so re-check every few minutes
# rather than sleeping until an unknown reopen (or hammering every cycle).
MT5_STALE_MARKET_CLOSED_BACKOFF_SEC = 300


# ── CORS (single source for api.py) ─────────────────────────────────────
CORS_ALLOWED_ORIGINS: list[str] = [
    "http://localhost:5173", "http://127.0.0.1:5173",
    "http://localhost:8000", "http://127.0.0.1:8000",
    "http://localhost:5174", "http://127.0.0.1:5174",
    "http://localhost:5175", "http://127.0.0.1:5175",
    "http://localhost:5176", "http://127.0.0.1:5176",
    "http://localhost:5177", "http://127.0.0.1:5177",
    "https://localhost:5173", "https://127.0.0.1:5173",
    "https://localhost:8000", "https://127.0.0.1:8000",
    "http://localhost:3000", "http://127.0.0.1:3000",
    "http://localhost:3001", "http://127.0.0.1:3001",
    "null",
    "http://localhost:8080", "http://127.0.0.1:8080",
]
_env_origins = os.environ.get("ALLOWED_ORIGINS", "")
if _env_origins:
    CORS_ALLOWED_ORIGINS.extend(o.strip() for o in _env_origins.split(",") if o.strip())
CORS_ALLOWED_METHODS: list[str] = ["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH", "HEAD"]
CORS_ALLOWED_HEADERS: list[str] = [
    "Accept", "Accept-Language", "Content-Language", "Content-Type",
    "Authorization", "X-Requested-With", "X-CSRFToken", "X-Request-ID",
    "Cache-Control", "Pragma", "Origin", "Referer", "User-Agent",
    "Access-Control-Request-Method", "Access-Control-Request-Headers",
]
CORS_EXPOSE_HEADERS: list[str] = [
    "Content-Type", "Cache-Control", "X-Request-ID",
    "Access-Control-Allow-Origin", "Access-Control-Allow-Credentials",
    "Access-Control-Allow-Methods", "Access-Control-Allow-Headers",
]
CORS_MAX_AGE = 3600
