"""Multi-expert orchestration for trading proposals."""

from __future__ import annotations

import asyncio
import inspect
import json
import time
import logging
from datetime import datetime, timezone
from typing import Any, Iterable, List, Dict, Optional, Union, Tuple
from pydantic import ValidationError
from dataclasses import dataclass

from .config import (
    MODEL_4O_MINI, MODEL_4, VALIDATE_EXPERT_RESPONSES,
    EXPERT_TIMEOUT_SECONDS, EXPERT_KINDS, JUDGE_MAX_PROPOSALS,
    JUDGE_SAFETY_BUFFER_SECONDS, JUDGE_CANDLE_RANGE,
    SCORE_RR_WEIGHT, SCORE_CONFIDENCE_WEIGHT, SCORE_TTL_PENALTIES,
    TTL_DEFAULT_MINUTES, coerce_ttl,
    WINDOW_EXTENSION_FACTOR, DEFAULT_MAX_TOTAL_TIME_SECONDS,
    DEFAULT_JUDGE_TIMEOUT_SECONDS, DEFAULT_EXPERT_TIMEOUT_SECONDS,
    MIN_CANDLES_FOR_JUDGE, MAX_CANDLES_FOR_JUDGE,
    DEFAULT_CANDLES_FOR_JUDGE, MIN_CANDLES_FOR_CODE_EXECUTION,
    HISTORICAL_CANDLES_COUNT,
)
from .anthropic_sonnet_55 import completion_options
from .ai_accounting import decision_accounting, trade_reason
from .gpt_client import complete
from .prompt_builders import (
    judge_system_prompt,
    code_sniper_judge_system_prompt,
    unified_expert_system_prompt,
    build_expert_user_prompt,
    build_judge_user_prompt,
)
from .models import RecentTradeView
from .schemas_trade import MultiExpertSignal, CodeSniperSignal, DynamicConfig, TRADE_JSON_SCHEMA, CODE_SNIPER_JSON_SCHEMA, UNIFIED_EXPERT_JSON_SCHEMA
from .market_structure import build_advanced_context, compute_htf_context, atr_from_candles, recommended_stop_distance
from .code_executor import execute_generated_code
from .validators import median_range_5m
from packages.db.models import ExpertInsight, JudgeOutcome, MultiExpertCycleDoc

logger = logging.getLogger(__name__)

# All constants imported from packages.core.config (single source of truth)
_EXPERT_TIMEOUT_SECONDS = EXPERT_TIMEOUT_SECONDS

# ============================================================================
# HELPER CLASSES
# ============================================================================

@dataclass
class ExpertProposal:
    """Type-safe proposal tracking (replaces id() dict)."""
    signal: MultiExpertSignal
    expert_kind: str  # always 'trend' — the single unified intraday expert
    market_context: Dict[str, Any]
    timing_ms: float

class CandleConverter:
    """Lazy candle conversion with caching to avoid repeated transformations."""
    
    def __init__(self):
        self._cache: Dict[str, List[List[Any]]] = {}
    
    def to_compact_arrays(self, candles: List[Dict[str, Any]], key: str) -> List[List[Any]]:
        """Convert candles to compact arrays with caching."""
        if key not in self._cache:
            self._cache[key] = _to_compact_arrays(candles)
        return self._cache[key]
    
    def clear_cache(self) -> None:
        """Clear the conversion cache."""
        self._cache.clear()

class ProposalScorer:
    """Score proposals for judge input prioritization."""
    
    def __init__(self, c5_superset: List[Dict[str, Any]]):
        self.c5_superset = c5_superset
    
    def score(self, proposal: ExpertProposal) -> float:
        """Score a proposal based on confidence, TTL, and market range."""
        return _score_proposal(proposal.signal, self.c5_superset)

# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

_COMPACT_KEYS = ("time", "open", "high", "low", "close", "volume")


def _ensure_candle_dicts(candles: list) -> List[Dict[str, Any]]:
    """Normalise candles to dicts.  Pass-through when already dicts.

    The live runner may pass compact arrays ``[[time,o,h,l,c,vol], ...]``
    while all internal code (``build_advanced_context``, guard helpers,
    ``market_structure``) uses ``candle.get('close')`` / ``candle['high']``.
    Converting once at the pipeline entry avoids scattered format checks.
    """
    if not candles:
        return candles
    sample = candles[0]
    if isinstance(sample, dict):
        return candles
    out: List[Dict[str, Any]] = []
    for c in candles:
        if isinstance(c, dict):
            out.append(c)
        elif isinstance(c, (list, tuple)) and len(c) >= 5:
            out.append({
                "time": c[0],
                "open": float(c[1]) if c[1] is not None else 0.0,
                "high": float(c[2]) if c[2] is not None else 0.0,
                "low": float(c[3]) if c[3] is not None else 0.0,
                "close": float(c[4]) if c[4] is not None else 0.0,
                "volume": float(c[5]) if len(c) > 5 and c[5] is not None else 0.0,
            })
    return out


def _fmt_ts(ts: Any) -> str:
    """Format timestamp as YYYY-MM-DDTHH:MM (no seconds, no timezone)."""
    if ts is None:
        return ""
    try:
        if isinstance(ts, datetime):
            return ts.strftime("%Y-%m-%dT%H:%M")
        
        # Parse string timestamps
        s = str(ts).replace(" ", "T").replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
            return dt.strftime("%Y-%m-%dT%H:%M")
        except Exception:
            return s[:16]
    except Exception:
        return str(ts)[:16]


def _to_compact_arrays(candles: List[Dict[str, Any]]) -> List[List[Any]]:
    """Convert candle dicts to compact arrays for efficient prompt payloads.
    
    Format: [timestamp, open, high, low, close, volume]
    Returns pre-formatted arrays without modification.
    """
    compact = []
    for c in candles or []:
        # Already compact - return as is
        if isinstance(c, list) and len(c) >= 5:
            compact.append(c)
            continue
        
        if not isinstance(c, dict):
            continue
        
        # Extract and convert values
        raw_ts = c.get('time') or c.get('index')
        ts = _fmt_ts(raw_ts)
        o = float(c.get('open', 0.0)) if c.get('open') is not None else 0.0
        h = float(c.get('high', 0.0)) if c.get('high') is not None else 0.0
        l = float(c.get('low', 0.0)) if c.get('low') is not None else 0.0
        cl = float(c.get('close', 0.0)) if c.get('close') is not None else 0.0
        v = float(c.get('volume', 0.0)) if c.get('volume') is not None else 0.0
        compact.append([ts, o, h, l, cl, v])
    
    return compact


def _convert_dict_to_signal(proposal: Dict[str, Any]) -> MultiExpertSignal:
    """Convert proposal dict to MultiExpertSignal object with validation."""
    try:
        return MultiExpertSignal(**proposal)
    except ValidationError as e:
        raise ValueError(f"Proposal validation failed: {e}. Proposal: {proposal}")


def _safe_float(value: Any, field_name: str) -> Optional[float]:
    """Safely convert value to float with error handling."""
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        raise ValueError(f"Field '{field_name}' must be numeric, got: {type(value).__name__} = {value}")


def _log_prompt(
    role: str,
    system_prompt: str,
    user_prompt: str,
    gpt_cfg: Dict[str, Any],
    **meta: Any,
) -> None:
    """Log the EXACT system + user prompt right before it is sent to the model.

    The full prompt text is placed in the log *message* (not only ``extra``)
    on purpose: the file-log formatter renders ``%(message)s`` only, so anything
    in ``extra`` is invisible in ``results/logs/*.log``. Structured metadata is
    still attached via ``extra`` for the backend's structured log sink/dashboard.

    Disable with ``gpt_cfg["log_prompts"] = False`` (defaults to enabled).
    """
    if not gpt_cfg.get("log_prompts", True):
        return
    header = " ".join(f"{k}={v}" for k, v in meta.items())
    logger.info(
        f"{role} PROMPT >>> asset={gpt_cfg.get('asset')} "
        f"agent_id={gpt_cfg.get('agent_id')} {header} "
        f"sys_chars={len(system_prompt)} user_chars={len(user_prompt)}\n"
        f"----- SYSTEM PROMPT -----\n{system_prompt}\n"
        f"----- USER PROMPT -----\n{user_prompt}\n"
        f"----- END {role} PROMPT -----",
        extra={
            "log_event": f"{role.lower()}_prompt",
            "asset": gpt_cfg.get("asset"),
            "agent_id": gpt_cfg.get("agent_id"),
            "system_prompt_chars": len(system_prompt),
            "user_prompt_chars": len(user_prompt),
            **meta,
        },
    )


# ============================================================================
# EXPERT CONFIGURATION
# ============================================================================

# The system runs a single unified intraday expert. `EXPERT_KINDS` (config) is
# `["trend"]`; the "trend" key here maps to that one expert. No breakout/reversal
# experts exist — the unified expert already picks the entry STYLE (retest /
# breakout / reversal) itself from the regime.
_EXPERT_SYSTEM_PROMPTS = {
    "trend": unified_expert_system_prompt,
}

# ============================================================================
# VALIDATION FUNCTIONS
# ============================================================================



def _create_flat_signal(rationale: str = "No trade signal generated") -> MultiExpertSignal:
    """Create a standard flat signal with default values."""
    return MultiExpertSignal(
        signal="flat",
        entry=None,
        stop=None,
        target=None,
        ttl_minutes=TTL_DEFAULT_MINUTES,
        confidence=0.0,
        rationale=rationale
    )


def _build_system_prompt(fn, asset):
    """Call a system-prompt builder, passing ``asset`` only when it accepts it.

    Inspecting the signature (instead of catching ``TypeError``) means a genuine
    ``TypeError`` raised *inside* the builder propagates normally rather than
    being masked into a degraded no-arg prompt.
    """
    try:
        accepts_asset = "asset" in inspect.signature(fn).parameters
    except (ValueError, TypeError):
        accepts_asset = False  # signature unavailable (e.g. C builtin)
    return fn(asset=asset) if accepts_asset else fn()


def _most_recent_closed_trade(recent: list[RecentTradeView] | None) -> Optional[RecentTradeView]:
    """Pick the most recently *closed* trade (smallest ``bars_elapsed``).

    ``recent_trades`` is now sorted by close time, but this re-derives the
    latest close defensively (and tolerates ``bars_elapsed=None`` rows from
    older callers). Returns ``None`` when no entry carries a numeric
    ``bars_elapsed``.
    """
    if not recent:
        return None
    with_bars = [t for t in recent if isinstance(t.get("bars_elapsed"), (int, float))]
    if not with_bars:
        return None
    return min(with_bars, key=lambda t: t["bars_elapsed"])


def _reentry_adaptive_floor(
    bars: Optional[float], cooldown_bars: int, exempt_bars: int, min_conf: float
) -> float:
    """Confidence floor required for a re-entry ``bars`` candles after last close.

    Holds at full ``min_conf`` through the cooldown plateau (``bars`` ≤
    ``cooldown_bars``), then decays linearly to 0.0 at ``exempt_bars``; 0.0 at or
    beyond ``exempt_bars`` (no gating). Returns 0.0 for ``bars is None``.
    """
    if bars is None or bars >= exempt_bars:
        return 0.0
    if bars <= cooldown_bars:
        return min_conf
    if exempt_bars > cooldown_bars:
        span = exempt_bars - cooldown_bars
        return min_conf * max(0.0, 1.0 - (bars - cooldown_bars) / span)
    return 0.0


# Symbols covered by the crypto dead-tape gate. Measured on BTC (report §8:
# ATR% < 0.5 entries netted −$140); applied class-wide so ETH gets the same
# protection instead of none (multi-asset analysis 2026-07-03 §4.6).
_CRYPTO_VOL_GATE_SYMBOLS = ("BTCUSD", "BTCUSDT", "ETHUSD", "ETHUSDT")


def _crypto_vol_gate_blocked(
    asset: str | None, htf_context: Dict[str, Any] | None, enabled: bool, gate_pct: float
) -> Tuple[bool, Optional[float]]:
    """Decide whether the crypto dead-tape volatility gate blocks this cycle.

    Returns ``(blocked, atr_pct_1h)``. Inert (``False``) for every non-crypto
    asset and when ``enabled`` is False. Fail-open: blocks only when a numeric
    1h ATR% is present AND strictly below ``gate_pct``; a missing/non-numeric
    ATR% never blocks. The broker demo ``M`` suffix is tolerated.
    """
    if not enabled:
        return False, None
    a = (asset or "").strip().upper()
    if a.endswith("M"):
        a = a[:-1]
    if a not in _CRYPTO_VOL_GATE_SYMBOLS:
        return False, None
    atr_pct = (htf_context or {}).get("atr_pct_1h")
    if isinstance(atr_pct, (int, float)) and atr_pct < gate_pct:
        return True, atr_pct
    return False, atr_pct if isinstance(atr_pct, (int, float)) else None


def _extract_recommended_candle_count(
    expert_kinds: Dict[int, str],
    proposals: Iterable[MultiExpertSignal],
    expert_market_contexts: Dict[str, Dict[str, Any]],
) -> int:
    """Extract recommended candle count from expert's candel_required_for_judge field.
    
    Returns: Clamped value between 10 and 30, defaults to 20.
    """
    if not expert_kinds or not proposals or not expert_market_contexts:
        return DEFAULT_CANDLES_FOR_JUDGE
    
    # Get the first proposal's expert kind
    first_proposal = list(proposals)[0]
    proposal_kind = expert_kinds.get(id(first_proposal), 'unknown')
    
    # Check if we have detailed_market_context for this expert kind
    if proposal_kind not in expert_market_contexts:
        return DEFAULT_CANDLES_FOR_JUDGE
    
    market_ctx = expert_market_contexts[proposal_kind]
    if not isinstance(market_ctx, dict):
        return DEFAULT_CANDLES_FOR_JUDGE
    
    # Use new field name: candel_required_for_judge
    if 'candel_required_for_judge' in market_ctx:
        recommended_count = market_ctx.get('candel_required_for_judge', DEFAULT_CANDLES_FOR_JUDGE)
        logger.info(f"Using expert-specified candle count from 'candel_required_for_judge': {recommended_count}")
    # Fallback to old field names for backward compatibility (temporary)
    elif 'recommended_candles_for_judge' in market_ctx:
        recommended_count = market_ctx.get('recommended_candles_for_judge', DEFAULT_CANDLES_FOR_JUDGE)
        logger.warning(f"Using deprecated field 'recommended_candles_for_judge'. Update to 'candel_required_for_judge'")
    elif 'recommended_candles' in market_ctx:
        recommended_count = market_ctx.get('recommended_candles', DEFAULT_CANDLES_FOR_JUDGE)
        logger.warning(f"Using deprecated field 'recommended_candles'. Update to 'candel_required_for_judge'")
    else:
        return DEFAULT_CANDLES_FOR_JUDGE
    
    # Clamp to valid range
    return min(max(int(recommended_count), MIN_CANDLES_FOR_JUDGE), MAX_CANDLES_FOR_JUDGE)




# ============================================================================
# EXPERT CALLS
# ============================================================================


def _cycle_htf_context(gpt_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """HTF trend/regime/ATR context, computed ONCE per GPT cycle.

    ``compute_htf_context`` runs EMA/ATR/ER math over ~270 HTF candles and was
    being recomputed up to five times per cycle (expert prompt, judge prompt,
    counter-trend policy, crypto vol gate). ``gpt_cfg`` is a fresh dict per
    cycle (built in the runner), so the result is memoized on it — identical
    output, one computation.
    """
    if "_htf_context_cache" not in gpt_cfg:
        if gpt_cfg.get("as_of") is not None:
            ctx = compute_htf_context(
                gpt_cfg.get("htf_candles") or {}, gpt_cfg.get("asset"), gpt_cfg["as_of"]
            )
        else:
            ctx = compute_htf_context(
                gpt_cfg.get("htf_candles") or {}, gpt_cfg.get("asset")
            )
        # Session-tape context (2026-07-25, LP-08 Phase 2a surface): the runner
        # sets gpt_cfg["session_tape"] ONLY when SESSION_TAPE mode is "context"
        # for the asset — nobody is enabled by default, so this branch is
        # dormant and the context stays byte-identical under "log"/"off".
        tape = gpt_cfg.get("session_tape")
        if tape and ctx:
            ctx["session_tape"] = {
                k: tape[k]
                for k in ("session", "direction", "move_pct", "er_session", "er_2h",
                          "conflict")
                if tape.get(k) is not None
            }
        gpt_cfg["_htf_context_cache"] = ctx
    return gpt_cfg["_htf_context_cache"]


def _stop_hint(c5_superset: List[Dict[str, Any]], gpt_cfg: Dict[str, Any]) -> Optional[str]:
    """Build a volatility-scaled stop hint for the prompt, or None (inert).

    Returns None unless ATR stops are enabled (``config.ATR_STOP_ENABLED``), so
    the prompt is unchanged by default. ATR is taken from the configured
    reference HTF when available, else computed from the 5m superset — so it
    works with or without the HTF fetch. Memoized per cycle on ``gpt_cfg``
    (same pattern as :func:`_cycle_htf_context`): the ATR scan over the HTF
    series runs once instead of once per prompt build.
    """
    if "_stop_hint_cache" in gpt_cfg:
        return gpt_cfg["_stop_hint_cache"]
    gpt_cfg["_stop_hint_cache"] = _compute_stop_hint(c5_superset, gpt_cfg)
    return gpt_cfg["_stop_hint_cache"]


def _compute_stop_hint(c5_superset: List[Dict[str, Any]], gpt_cfg: Dict[str, Any]) -> Optional[str]:
    """Uncached body of :func:`_stop_hint`."""
    from .config import ATR_STOP_ENABLED, ATR_STOP_REFERENCE_TF
    if not ATR_STOP_ENABLED:
        return None  # true no-op when disabled (skip all ATR computation)
    try:
        price = float(c5_superset[-1].get("close")) if c5_superset else None
    except (TypeError, ValueError, AttributeError):
        price = None
    htf = gpt_cfg.get("htf_candles") or {}
    atr_ref = None
    ref_candles = htf.get(ATR_STOP_REFERENCE_TF)
    if ref_candles:
        atr_ref = atr_from_candles(ref_candles)
    if atr_ref is None:
        atr_ref = atr_from_candles(c5_superset)
    # The session term of the floor is keyed on WHEN the stop is placed. Take
    # that from the newest 5m bar rather than the wall clock: identical in live
    # (the bar just closed), and the only correct answer in replay.
    from .market_structure import candle_time_utc
    as_of = gpt_cfg.get("as_of")
    if as_of is None and c5_superset:
        from datetime import timedelta
        latest_open = candle_time_utc(c5_superset[-1])
        as_of = latest_open + timedelta(minutes=5) if latest_open else None
    dist = recommended_stop_distance(atr_ref, price, gpt_cfg.get("asset"), as_of)
    if not dist:
        return None
    return (
        f"HARD FLOOR: final |entry - SL| must be >= {round(dist, 6)} price units "
        f"(volatility-scaled; stops inside this distance sit within normal noise and "
        f"will be auto-widened by the executor). Place the SL behind a real swing "
        f"H/L at or beyond this distance, then verify RR to TP1 >= 1.2 with that "
        f"final stop — if RR fails, the setup is too small: go flat."
    )


async def call_expert(
    kind: str,
    c5_superset: List[Dict[str, Any]],
    c15_superset: List[Dict[str, Any]],
    gpt_cfg: Dict[str, Any],
) -> Tuple[Union[MultiExpertSignal, Dict[str, Any]], float]:
    if kind not in _EXPERT_SYSTEM_PROMPTS:
        raise ValueError(f"Unsupported expert kind: {kind}")

    # Get system prompt (role & instructions), injecting per-asset rules.
    # Pass `asset` only to builders that declare it — inspecting the signature
    # instead of catching TypeError so a genuine TypeError raised *inside* a
    # builder is never silently swallowed into a degraded no-arg prompt.
    system_prompt_fn = _EXPERT_SYSTEM_PROMPTS[kind]
    _asset = gpt_cfg.get("asset")
    system_prompt = _build_system_prompt(system_prompt_fn, _asset)

    # Build compact arrays for prompt payloads using converter
    converter = CandleConverter()
    c5_compact = converter.to_compact_arrays(c5_superset, f"c5_{id(c5_superset)}")
    c15_compact = converter.to_compact_arrays(c15_superset, f"c15_{id(c15_superset)}")

    # Build user prompt (market data)
    # Optional higher-timeframe context + volatility-scaled stop hint
    # (both empty/inert unless their config flags are enabled).
    htf_context = _cycle_htf_context(gpt_cfg)
    stop_hint = _stop_hint(c5_superset, gpt_cfg)
    user_prompt = build_expert_user_prompt(
        c5_compact, c15_compact, asset=gpt_cfg.get("asset"),
        htf_context=htf_context, stop_hint=stop_hint,
        recent_trades=gpt_cfg.get("recent_trades"),
    )

    from .typed_setup import typed_prompt
    user_prompt += typed_prompt(c15_superset, gpt_cfg)
    from .entry_policy import prompt_entry_policy
    user_prompt += prompt_entry_policy(gpt_cfg.get("asset"), gpt_cfg.get("as_of"))

    # The single unified expert always uses the unified-expert JSON schema.
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": UNIFIED_EXPERT_JSON_SCHEMA["name"],
            "schema": {k: v for k, v in UNIFIED_EXPERT_JSON_SCHEMA.items() if k != "name"},
        },
    }

    # Time the expert call
    start_time = time.time()
    
    # Choose model: expert-specific > shared > default mini for cost/speed
    expert_model = (
        gpt_cfg.get("model_expert") or gpt_cfg.get("model") or MODEL_4O_MINI
    )
    import hashlib
    gpt_cfg["_resolved_model_expert"] = expert_model
    gpt_cfg["_typed_prompt_hash"] = hashlib.sha256((system_prompt+"\n"+user_prompt).encode()).hexdigest()
    # Configure expert call timing to avoid inner > outer timeout mismatches
    inner_timeout = int(gpt_cfg.get("timeout_seconds_expert", max(5, int(EXPERT_TIMEOUT_SECONDS) - 5)))

    # Log the EXACT prompt we built BEFORE handing it to the expert model.
    _log_prompt(
        "EXPERT",
        system_prompt,
        user_prompt,
        gpt_cfg,
        expert_kind=kind,
        model=expert_model,
        gpt_mode=f"multi_expert_{kind}",
    )

    result = await complete(
        prompt="",  # Empty for new format
        model=expert_model,
        store_response=gpt_cfg.get("store_response", False),
        asset=gpt_cfg.get("asset"),
        agent_id=gpt_cfg.get("agent_id"),
        prompt_hash=gpt_cfg.get("prompt_hash"),
        broker_type=gpt_cfg.get("broker_type"),
        gpt_mode=f"multi_expert_{kind}",
        response_format=response_format,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        temperature=float(gpt_cfg.get("temperature_expert", 0.0)),
        top_p=float(gpt_cfg.get("top_p_expert", 0.7)),
        timeout_seconds=inner_timeout,
        retries=int(gpt_cfg.get("retries_expert", 0)),
        **completion_options(expert_model, gpt_cfg),
    )

    expert_ms = (time.time() - start_time) * 1000

    if not isinstance(result, dict):
        raise ValueError(f"Expert {kind} did not return JSON, got: {type(result)}")

    # Handle unified expert response format (multiple proposals) vs single signal format
    if "proposals" in result and isinstance(result["proposals"], list):
        # Ensure candel_required_for_judge is present — GPT occasionally omits it.
        if "candel_required_for_judge" not in result:
            logger.warning(
                "Unified expert omitted 'candel_required_for_judge'; defaulting to %d",
                DEFAULT_CANDLES_FOR_JUDGE,
                extra={
                    "expert_kind": kind,
                    "got_fields": list(result.keys()),
                    "asset": gpt_cfg.get("asset"),
                    "agent_id": gpt_cfg.get("agent_id"),
                },
            )
            result["candel_required_for_judge"] = DEFAULT_CANDLES_FOR_JUDGE

        proposals_raw = [p for p in result["proposals"] if isinstance(p, dict)]

        if not proposals_raw:
            raise ValueError(f"Unified expert returned no valid proposals: {result}")

        if not (1 <= len(proposals_raw) <= 2):
            logger.warning(
                "Unified expert returned unexpected proposal count",
                extra={
                    "expert_kind": kind,
                    "proposals_count": len(proposals_raw),
                    "asset": gpt_cfg.get("asset"),
                    "agent_id": gpt_cfg.get("agent_id"),
                }
            )

        logger.info(
            "Unified expert call completed",
            extra={
                "expert_kind": kind,
                "expert_ms": round(expert_ms, 2),
                "proposals_count": len(proposals_raw),
                "candle_count_for_judge": result.get("candel_required_for_judge"),
                "asset": gpt_cfg.get("asset"),
                "agent_id": gpt_cfg.get("agent_id"),
            }
        )

        # Return the full response directly - no primary signal concept in new format
        return result, expert_ms
        
    else:
        # Legacy single signal format - backward compatibility
        try:
            signal = MultiExpertSignal(**result)
        except ValidationError as e:
            # Log detailed validation error for debugging
            logger.error(
                f"Expert {kind} returned invalid response structure: {e}",
                extra={
                    "expert_kind": kind,
                    "validation_errors": str(e),
                    "response_data": result,
                    "asset": gpt_cfg.get("asset"),
                    "agent_id": gpt_cfg.get("agent_id"),
                }
            )
            # Re-raise with more context
            raise ValueError(f"Expert {kind} response validation failed: {e}. Response: {result}")
        
        # Structured logging for expert call metrics
        logger.info(
            "Multi-expert call completed",
            extra={
                "expert_kind": kind,
                "expert_ms": round(expert_ms, 2),
                "signal": getattr(signal, "signal", None),
                "ttl_minutes": getattr(signal, "ttl_minutes", None),
                "confidence": getattr(signal, "confidence", None),
                "asset": gpt_cfg.get("asset"),
                "agent_id": gpt_cfg.get("agent_id"),
            }
        )

        return signal, expert_ms




def _score_proposal(
    proposal: MultiExpertSignal,
    c5_superset: List[Dict[str, Any]],
) -> float:
    """Score a proposal based on confidence, TTL, and market range.
    
    Higher scores are better. Flat signals return 0.
    Note: RR ratio calculation removed as validation is disabled.
    """
    if proposal.signal == "flat":
        return 0.0

    # Calculate components (without RR validation)
    median_rng = median_range_5m(c5_superset)
    
    # TTL penalty: shorter TTL is slightly preferred
    ttl_penalty = SCORE_TTL_PENALTIES.get(proposal.ttl_minutes, 1.0)
    
    # Combined score: confidence - TTL penalty + range factor
    return (proposal.confidence or 0.0) - ttl_penalty + (median_rng / 100)


def _apply_counter_trend_policy(proposals, htf_trend, asset):
    """Soft, configurable de-prioritisation of counter-HTF-trend proposals.

    Modes (``config.COUNTER_TREND_MODE``, per-asset overridable):
      - ``"off"``  : no effect (default) — nothing is filtered.
      - ``"soft"`` : multiply a counter-trend proposal's confidence by
                     ``(1 - COUNTER_TREND_CONFIDENCE_PENALTY)``. Never drops it,
                     so good reversals survive — they are merely de-prioritised
                     when proposals are scored/limited for the judge.
      - ``"gate"`` : drop counter-trend proposals whose confidence is below
                     ``COUNTER_TREND_MIN_CONFIDENCE`` (opt-in hard filter).

    Inert unless a directional ``htf_trend`` ('up'/'down') is known AND mode is
    not "off" — so with HTF disabled or trend 'mixed', this returns proposals
    unchanged.
    """
    from .config import (
        COUNTER_TREND_MODE, COUNTER_TREND_MODE_BY_ASSET,
        COUNTER_TREND_CONFIDENCE_PENALTY, COUNTER_TREND_MIN_CONFIDENCE,
        resolve_for_asset,
    )
    mode = resolve_for_asset(COUNTER_TREND_MODE_BY_ASSET, asset, COUNTER_TREND_MODE) or "off"
    if mode == "off" or htf_trend not in ("up", "down"):
        return proposals
    kept = []
    for p in proposals:
        sig = getattr(p.signal, "signal", None)
        is_counter = (sig == "buy" and htf_trend == "down") or (sig == "sell" and htf_trend == "up")
        if not is_counter:
            kept.append(p)
            continue
        conf = p.signal.confidence or 0.0
        if mode == "soft":
            new_conf = max(0.0, round(conf * (1.0 - COUNTER_TREND_CONFIDENCE_PENALTY), 4))
            try:
                p.signal = p.signal.model_copy(update={"confidence": new_conf})
            except Exception:
                pass
            logger.info(
                "Counter-trend soft haircut applied",
                extra={"signal": sig, "htf_trend": htf_trend, "old_confidence": conf,
                       "new_confidence": new_conf, "asset": asset},
            )
            kept.append(p)
        elif mode == "gate":
            if conf >= COUNTER_TREND_MIN_CONFIDENCE:
                kept.append(p)
            else:
                logger.info(
                    "Counter-trend proposal gated out",
                    extra={"signal": sig, "htf_trend": htf_trend, "confidence": conf,
                           "min_required": COUNTER_TREND_MIN_CONFIDENCE, "asset": asset},
                )
        else:
            kept.append(p)
    return kept


# ============================================================================
# JUDGE ORCHESTRATION
# ============================================================================


async def call_judge(
    c5_superset: List[Dict[str, Any]],
    c15_superset: List[Dict[str, Any]],
    proposals: Iterable[MultiExpertSignal],
    gpt_cfg: Dict[str, Any],
    expert_kinds: Optional[Dict[int, str]] = None,
    use_code_sniper: bool = False,
    expert_market_contexts: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Tuple[Optional[Union[MultiExpertSignal, CodeSniperSignal]], float, Dict[str, Any], None]:
    # Note: Validation is now done in the main multi_expert_decision function before calling judge
    # This function only receives pre-validated proposals, so no validation needed here
    logger.info(f"Judge processing {len(proposals)} pre-validated proposals")

    # Build comprehensive context for sophisticated code generation
    # Extract the single unified expert's proposal for the judge context.
    # Only the "trend" expert exists, so the context carries one slot.
    expert_signals: Dict[str, Any] = {'trend_proposal': {}}

    for proposal in proposals:
        proposal_kind = expert_kinds.get(id(proposal), 'unknown') if expert_kinds else 'unknown'

        if isinstance(proposal, dict):
            # Raw dict from expert: map to a compact structure for judge
            proposal_data = {
                'side': proposal.get('side'),
                'zone': proposal.get('zone') or {},
                'target_pattern': proposal.get('target_pattern'),
                'confidence': proposal.get('confidence'),
                'rationale': proposal.get('level_source') or proposal.get('notes') or proposal_kind
            }
        else:
            # Legacy object path - use helper function for type safety
            proposal_data = {
                'signal': proposal.signal,
                'entry': _safe_float(proposal.entry, 'entry'),
                'stop': _safe_float(proposal.stop, 'stop'),
                'target': _safe_float(proposal.target, 'target'),
                'confidence': _safe_float(proposal.confidence, 'confidence'),
                'rationale': proposal.rationale or f'{proposal_kind.title()} signal: {proposal.signal}'
            }

        # Only the unified "trend" expert feeds the judge context.
        if proposal_kind == 'trend':
            expert_signals['trend_proposal'] = proposal_data

    # Get current candle and historical data for advanced context
    # Ensure we have exactly the required candles: historical + 1 current
    required_candles = MIN_CANDLES_FOR_CODE_EXECUTION
    if not c5_superset or len(c5_superset) < required_candles:
        raise ValueError(f"c5_superset must contain at least {required_candles} candles for code execution, got {len(c5_superset) if c5_superset else 0}")
    
    current_candle = c5_superset[-1]  # Last candle is current
    historical_candles = c5_superset[-required_candles:-1]  # Previous candles for historical context
    
    # Validate we have exactly the required historical candles
    if len(historical_candles) != HISTORICAL_CANDLES_COUNT:
        raise ValueError(f"Expected exactly {HISTORICAL_CANDLES_COUNT} historical candles, got {len(historical_candles)}")
    
    # Build context based on mode
    # Get TTL from dynamic config or use default
    ttl_minutes = (getattr(list(proposals)[0], 'ttl_minutes', TTL_DEFAULT_MINUTES)
                   if proposals else TTL_DEFAULT_MINUTES)
    
    if use_code_sniper:
        # Use sophisticated context for code generation
        advanced_context = build_advanced_context(
            expert_signals=expert_signals,
            historical_candles=historical_candles,
            current_candle=current_candle,
            ttl_minutes=ttl_minutes
        )
        
        # Add judge-specific instructions for code generation
        advanced_context['code_generation_task'] = {
            'purpose': 'monitor_trigger_conditions',
            'candle_format': 'candles_5[0-39]=historical, candles_5[40]=current_to_check',
            'buffer_strategy': 'calculate_in_code_based_on_atr_volatility',
            'trigger_selection': 'best_single_opportunity_from_experts',
            'structure_validation': 'use_market_structure_zones',
            'return_format': 'hold_until_trigger_then_buy_sell'
        }
    else:
        # Use simple context for normal judge mode
        advanced_context = {
            'expert_signals': expert_signals,
            'current_price': current_candle.get('close', 0),
            'time_remaining': ttl_minutes,
            'market_summary': f"Current price: {current_candle.get('close', 0)}, TTL: {ttl_minutes} minutes"
        }

    prompt_payload = {
        "proposals": [p.model_dump() for p in proposals],
        "advanced_context": advanced_context
    }

    # Get system prompt (role & instructions), injecting per-asset rules
    _judge_asset = gpt_cfg.get("asset")
    if use_code_sniper:
        system_prompt = code_sniper_judge_system_prompt(asset=_judge_asset)
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": CODE_SNIPER_JSON_SCHEMA["name"],
                "schema": {k: v for k, v in CODE_SNIPER_JSON_SCHEMA.items() if k != "name"}
            }
        }
    else:
        system_prompt = judge_system_prompt(asset=_judge_asset)
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": TRADE_JSON_SCHEMA["name"],
                "schema": {k: v for k, v in TRADE_JSON_SCHEMA.items() if k != "name"}
            }
        }
    
    # Extract recommended candle count from expert's candel_required_for_judge field
    recommended_count = _extract_recommended_candle_count(expert_kinds, proposals, expert_market_contexts)
    
    # Slice recent candles for judge with checkpoint alignment
    # CRITICAL: Ensure judge receives exactly N candles ending at checkpoint-aligned candle
    # The last candle in c5_superset should be the checkpoint-aligned candle
    if len(c5_superset) >= recommended_count:
        # Take last N candles ending at checkpoint (last candle is checkpoint-aligned)
        recent_c5 = c5_superset[-recommended_count:]
        
        # Verify we got exactly the requested count
        if len(recent_c5) != recommended_count:
            logger.warning(
                f"Judge candle count mismatch: requested {recommended_count}, got {len(recent_c5)}"
            )
        
        # Validate last candle is checkpoint-aligned (should be last candle in superset)
        if recent_c5 and c5_superset:
            last_superset_time = c5_superset[-1].get('time')
            last_recent_time = recent_c5[-1].get('time')
            if last_superset_time != last_recent_time:
                logger.warning(
                    f"Judge candle alignment issue: last superset candle ({last_superset_time}) != "
                    f"last recent candle ({last_recent_time})"
                )
        
        logger.info(
            f"Providing last {recommended_count} candles to judge (total available: {len(c5_superset)})",
            extra={
                "judge_candle_source": "historical_from_c5_superset",
                "candle_count": len(recent_c5),
                "requested_count": recommended_count,
                "first_candle_time": recent_c5[0].get('time') if recent_c5 else None,
                "last_candle_time": recent_c5[-1].get('time') if recent_c5 else None,
                "checkpoint_aligned": True
            }
        )
    else:
        recent_c5 = c5_superset  # Use all available candles if not enough
        logger.info(
            f"Providing all {len(c5_superset)} available candles to judge (less than recommended {recommended_count})",
            extra={
                "judge_candle_source": "historical_from_c5_superset",
                "candle_count": len(recent_c5),
                "requested_count": recommended_count,
                "first_candle_time": recent_c5[0].get('time') if recent_c5 else None,
                "last_candle_time": recent_c5[-1].get('time') if recent_c5 else None,
                "checkpoint_aligned": True
            }
        )
    
    # Convert recent candles to compact arrays for judge prompt using converter
    converter = CandleConverter()
    recent_c5_compact = converter.to_compact_arrays(recent_c5, f"judge_c5_{id(recent_c5)}")
    
    # Also provide 15m candles for higher-timeframe context
    recent_c15_compact = converter.to_compact_arrays(c15_superset[-30:], f"judge_c15_{id(c15_superset)}") if c15_superset else None

    # Build user prompt (proposals & context with compact candles for both timeframes)
    user_prompt = build_judge_user_prompt(
        [p.model_dump() for p in proposals],
        advanced_context,
        recent_c5_compact,
        recent_candles_15m=recent_c15_compact,
        use_code_sniper=use_code_sniper,
        asset=gpt_cfg.get("asset"),
        htf_context=_cycle_htf_context(gpt_cfg),
        stop_hint=_stop_hint(c5_superset, gpt_cfg),
        recent_trades=gpt_cfg.get("recent_trades"),
    )

    if use_code_sniper:
        from .entry_policy import prompt_entry_policy
        user_prompt += prompt_entry_policy(gpt_cfg.get("asset"), gpt_cfg.get("as_of"))
        user_prompt += ("\nFor an actionable setup, include metadata.expected_horizon_minutes (5..480): "
                        "the estimated holding time needed to reach the structural objective after entry. "
                        "This is separate from the arming TTL. Do not shorten it to bypass session/event "
                        "limits; skip ideas whose objective needs more tradable time than remains.")

    # Time the judge call
    start_time = time.time()
    
    gpt_mode = "multi_expert_judge_code_sniper" if use_code_sniper else "multi_expert_judge"
    
    # Choose model with speed preference option
    prefer_speed = bool(gpt_cfg.get("judge_prefer_speed", False))
    default_judge_model = MODEL_4O_MINI if prefer_speed else MODEL_4
    judge_model = (
        gpt_cfg.get("model_judge") or gpt_cfg.get("model") or default_judge_model
    )
    gpt_cfg["_resolved_model_judge"] = judge_model
    # Configurable judge timeout and retries
    judge_timeout_cfg = int(gpt_cfg.get("timeout_seconds_judge", DEFAULT_JUDGE_TIMEOUT_SECONDS))
    judge_retries_cfg = int(gpt_cfg.get("retries_judge", 0))

    # Log the EXACT prompt we built BEFORE handing it to the judge model.
    _log_prompt(
        "JUDGE",
        system_prompt,
        user_prompt,
        gpt_cfg,
        judge_mode="code_sniper" if use_code_sniper else "standard",
        model=judge_model,
        gpt_mode=gpt_mode,
        proposals=len(proposals),
    )

    result = await complete(
        prompt="",  # Empty for new format
        model=judge_model,
        store_response=gpt_cfg.get("store_response", False),
        asset=gpt_cfg.get("asset"),
        agent_id=gpt_cfg.get("agent_id"),
        prompt_hash=gpt_cfg.get("prompt_hash"),
        broker_type=gpt_cfg.get("broker_type"),
        gpt_mode=gpt_mode,
        response_format=response_format,
        timeout_seconds=judge_timeout_cfg,
        retries=judge_retries_cfg,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        temperature=float(gpt_cfg.get("temperature_judge", 0.1)),
        top_p=float(gpt_cfg.get("top_p_judge", 0.9)),
        **completion_options(judge_model, gpt_cfg),
    )

    judge_ms = (time.time() - start_time) * 1000
    
    if isinstance(result, dict):
        try:
            if use_code_sniper:
                try:
                    # Clamp dynamic_config values before validation —
                    # LLMs (especially smaller models) sometimes return
                    # values outside the schema bounds.
                    dc = result.get("dynamic_config")
                    if isinstance(dc, dict):
                        dc["candles_5m_count"] = max(50, min(200, dc.get("candles_5m_count", 50)))
                        dc["candles_15m_count"] = max(20, min(100, dc.get("candles_15m_count", 50)))
                        # Snap, do not clamp: ttl_minutes is an ENUM, so a range
                        # clamp can emit a legal-looking value the schema rejects.
                        dc["ttl_minutes"] = coerce_ttl(dc.get("ttl_minutes"))
                    signal = CodeSniperSignal(**result)
                except ValidationError as e:
                    # Log detailed validation error for debugging
                    logger.error(
                        f"Code sniper judge returned invalid response structure: {e}",
                        extra={
                            "validation_errors": str(e),
                            "response_data": result,
                            "asset": gpt_cfg.get("asset"),
                            "agent_id": gpt_cfg.get("agent_id"),
                            "judge_ms": round(judge_ms, 2),
                        }
                    )
                    # Re-raise with more context
                    raise ValueError(f"Code sniper judge response validation failed: {e}. Response: {result}")
                # Check if judge decided no good setup exists. "rejected" is
                # treated identically to "flat" (2026-07-02 audit D6): a
                # program the judge itself rejected must never be monitored.
                decision_type = signal.metadata.get("decision", "")
                if decision_type in ("flat", "rejected"):
                    logger.info(
                        "Judge: No good setup in TTL window - waiting for next cycle",
                        extra={
                            "judge_ms": round(judge_ms, 2),
                            "metadata": signal.metadata,
                            "ttl_minutes": signal.ttl_minutes,
                            "asset": gpt_cfg.get("asset"),
                            "agent_id": gpt_cfg.get("agent_id"),
                            "note": "Market conditions not favorable - judge decided flat, no monitoring needed"
                        }
                    )
                    # Return None to indicate no trade setup - agent should wait for next cycle
                    # This is NOT an error - it's a valid decision that market conditions are unfavorable
                    return None, judge_ms, {}, None
                
                # Return CodeSniperSignal directly for monitoring
                # The code will be executed by the monitor on each 5-minute candle close
                final_decision = signal
                
                # Structured logging for code sniper judge decision - code will be monitored every 5min
                logger.info(
                    "Code sniper judge completed - code will be monitored every 5min",
                    extra={
                        "judge_ms": round(judge_ms, 2),
                        "code_length": len(signal.code),
                        "selected_from_proposals": len(proposals),
                        "ttl_minutes": signal.ttl_minutes,
                        "confidence": signal.confidence,
                        "asset": gpt_cfg.get("asset"),
                        "agent_id": gpt_cfg.get("agent_id"),
                        "note": "Code execution will happen in monitor on each 5min candle close"
                    }
                )
                return final_decision, judge_ms, {}, None
            else:
                try:
                    signal = MultiExpertSignal(**result)
                except ValidationError as e:
                    # Log detailed validation error for debugging
                    logger.error(
                        f"Judge returned invalid response structure: {e}",
                        extra={
                            "validation_errors": str(e),
                            "response_data": result,
                            "asset": gpt_cfg.get("asset"),
                            "agent_id": gpt_cfg.get("agent_id"),
                            "judge_ms": round(judge_ms, 2),
                        }
                    )
                    # Re-raise with more context
                    raise ValueError(f"Judge response validation failed: {e}. Response: {result}")
                
                # Skip validation - accept all judge decisions
                
                # Structured logging for judge decision
                logger.info(
                    "Multi-expert judge decision completed",
                    extra={
                        "judge_ms": round(judge_ms, 2),
                        "signal": signal.signal,
                        "selected_from_proposals": len(proposals),
                        "ttl_minutes": signal.ttl_minutes,
                        "confidence": signal.confidence,
                        "asset": gpt_cfg.get("asset"),
                        "agent_id": gpt_cfg.get("agent_id"),
                    }
                )
                return signal, judge_ms, {}, None
        except Exception as e:
            # Log the specific error for debugging
            logger.warning(
                f"Judge returned invalid signal: {e}",
                extra={
                    "error_type": type(e).__name__,
                    "error_message": str(e),
                    "asset": gpt_cfg.get("asset"),
                    "agent_id": gpt_cfg.get("agent_id"),
                }
            )

    # If we get here, handle error based on mode
    if use_code_sniper:
        # CRITICAL: No fallbacks - fail with error when code sniper fails
        logger.error(
            "Code sniper judge failed - no fallback allowed",
            extra={
                "judge_error": "Code sniper execution failed",
                "asset": gpt_cfg.get("asset"),
                "agent_id": gpt_cfg.get("agent_id"),
                "valid_proposals": len(proposals)
            }
        )
        raise ValueError(f"Code sniper judge failed with {len(proposals)} proposals")
    
    # Check if we have any proposals at all (not just valid ones)
    if not proposals:
        raise ValueError(
            f"Code sniper judge failed and no expert proposals available"
        )
    
    # NO FALLBACK POLICY: Judge failed - raise explicit error instead of using best proposal
    error_context = {
        "judge_ms": round(judge_ms, 2),
        "selected_from_proposals": len(proposals),
        "asset": gpt_cfg.get("asset"),
        "agent_id": gpt_cfg.get("agent_id"),
        "use_code_sniper": use_code_sniper,
        "reason": "Judge failed to produce valid decision"
    }
    
    logger.error(
        "Judge failed to produce valid decision - NO FALLBACK ALLOWED",
        extra=error_context
    )
    
    # Raise explicit error instead of falling back to best proposal
    raise ValueError(
        f"Judge failed to produce valid decision for {gpt_cfg.get('asset', 'unknown')}. "
        f"Judge processing time: {round(judge_ms, 2)}ms, "
        f"Proposals available: {len(proposals)}, "
        f"Code sniper mode: {use_code_sniper}, "
        f"Reason: Judge failed to produce valid decision. "
        f"NO FALLBACK TO BEST PROPOSAL ALLOWED."
    )


# ============================================================================
# MULTI-EXPERT ORCHESTRATION
# ============================================================================

async def multi_expert_decision(
    c5_superset: List[Dict[str, Any]],
    c15_superset: List[Dict[str, Any]],
    gpt_cfg: Dict[str, Any],
    use_code_sniper: bool = False,
    max_total_time_seconds: int = DEFAULT_MAX_TOTAL_TIME_SECONDS,
) -> Tuple[Union[MultiExpertSignal, CodeSniperSignal], Dict[str, Any], Optional[DynamicConfig]]:
    """Run multi-expert decision process with comprehensive metrics logging and time budget.
    
    This is the main entry point for multi-expert trading decisions. It orchestrates
    parallel expert calls, validates proposals, and coordinates judge decision.
    """
    kinds = EXPERT_KINDS  # Single unified intraday expert; config value is ["trend"]

    # Time the overall process with budget enforcement
    total_start_time = time.time()
    
    # Create timeout for entire multi-expert process
    async def _run_with_timeout():
        return await _multi_expert_decision_internal(
            c5_superset,
            c15_superset,
            gpt_cfg,
            use_code_sniper,
            total_start_time,
            max_total_time_seconds,
            cycle_doc=None,  # Regular mode
        )
    
    try:
        final_decision, _, _ = await asyncio.wait_for(_run_with_timeout(), timeout=max_total_time_seconds)
        return final_decision, {}, None
    except asyncio.TimeoutError:
        total_ms = (time.time() - total_start_time) * 1000
        error_msg = (
            f"Multi-expert decision exceeded time budget of {max_total_time_seconds}s "
            f"for asset {gpt_cfg.get('asset', 'unknown')}. "
            f"Actual time: {round(total_ms, 2)}ms"
        )
        logger.error("Multi-expert decision timeout", extra={
            "asset": gpt_cfg.get("asset"),
            "agent_id": gpt_cfg.get("agent_id"),
            "timeout_seconds": max_total_time_seconds,
            "actual_ms": round(total_ms, 2),
            "error_type": "timeout",
            "error_category": "decision_process",
            "budget_exceeded_by_ms": round(total_ms - (max_total_time_seconds * 1000), 2)
        })
        raise ValueError(error_msg)


async def _multi_expert_decision_internal(
    c5_superset: List[Dict[str, Any]],
    c15_superset: List[Dict[str, Any]],
    gpt_cfg: Dict[str, Any],
    use_code_sniper: bool,
    total_start_time: float,
    max_total_time_seconds: int,
    cycle_doc: Optional[MultiExpertCycleDoc] = None,
) -> Tuple[Union[MultiExpertSignal, CodeSniperSignal], Union[Dict[str, Any], MultiExpertCycleDoc], Optional[DynamicConfig]]:
    """Internal implementation of multi-expert decision process."""
    # Normalise candles to dicts once — downstream code uses .get() / ['key']
    c5_superset = _ensure_candle_dicts(c5_superset)
    c15_superset = _ensure_candle_dicts(c15_superset)

    # ── Crypto volatility hard gate (dead-tape skip) ──────────────────────────
    # Additive & inert for every non-crypto asset, and only when
    # BTC_ATR_GATE_ENABLED. When the 1h ATR% is below BTC_ATR_PCT_GATE the
    # tape is too dead to trade (report §8: those BTC entries netted −$140) so
    # we return flat *before* spending any expert/judge calls. Applied to the
    # whole crypto class (BTC + ETH) — ETH previously ran with none of BTC's
    # protections. Fail-open: if ATR% cannot be computed (too few 1h candles)
    # or anything raises, the cycle runs.
    try:
        from .config import BTC_ATR_GATE_ENABLED, BTC_ATR_PCT_GATE
        _gate_htf = _cycle_htf_context(gpt_cfg)
        _blocked, _atr_pct_1h = _crypto_vol_gate_blocked(
            gpt_cfg.get("asset"), _gate_htf, BTC_ATR_GATE_ENABLED, BTC_ATR_PCT_GATE
        )
        if _blocked:
            logger.info(
                "Crypto volatility gate: 1h ATR% below floor — skipping cycle (flat)",
                extra={
                    "asset": gpt_cfg.get("asset"),
                    "atr_pct_1h": _atr_pct_1h,
                    "btc_atr_pct_gate": BTC_ATR_PCT_GATE,
                    "regime_hint": _gate_htf.get("regime_hint"),
                    "er_1h": _gate_htf.get("er_1h"),
                },
            )
            flat_signal = _create_flat_signal(
                f"Crypto volatility gate: 1h ATR% {_atr_pct_1h:.3f} < {BTC_ATR_PCT_GATE:.3f}"
            )
            if cycle_doc:
                cycle_doc.judge = JudgeOutcome(
                    selected="flat",
                    rationale=f"Crypto volatility gate: 1h ATR% {_atr_pct_1h:.3f} < floor {BTC_ATR_PCT_GATE:.3f}",
                    final_signal=flat_signal.model_dump() if hasattr(flat_signal, 'model_dump') else None,
                    validation_summary={"gated": True, "atr_pct_1h": _atr_pct_1h, "btc_atr_pct_gate": BTC_ATR_PCT_GATE},
                )
                cycle_doc.status = 'completed'
                cycle_doc.completed_at = datetime.now(timezone.utc)
                cycle_doc.error = None
                return flat_signal, cycle_doc, None
            return flat_signal, {}, None
    except Exception as _vol_gate_err:
        logger.warning(f"Crypto volatility gate check failed (allowing cycle): {_vol_gate_err}")

    kinds = EXPERT_KINDS  # Single unified intraday expert; config value is ["trend"]
    
    async def _invoke_expert(kind: str) -> Tuple[str, Optional[Union[MultiExpertSignal, Dict[str, Any]]], Optional[float], Optional[Dict[str, Any]]]:
        # Allow overriding the outer timeout via config, default to constant
        timeout = float(gpt_cfg.get("timeout_seconds_expert_outer", EXPERT_TIMEOUT_SECONDS))
        expert_start_time = time.time()
        
        try:
            result, expert_ms = await asyncio.wait_for(
                call_expert(kind, c5_superset, c15_superset, gpt_cfg),
                timeout=timeout,
            )
            
            # Build market analysis for detailed tracking
            market_analysis = None
            if cycle_doc:
                if isinstance(result, dict) and "proposals" in result:
                    # Unified expert format
                    market_analysis = {
                        "c5_candles_count": len(c5_superset),
                        "c15_candles_count": len(c15_superset),
                        "current_price": c5_superset[-1].get("close") if c5_superset else None,
                        "expert_kind": kind,
                        "proposals_count": len(result.get('proposals', [])),
                        "candel_required_for_judge": result.get('candel_required_for_judge', DEFAULT_CANDLES_FOR_JUDGE),
                    }
                else:
                    # Legacy single signal format
                    signal = result
                    signal_data = {
                        "signal": signal.signal if signal else None,
                        "confidence": signal.confidence if signal else None,
                        "ttl_minutes": signal.ttl_minutes if signal else None,
                        "rationale": signal.rationale if signal else None
                    }
                    market_analysis = {
                        "c5_candles_count": len(c5_superset),
                        "c15_candles_count": len(c15_superset),
                        "current_price": c5_superset[-1].get("close") if c5_superset else None,
                        "expert_kind": kind,
                        **signal_data
                    }
            
            return kind, result, expert_ms, market_analysis
            
        except asyncio.TimeoutError:
            expert_ms = (time.time() - expert_start_time) * 1000
            logger.warning(
                "Expert call timed out",
                extra={
                    "expert_kind": kind,
                    "timeout_seconds": timeout,
                    "actual_ms": round(expert_ms, 2),
                    "asset": gpt_cfg.get("asset"),
                    "agent_id": gpt_cfg.get("agent_id"),
                    "cycle_id": cycle_doc.cycle_id if cycle_doc else None,
                    "error_type": "timeout"
                },
            )
            return kind, None, expert_ms, None
        except Exception as e:
            expert_ms = (time.time() - expert_start_time) * 1000
            logger.exception(
                "Expert call failed",
                extra={
                    "expert_kind": kind,
                    "actual_ms": round(expert_ms, 2),
                    "asset": gpt_cfg.get("asset"),
                    "agent_id": gpt_cfg.get("agent_id"),
                    "cycle_id": cycle_doc.cycle_id if cycle_doc else None,
                    "error": str(e),
                    "error_type": type(e).__name__,
                    "error_category": "expert_execution"
                },
            )
            return kind, None, expert_ms, None

    gather_results = await asyncio.gather(
        *(_invoke_expert(kind) for kind in kinds), return_exceptions=True
    )

    proposals: List[ExpertProposal] = []  # Use ExpertProposal dataclass
    expert_timings: Dict[str, float] = {}
    expert_market_contexts: Dict[str, Dict[str, Any]] = {}  # Store market_context from unified expert

    for result in gather_results:
        if isinstance(result, Exception):
            logger.error(
                "Unexpected expert error",
                exc_info=(type(result), result, result.__traceback__),
            )
            continue

        kind, expert_result, expert_ms, market_analysis = result
        if expert_result is None or expert_ms is None:
            logger.warning(f"Expert {kind} returned None signal or timing")
            continue

        # Expert results are no longer persisted; keep data in memory only for judge

        # Handle unified expert response (dict with proposals) vs single signal
        if isinstance(expert_result, dict) and "proposals" in expert_result:
            # New unified expert format - process proposals into ExpertProposal objects
            all_proposals = expert_result.get('proposals', [])
            candel_count = expert_result.get('candel_required_for_judge', DEFAULT_CANDLES_FOR_JUDGE)

            # Store candle count for judge in expert_market_contexts
            market_ctx_with_candles = {'candel_required_for_judge': candel_count}
            expert_market_contexts[kind] = market_ctx_with_candles
            
            # Convert and add all proposals as ExpertProposal objects
            for proposal in all_proposals:
                if hasattr(proposal, 'model_dump'):
                    # Already a MultiExpertSignal object
                    expert_proposal = ExpertProposal(
                        signal=proposal,
                        expert_kind=kind,
                        market_context=market_ctx_with_candles,
                        timing_ms=expert_ms
                    )
                    proposals.append(expert_proposal)
                elif isinstance(proposal, dict):
                    # Convert dict to MultiExpertSignal with validation
                    try:
                        signal_obj = _convert_dict_to_signal(proposal)
                        expert_proposal = ExpertProposal(
                            signal=signal_obj,
                            expert_kind=kind,
                            market_context=market_ctx_with_candles,
                            timing_ms=expert_ms
                        )
                        proposals.append(expert_proposal)
                    except ValueError as e:
                        logger.error(
                            f"Proposal validation failed: {e}",
                            extra={
                                "expert_kind": kind,
                                "proposal": proposal,
                                "asset": gpt_cfg.get("asset"),
                                "agent_id": gpt_cfg.get("agent_id"),
                            }
                        )
        else:
            # Legacy single signal format - validate signal is not None
            signal = expert_result
            if signal is None:
                logger.warning(f"Legacy expert {kind} returned None signal - skipping")
                continue
            
            # Create ExpertProposal object
            expert_proposal = ExpertProposal(
                signal=signal,
                expert_kind=kind,
                market_context={},
                timing_ms=expert_ms
            )
            proposals.append(expert_proposal)
        
        expert_timings[kind] = expert_ms
    
    # All proposals are now ExpertProposal objects
    sigs = [p.signal.signal for p in proposals]
    logger.info(
        "Expert proposals collected",
        extra={
            "proposal_count": len(proposals),
            "proposal_signals": sigs,
            "expert_kinds": list(expert_timings.keys()),
            "asset": gpt_cfg.get("asset"),
            "agent_id": gpt_cfg.get("agent_id"),
            "cycle_id": cycle_doc.cycle_id if cycle_doc else None,
            "validation_mode": "disabled"
        }
    )
    # Soft, configurable counter-trend policy (inert unless COUNTER_TREND_MODE
    # != "off" AND a directional HTF trend is known). Default off → no effect.
    _htf_trend = _cycle_htf_context(gpt_cfg).get("htf_trend")
    valid_proposals = _apply_counter_trend_policy(proposals, _htf_trend, gpt_cfg.get("asset"))
    
    # GRACEFUL SKIP: If no proposals available, return skipped result
    if not valid_proposals:
        total_ms = (time.time() - total_start_time) * 1000
        skip_context = {
            "asset": gpt_cfg.get("asset"),
            "agent_id": gpt_cfg.get("agent_id"),
            "total_ms": round(total_ms, 2),
            "total_proposals": len(proposals),
            "reason": "No expert proposals available"
        }
        
        # Log as INFO/WARNING, not ERROR - this is a normal "no trade" outcome
        logger.info(
            "No expert proposals available - returning skipped status",
            extra=skip_context
        )
        
        # Return a flat signal with skipped indication instead of raising
        flat_signal = _create_flat_signal("No expert proposals available - no trade signal generated")
        
        # Handle detailed mode
        if cycle_doc:
            cycle_doc.judge = JudgeOutcome(
                selected=None,
                rationale="No expert proposals available",
                final_signal=None,
                validation_summary={"total_proposals": len(proposals), "reason": "No proposals"}
            )
            cycle_doc.status = 'skipped'
            cycle_doc.completed_at = datetime.now(timezone.utc)
            cycle_doc.error = None
            return flat_signal, cycle_doc, None
        else:
            return flat_signal, {}, None
    
    # Skip the judge when every valid proposal is flat — in BOTH modes.
    # The code-sniper exemption here used to force the judge to emit
    # monitoring code even when its own experts saw nothing (2026-07-02 audit
    # D5/F5: 310/310 cycles produced code in an ER≤0.08 week; the trades it
    # manufactured in chop were the USOIL/USTEC bleed). "Flat means flat"
    # also saves the judge API call in exactly the cycles it adds no value.
    flat_proposals = [p for p in valid_proposals if p.signal.signal == "flat"]
    if len(flat_proposals) == len(valid_proposals) and len(valid_proposals) > 0:
        logger.info(f"All {len(valid_proposals)} valid proposals are flat - skipping judge and returning flat signal")

        # Create a flat signal as the final decision
        final_decision = _create_flat_signal("All experts returned flat signals - no trading opportunity identified")

        # Handle detailed mode
        if cycle_doc:
            cycle_doc.judge = JudgeOutcome(
                selected="flat",
                rationale="All experts returned flat signals - no judge needed",
                final_signal=final_decision.model_dump(),
                validation_summary={"total_proposals": len(proposals), "all_flat": True}
            )
            return final_decision, cycle_doc, None
        else:
            return final_decision, {}, None

    # Only call judge if we have valid proposals
    # Limit proposals to top-N to reduce token size; include at most one flat
    max_judge_proposals = int(gpt_cfg.get("judge_max_proposals", JUDGE_MAX_PROPOSALS))
    
    # Use ProposalScorer for consistent scoring
    scorer = ProposalScorer(c5_superset)
    
    # All proposals are ExpertProposal objects
    non_flat = [p for p in valid_proposals if p.signal.signal != "flat"]
    flat = [p for p in valid_proposals if p.signal.signal == "flat"]
    
    # Score non-flat proposals using ProposalScorer
    non_flat_sorted = sorted(non_flat, key=scorer.score, reverse=True)
    limited = non_flat_sorted[:max_judge_proposals]
    if flat:
        limited.append(flat[0])
    logger.info(
        "Calling judge with limited proposals",
        extra={
            "limited_count": len(limited),
            "total_count": len(valid_proposals),
            "non_flat_count": len(non_flat),
            "flat_count": len(flat),
            "max_judge_proposals": max_judge_proposals,
            "asset": gpt_cfg.get("asset"),
            "agent_id": gpt_cfg.get("agent_id"),
            "cycle_id": cycle_doc.cycle_id if cycle_doc else None,
            "judge_mode": "code_sniper" if use_code_sniper else "standard"
        }
    )

    # Enforce remaining outer budget for judge via local timeout wrapper
    elapsed = time.time() - total_start_time
    remaining = max_total_time_seconds - elapsed
    judge_safety_buffer = JUDGE_SAFETY_BUFFER_SECONDS
    judge_timeout = max(1.0, remaining - judge_safety_buffer)
    logger.info(
        "Enforcing remaining time budget for judge",
        extra={
            "remaining_seconds": round(remaining, 2),
            "judge_timeout_seconds": round(judge_timeout, 2),
            "max_total_seconds": max_total_time_seconds,
            "elapsed_seconds": round(elapsed, 2),
            "safety_buffer_seconds": judge_safety_buffer,
            "asset": gpt_cfg.get("asset"),
            "agent_id": gpt_cfg.get("agent_id"),
            "cycle_id": cycle_doc.cycle_id if cycle_doc else None,
            "budget_utilization": round((elapsed / max_total_time_seconds) * 100, 1)
        },
    )
    # Convert ExpertProposal objects back to MultiExpertSignal for judge
    limited_signals = [p.signal for p in limited]
    
    # Create expert_kinds mapping for backward compatibility
    expert_kinds = {id(p.signal): p.expert_kind for p in limited}
    
    from .typed_setup import conditional_compile
    final_decision = conditional_compile([p.signal for p in valid_proposals], c5_superset, c15_superset, gpt_cfg) if use_code_sniper else None
    judge_ms = 0.0
    if final_decision is None:
        final_decision, judge_ms, _, _ = await asyncio.wait_for(
            call_judge(c5_superset, c15_superset, limited_signals, gpt_cfg,
                       expert_kinds, use_code_sniper, expert_market_contexts),
            timeout=judge_timeout,
        )
        if isinstance(final_decision, CodeSniperSignal) and gpt_cfg.get("typed_compilation"):
            final_decision.metadata["typed_shadow"] = gpt_cfg["typed_compilation"]
    
    total_ms = (time.time() - total_start_time) * 1000
    
    # Handle no valid decision: flat (valid) vs actual failure
    if final_decision is None:
        total_ms = (time.time() - total_start_time) * 1000
        
        if judge_ms > 0:
            # Judge ran successfully but decided flat — valid "no trade" decision
            logger.info(
                "Judge decided flat - no trade setup in current conditions",
                extra={
                    "asset": gpt_cfg.get("asset"),
                    "agent_id": gpt_cfg.get("agent_id"),
                    "total_ms": round(total_ms, 2),
                    "judge_ms": round(judge_ms, 2),
                    "total_proposals": len(proposals),
                }
            )
        else:
            # Judge never ran or failed before producing output
            logger.error(
                "Judge failed to produce valid decision",
                extra={
                    "asset": gpt_cfg.get("asset"),
                    "total_ms": round(total_ms, 2),
                    "judge_ms": round(judge_ms, 2),
                    "total_proposals": len(proposals),
                }
            )
        
        # Update cycle_doc for flat outcome
        if cycle_doc:
            cycle_doc.judge = JudgeOutcome(
                selected="flat",
                rationale="Judge decided flat - no trade setup",
                final_signal=None,
                validation_summary={"total_proposals": len(proposals), "selected": False}
            )
            cycle_doc.status = 'completed'
            cycle_doc.completed_at = datetime.now(timezone.utc)
        
        # Return None — callers handle this as a valid skip
        if cycle_doc:
            return None, cycle_doc, None
        else:
            return None, {}, None
    
    # ── Adaptive re-entry confidence gate ─────────────────────────────────────
    # The required confidence floor holds at full REENTRY_MIN_CONFIDENCE for the
    # first REENTRY_COOLDOWN_BARS after the last close (the "cooldown window"),
    # then decays linearly to 0.0 at REENTRY_EXEMPT_BARS. This backstops the LLM:
    # the judge may approve a re-entry, but if confidence is below the adaptive
    # floor the signal is suppressed to flat. Inert when REENTRY_COOLDOWN_BARS=0.
    _final_sig = getattr(final_decision, 'signal', None)
    if isinstance(final_decision, CodeSniperSignal) and final_decision.metadata.get("typed_setup"):
        _final_sig = {"long": "buy", "short": "sell"}.get(final_decision.metadata["typed_setup"].get("side"))
    if _final_sig not in (None, "flat"):
        try:
            from .config import REENTRY_COOLDOWN_BARS, REENTRY_EXEMPT_BARS, REENTRY_MIN_CONFIDENCE
            if REENTRY_COOLDOWN_BARS > 0:
                latest = _most_recent_closed_trade(gpt_cfg.get("recent_trades"))
                if latest is not None:
                    bars = latest.get("bars_elapsed")
                    if bars is not None and bars < REENTRY_EXEMPT_BARS:
                        # Sweep re-entry carve-out: a SAME-side re-entry after a
                        # losing close is the setup the prompts explicitly hunt
                        # (see config.SWEEP_REENTRY_CARVEOUT_ENABLED). Direction
                        # flips after a stop-out stay on the full floor — that is
                        # the revenge pattern this gate exists for.
                        from .config import (
                            SWEEP_REENTRY_CARVEOUT_ENABLED,
                            SWEEP_REENTRY_MIN_CONFIDENCE,
                        )
                        effective_min_conf = REENTRY_MIN_CONFIDENCE
                        is_sweep_reentry = (
                            SWEEP_REENTRY_CARVEOUT_ENABLED
                            and latest.get("result") == "loss"
                            and str(latest.get("side") or "").lower() == str(_final_sig).lower()
                        )
                        if is_sweep_reentry:
                            effective_min_conf = SWEEP_REENTRY_MIN_CONFIDENCE

                        adaptive_floor = _reentry_adaptive_floor(
                            bars, REENTRY_COOLDOWN_BARS, REENTRY_EXEMPT_BARS, effective_min_conf
                        )
                        signal_conf = getattr(final_decision, 'confidence', 1.0) or 0.0
                        if is_sweep_reentry:
                            logger.info(
                                "Sweep re-entry carve-out applied — same-side after a stop-out",
                                extra={
                                    "asset": gpt_cfg.get("asset"),
                                    "signal": _final_sig,
                                    "bars_since_last_close": bars,
                                    "floor_without_carveout": round(
                                        _reentry_adaptive_floor(
                                            bars, REENTRY_COOLDOWN_BARS,
                                            REENTRY_EXEMPT_BARS, REENTRY_MIN_CONFIDENCE,
                                        ), 3),
                                    "floor_applied": round(adaptive_floor, 3),
                                },
                            )
                        if adaptive_floor > 0 and signal_conf < adaptive_floor:
                            logger.info(
                                "Re-entry gated: confidence below adaptive floor",
                                extra={
                                    "asset": gpt_cfg.get("asset"),
                                    "signal": _final_sig,
                                    "confidence": signal_conf,
                                    "adaptive_floor": round(adaptive_floor, 3),
                                    "bars_since_last_close": bars,
                                    "reentry_exempt_bars": REENTRY_EXEMPT_BARS,
                                },
                            )
                            flat_signal = _create_flat_signal(
                                f"Re-entry gated: confidence {signal_conf:.2f} < floor "
                                f"{adaptive_floor:.2f} ({bars} bars since last close)"
                            )
                            if cycle_doc:
                                cycle_doc.judge = JudgeOutcome(
                                    selected="flat",
                                    rationale=f"Adaptive re-entry gate: conf {signal_conf:.2f} < floor {adaptive_floor:.2f}",
                                    final_signal=flat_signal.model_dump() if hasattr(flat_signal, 'model_dump') else None,
                                    validation_summary={"gated": True, "bars_since_close": bars, "adaptive_floor": adaptive_floor},
                                )
                                cycle_doc.status = 'completed'
                                cycle_doc.completed_at = datetime.now(timezone.utc)
                                cycle_doc.error = None
                                return flat_signal, cycle_doc, None
                            return flat_signal, {}, None
        except Exception as _gate_err:
            logger.warning(f"Re-entry gate check failed (allowing signal): {_gate_err}")

    # Update cycle_doc with judge outcome if in detailed mode
    if cycle_doc:
        cycle_doc.judge = JudgeOutcome(
            selected=getattr(final_decision, 'signal', 'code_sniper'),
            rationale=trade_reason(final_decision),
            final_signal=final_decision.model_dump() if hasattr(final_decision, 'model_dump') else None,
            validation_summary={"total_proposals": len(proposals), "selected": True}
        )
        cycle_doc.status = 'completed'
        cycle_doc.completed_at = datetime.now(timezone.utc)
        cycle_doc.error = None
    
    # Calculate basic metrics for final decision
    final_metrics = {}
    if isinstance(final_decision, CodeSniperSignal):
        from .typed_setup import snapshot_id, VERSION
        final_decision.metadata["decision_trace"] = {
            "snapshot_id": snapshot_id(c5_superset, c15_superset, gpt_cfg.get("as_of")),
            "compiler_version": VERSION if final_decision.metadata.get("typed_setup") else None,
            "expert_model": gpt_cfg.get("_resolved_model_expert"),
            "judge_model": gpt_cfg.get("_resolved_model_judge") if judge_ms else None,
            "expert_prompt_hash": gpt_cfg.get("_typed_prompt_hash"),
            "expert_ms": sum(expert_timings.values()), "judge_ms": judge_ms, "total_ms": total_ms,
        }
        final_metrics["code_length"] = len(final_decision.code)
        final_metrics["strategy_type"] = final_decision.metadata.get("strategy_type", "unknown")
    
    # Enhanced structured logging with comprehensive timing metrics
    structured_metrics = {
        "expert_kind": "multi_expert_orchestrator",
        "expert_ms": round(sum(expert_timings.values()), 2),
        "judge_ms": round(judge_ms, 2),
        "total_ms": round(total_ms, 2),
        "processing_ms": round(total_ms - sum(expert_timings.values()) - judge_ms, 2),
        "ttl_minutes": final_decision.ttl_minutes,
        "confidence": final_decision.confidence,
        "expert_timings": expert_timings,
        "performance": {
            "expert_parallel_efficiency": round(sum(expert_timings.values()) / max(expert_timings.values()), 2) if expert_timings and max(expert_timings.values()) > 0 else 1.0,
            "total_efficiency": round((sum(expert_timings.values()) + judge_ms) / total_ms if total_ms > 0 else 1.0, 2),
            "api_calls_count": len(expert_timings) + (1 if judge_ms > 0 else 0),
            "processing_overhead_ms": round(total_ms - sum(expert_timings.values()) - judge_ms, 2)
        },
        "asset": gpt_cfg.get("asset"),
        "agent_id": gpt_cfg.get("agent_id"),
    }
    
    # Add signal-specific metrics
    if isinstance(final_decision, MultiExpertSignal):
        structured_metrics.update({
            "final_signal": final_decision.signal,
        })
    elif isinstance(final_decision, CodeSniperSignal):
        structured_metrics.update({
            "code_length": final_metrics.get("code_length"),
            "strategy_type": final_metrics.get("strategy_type"),
            "mode": "code_sniper",
        })
    
    # dynamic_config removed: do not include in metrics
    
    logger.info(
        "Multi-expert decision process completed",
        extra={
            **structured_metrics,
            "performance_breakdown": {
                "expert_phase_ms": round(sum(expert_timings.values()), 2),
                "judge_phase_ms": round(judge_ms, 2),
                "processing_overhead_ms": round(total_ms - sum(expert_timings.values()) - judge_ms, 2),
                "efficiency_ratio": round((sum(expert_timings.values()) + judge_ms) / total_ms if total_ms > 0 else 1.0, 3),
                "api_calls_count": len(expert_timings) + (1 if judge_ms > 0 else 0),
                "avg_expert_time_ms": round(sum(expert_timings.values()) / len(expert_timings) if expert_timings else 0, 2)
            },
            "decision_summary": {
                "final_signal": final_decision.signal if hasattr(final_decision, 'signal') else "code_sniper",
                "confidence": getattr(final_decision, 'confidence', None),
                "ttl_minutes": getattr(final_decision, 'ttl_minutes', None),
                "mode": "code_sniper" if isinstance(final_decision, CodeSniperSignal) else "standard"
            }
        }
    )
    
    # Handle detailed mode
    if cycle_doc:
        return final_decision, cycle_doc, None
    else:
        return final_decision, {}, None


@decision_accounting
async def multi_expert_decision_with_details(
    c5_superset: List[Dict[str, Any]],
    c15_superset: List[Dict[str, Any]],
    gpt_cfg: Dict[str, Any],
    cycle_id: str,
    use_code_sniper: bool = False,
    max_total_time_seconds: int = DEFAULT_MAX_TOTAL_TIME_SECONDS,
) -> Tuple[Union[MultiExpertSignal, CodeSniperSignal], MultiExpertCycleDoc]:
    """Run multi-expert decision process with detailed cycle tracking for database storage."""
    from datetime import datetime, timezone
    
    kinds = EXPERT_KINDS  # Single unified intraday expert; config value is ["trend"]
    started_at = datetime.now(timezone.utc)
    
    # Initialize cycle document with mode validation
    mode = 'multi_expert' if not use_code_sniper else 'multi_expert_code_sniper'
    valid_modes = {'multi_expert', 'normal', 'multi_expert_code_sniper'}
    if mode not in valid_modes:
        raise ValueError(f"Invalid mode assignment: {mode}. Must be one of {valid_modes}")
    
    cycle_doc = MultiExpertCycleDoc(
        cycle_id=cycle_id,
        mode=mode,
        agent_id=gpt_cfg.get("agent_id", "unknown"),
        asset=gpt_cfg.get("asset", "unknown"),
        symbol=gpt_cfg.get("symbol", gpt_cfg.get("asset", "unknown")),
        started_at=started_at,
        status='skipped',
        experts=[],
        judge=None
    )
    
    try:
        # Time the overall process with budget enforcement
        total_start_time = time.time()
        
        # Create timeout for entire multi-expert process
        async def _run_with_timeout():
            return await _multi_expert_decision_internal(
                c5_superset, c15_superset, gpt_cfg, use_code_sniper, 
                total_start_time, max_total_time_seconds, cycle_doc
            )
        
        try:
            final_decision, detailed_cycle, _ = await asyncio.wait_for(_run_with_timeout(), timeout=max_total_time_seconds)
            return final_decision, detailed_cycle
        except asyncio.TimeoutError:
            total_ms = (time.time() - total_start_time) * 1000
            error_msg = (
                f"Multi-expert decision exceeded time budget of {max_total_time_seconds}s "
                f"for asset {gpt_cfg.get('asset', 'unknown')}. "
                f"Actual time: {round(total_ms, 2)}ms"
            )
            logger.error("Multi-expert timeout", extra={
                "asset": gpt_cfg.get("asset"),
                "agent_id": gpt_cfg.get("agent_id"),
                "timeout_seconds": max_total_time_seconds,
                "actual_ms": round(total_ms, 2)
            })
            cycle_doc.completed_at = datetime.now(timezone.utc)
            cycle_doc.status = 'failed'
            cycle_doc.error = error_msg
            # Attach cycle_doc to exception for storage in caller
            exc = ValueError(error_msg)
            exc.cycle_doc = cycle_doc  # type: ignore
            raise exc
            
    except Exception as e:
        cycle_doc.completed_at = datetime.now(timezone.utc)
        cycle_doc.status = 'failed'
        cycle_doc.error = str(e)
        # Attach cycle_doc to exception for storage in caller
        if not hasattr(e, 'cycle_doc'):
            e.cycle_doc = cycle_doc  # type: ignore
        raise


__all__ = [
    "call_expert",
    "call_judge",
    "multi_expert_decision",
    "multi_expert_decision_with_details",
]
