"""Configurable expert and judge prompt builders."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .asset_registry import profile_for
from .config import PROMPT_FLOAT_DECIMALS, PROMPT_MIN_RR as MIN_RR, TTL_ENUM

if TYPE_CHECKING:
    from .models import RecentTradeView


def _round_floats(obj, ndigits: int = PROMPT_FLOAT_DECIMALS):
    """Round floats only while serializing model inputs."""
    if isinstance(obj, float):
        return round(obj, ndigits)
    if isinstance(obj, dict):
        return {key: _round_floats(value, ndigits) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_round_floats(value, ndigits) for value in obj]
    return obj


def _dumps(obj) -> str:
    return json.dumps(_round_floats(obj), separators=(",", ":"))


def _asset_prompt_line(asset: str | None) -> str:
    symbol = (asset or "").strip().upper()
    if not symbol:
        return ""
    profile = profile_for(symbol)
    detail = profile.one_liner if profile else "Trading instrument under analysis."
    return f"ASSET: {symbol} - {detail}"


def unified_expert_system_prompt(asset: str | None = None) -> str:
    """Return a deliberately basic public expert prompt."""
    asset_line = _asset_prompt_line(asset)
    prefix = f"{asset_line}\n" if asset_line else ""
    return (
        f"{prefix}You are a market-analysis expert. Review only the supplied closed "
        "candles and context. Return JSON matching the provided schema. Propose buy, "
        "sell, or flat; do not propose opposite directions in one response. Include "
        "entry, structural stop, nearest supported target, confidence, rationale, "
        f"ttl_minutes from {TTL_ENUM}, and RR of at least {MIN_RR}. Use flat when the "
        "evidence or risk geometry is insufficient."
    )


def judge_system_prompt(asset: str | None = None) -> str:
    """Return a deliberately basic public trade-critic prompt."""
    asset_line = _asset_prompt_line(asset)
    prefix = f"{asset_line}\n" if asset_line else ""
    return (
        f"{prefix}You are a trading-risk judge. Evaluate the supplied proposals using "
        "only the supplied market evidence. Verify direction, entry, stop, target, "
        f"ttl_minutes, and RR >= {MIN_RR}. Return JSON matching the provided schema. "
        "Choose the best valid proposal or return flat when none passes."
    )


def code_sniper_judge_system_prompt(asset: str | None = None) -> str:
    """Return the basic public code-monitor contract used by the judge."""
    asset_line = _asset_prompt_line(asset)
    prefix = f"{asset_line}\n" if asset_line else ""
    return (
        f"{prefix}You are a trading-risk judge that returns JSON containing a small "
        "entry-monitor function. Use only supplied closed candles. The code contract is:\n"
        "def run(candles_5, now):\n"
        "    # no imports, I/O, network access, or persistent state; use pass only when needed\n"
        "    return {'direction':'buy|sell|hold','entry':None,'stop_loss':None,"
        "'take_profit_1':None,'take_profit_2':None,'confidence':0.0,"
        "'rationale':'brief evidence'}\n"
        f"The response must include ttl_minutes from {TTL_ENUM}, metadata, confidence, "
        f"and code. Require RR >= {MIN_RR}; return a hold stub and flat metadata when "
        "the evidence or risk geometry is insufficient."
    )


def _htf_section(htf_context: dict | None) -> str:
    if not htf_context:
        return ""
    return f"\n\nHTF_CONTEXT (higher-timeframe computed context): {_dumps(htf_context)}"


def _stop_section(stop_hint: str | None) -> str:
    return f"\n\nSTOP_GUIDANCE: {stop_hint}" if stop_hint else ""


def _recent_trades_section(recent_trades: list[RecentTradeView] | None) -> str:
    if not recent_trades:
        return ""
    return f"\n\nRECENT_TRADES (newest first): {_dumps(recent_trades)}"


def build_expert_user_prompt(
    c5_superset: list,
    c15_superset: list,
    asset: str | None = None,
    htf_context: dict | None = None,
    stop_hint: str | None = None,
    recent_trades: list[RecentTradeView] | None = None,
) -> str:
    """Serialize market inputs for the expert."""
    asset_line = _asset_prompt_line(asset)
    prefix = f"{asset_line}\n" if asset_line else ""
    return (
        f"{prefix}Market data:\n\n"
        f"5MIN ({len(c5_superset)}): {_dumps(c5_superset)}\n\n"
        f"15MIN ({len(c15_superset)}): {_dumps(c15_superset)}"
        f"{_htf_section(htf_context)}{_stop_section(stop_hint)}"
        f"{_recent_trades_section(recent_trades)}\n\n"
        "TASK: Analyze the supplied evidence and return the required JSON response."
    )


def build_judge_user_prompt(
    proposals: list,
    advanced_context: dict,
    recent_candles_5m: list | None = None,
    recent_candles_15m: list | None = None,
    use_code_sniper: bool = True,
    asset: str | None = None,
    htf_context: dict | None = None,
    stop_hint: str | None = None,
    recent_trades: list[RecentTradeView] | None = None,
) -> str:
    """Serialize judge inputs while preserving the public schema contract."""
    asset_line = _asset_prompt_line(asset)
    prefix = f"{asset_line}\n" if asset_line else ""
    c5 = recent_candles_5m or []
    c15 = recent_candles_15m or []
    action = (
        "Return JSON with code, metadata, ttl_minutes, and a seven-field run() result: "
        "direction, entry, stop_loss, take_profit_1, take_profit_2, confidence, rationale."
        if use_code_sniper
        else "Return JSON with buy, sell, or flat plus entry, stop, target, ttl_minutes, confidence, and rationale."
    )
    return (
        f"{prefix}PROPOSALS: {_dumps(proposals)}\n"
        f"CONTEXT: {_dumps(advanced_context)}"
        f"\n\n5MIN ({len(c5)}): {_dumps(c5)}"
        f"\n\n15MIN ({len(c15)}): {_dumps(c15)}"
        f"{_htf_section(htf_context)}{_stop_section(stop_hint)}"
        f"{_recent_trades_section(recent_trades)}\n\nTASK: {action}"
    )


__all__ = [
    "unified_expert_system_prompt",
    "judge_system_prompt",
    "code_sniper_judge_system_prompt",
    "build_expert_user_prompt",
    "build_judge_user_prompt",
]

