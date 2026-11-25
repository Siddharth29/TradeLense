"""Response-usage accounting; one total for a decision's expert and judge calls.

Amounts are USD usage charges at the saved rate card, not invoice/tax totals.
Unknown usage/rates (including timed-out requests) must never become free calls.
"""
from __future__ import annotations

from contextvars import ContextVar
from decimal import Decimal
from functools import wraps
from inspect import signature
import json
import os
from types import SimpleNamespace
from uuid import uuid4

from packages.db.persistence_gate import log_event

PRICING_VERSION = "2026-11-17"
# USD per million tokens: input, output, cache read, 5m write, 1h write.
# Sources and billing assumptions: docs/engineering/trade-cost-accounting.md.
RATES = {
    "openai:gpt-4o-mini": ("0.15", "0.60", "0.075", "0", "0"),
    "openai:gpt-4o": ("2.50", "10", "1.25", "0", "0"),
    "openai:gpt-3.5-turbo": ("0.50", "1.50", "0", "0", "0"),
    "anthropic:claude-sonnet-5-5": ("2", "10", "0.10", "2.50", "4"),
    "anthropic:claude-sonnet-5": ("2", "10", "0.20", "2.50", "4"),
    "anthropic:claude-sonnet-4-6": ("3", "15", "0.30", "3.75", "6"),
    "anthropic:claude-haiku-4-5-20251001": ("1", "5", "0.10", "1.25", "2"),
    "gemini:gemini-2.5-flash": ("0.30", "2.50", "0.03", "0", "0"),
}
_current: ContextVar = ContextVar("decision_ai_accounting", default=None)


def _mapping(value):
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        result = value.model_dump()
        return result if isinstance(result, dict) else {}
    return vars(value) if isinstance(value, SimpleNamespace) else {}


def _usage_snapshot(value, depth=0):
    """Keep only JSON usage data; never traverse arbitrary SDK object graphs."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if depth > 6:
        return None
    if isinstance(value, dict):
        return {str(key): _usage_snapshot(item, depth+1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_usage_snapshot(item, depth+1) for item in value]
    return None


def _count(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Missing or invalid provider token counter")
    return value


def price_usage(provider, model, raw, cache_ttl=None):
    """Price actual counters. Retain raw usage and applied rates for auditing."""
    usage = _mapping(raw)
    if not usage:
        raise ValueError("Provider did not return usage")
    read = write5 = write1 = 0
    if provider == "openai":
        total_in = _count(usage.get("prompt_tokens"))
        out = _count(usage.get("completion_tokens"))  # includes reasoning
        read = _count(_mapping(usage.get("prompt_tokens_details")).get("cached_tokens", 0))
        inp = total_in - read
    elif provider == "anthropic":
        inp, out = _count(usage.get("input_tokens")), _count(usage.get("output_tokens"))
        read = _count(usage.get("cache_read_input_tokens", 0) or 0)
        writes = _count(usage.get("cache_creation_input_tokens", 0) or 0)
        split = _mapping(usage.get("cache_creation"))
        if split:
            write5 = _count(split.get("ephemeral_5m_input_tokens", 0) or 0)
            write1 = _count(split.get("ephemeral_1h_input_tokens", 0) or 0)
            if write5 + write1 != writes:
                raise ValueError("Cache creation counters do not reconcile")
        elif writes:
            if cache_ttl not in ("5m", "1h"):
                raise ValueError("Cache write TTL unavailable")
            write5, write1 = (writes, 0) if cache_ttl == "5m" else (0, writes)
    elif provider == "gemini":
        total_in = _count(usage.get("prompt_token_count"))
        out = _count(usage.get("candidates_token_count", 0) or 0)
        out += _count(usage.get("thoughts_token_count", 0) or 0)
        read = _count(usage.get("cached_content_token_count", 0) or 0)
        inp = total_in - read
        if usage.get("tool_use_prompt_token_count"):
            raise ValueError("Tool pricing is not configured")
    elif provider == "bedrock":
        inp, out = _count(usage.get("inputTokens")), _count(usage.get("outputTokens"))
        read = _count(usage.get("cacheReadInputTokens", 0) or 0)
        writes = _count(usage.get("cacheWriteInputTokens", 0) or 0)
        if writes:
            raise ValueError("Bedrock cache write TTL pricing requires an explicit integration")
    else:
        raise ValueError(f"Unsupported provider: {provider}")
    if inp < 0:
        raise ValueError("Cached tokens exceed prompt tokens")
    key = f"{provider}:{model}"
    overrides = json.loads(os.getenv("AI_COST_RATES_JSON", "{}"))
    if not isinstance(overrides, dict):
        raise ValueError("AI_COST_RATES_JSON must be an object")
    rates = overrides.get(key, RATES.get(key))
    # The repository's EU regional Sonnet 4.6 profile has a 10% premium.
    if rates is None and provider == "bedrock" and model.endswith("/eu.anthropic.claude-sonnet-4-6"):
        rates = ("3.30", "16.50", "0.33", "4.125", "6.60")
    if rates is None or len(rates) != 5:
        raise ValueError(f"No verified rate card for {key}")
    decimals = [Decimal(str(rate)) for rate in rates]
    if any(not rate.is_finite() or rate < 0 for rate in decimals):
        raise ValueError("Invalid rate card")
    counters = [inp, out, read, write5, write1]
    cost = sum((Decimal(n) * rate for n, rate in zip(counters, decimals)), Decimal(0)) / 1_000_000
    return {
        "cost_usd": float(cost), "cost_decimal_usd": str(cost),
        "token_counts": dict(zip(("input", "output", "cache_read", "cache_write_5m", "cache_write_1h"), counters)),
        "rates_usd_per_million": [str(rate) for rate in decimals],
        "pricing_version": "configured" if key in overrides else PRICING_VERSION,
        "basis": "provider_usage_at_saved_rates",
    }


class APICall:
    """Scope one network attempt, including cancellation and ambiguous retries."""
    def __init__(self, provider, model, *, asset=None, agent_id=None, cache_ttl=None):
        self.tracker = _current.get()
        self.cache_ttl = cache_ttl
        self.recorded = False
        self.record_data = {
            "call_id": uuid4().hex, "provider": provider, "model": model,
            "asset": asset, "agent_id": agent_id,
            "cycle_id": self.tracker.cycle_id if self.tracker else None,
            "currency": "USD",
        }

    def __enter__(self):
        return self

    def record(self, raw, *, response_id=None, response_model=None):
        self.recorded = True
        self.record_data.update(provider_response_id=response_id if isinstance(response_id, str) else None,
                                response_model=response_model if isinstance(response_model, str) else None)
        try:
            self.record_data["usage"] = _usage_snapshot(_mapping(raw))
            self.record_data.update(price_usage(self.record_data["provider"], self.record_data["model"], self.record_data["usage"], self.cache_ttl))
            self.record_data["status"] = "priced"
        except (ValueError, TypeError, AttributeError, ArithmeticError) as exc:
            self.record_data.update(cost_usd=None, status="unavailable", error=str(exc))
        self._emit()

    def _emit(self):
        if self.tracker is not None:
            self.tracker.calls.append(dict(self.record_data))
        log_event("ai_api_cost", self.record_data, cycle_id=self.record_data["cycle_id"],
                  asset=self.record_data["asset"], agent_id=self.record_data["agent_id"])

    def __exit__(self, exc_type, exc, tb):
        if not self.recorded:
            self.record_data.update(cost_usd=None, status="unavailable",
                                    error="Request ended without confirmed provider usage" if exc is None else exc_type.__name__)
            self._emit()
        return False


class DecisionCosts:
    def __init__(self, cycle_id):
        self.cycle_id = cycle_id
        self.calls = []

    def __enter__(self):
        self.token = _current.set(self)
        return self

    def __exit__(self, exc_type, exc, tb):
        log_event("ai_cycle_cost", self.summary(), cycle_id=self.cycle_id)
        _current.reset(self.token)
        return False

    def summary(self):
        known = sum((Decimal(call["cost_decimal_usd"]) for call in self.calls if call.get("cost_usd") is not None), Decimal(0))
        complete = bool(self.calls) and all(call.get("cost_usd") is not None for call in self.calls)
        return {"total_usd": float(known) if complete else None,
                "total_decimal_usd": str(known) if complete else None,
                "known_subtotal_usd": float(known), "status": "priced" if complete else "unavailable",
                "currency": "USD", "scope": "decision_cycle", "cycle_id": self.cycle_id,
                "calls": self.calls}


def decision_accounting(function):
    """Keep task-local totals across parallel expert tasks and attach on failure."""
    parameters = signature(function)

    @wraps(function)
    async def wrapped(*args, **kwargs):
        bound = parameters.bind(*args, **kwargs)
        tracker = DecisionCosts(bound.arguments["cycle_id"])
        token = _current.set(tracker)
        try:
            signal, cycle = await function(*args, **kwargs)
            cycle.ai_cost = tracker.summary()
            if hasattr(signal, "metadata"):
                signal.metadata["ai_cost"] = cycle.ai_cost
            return signal, cycle
        except BaseException as exc:
            if hasattr(exc, "cycle_doc"):
                exc.cycle_doc.ai_cost = tracker.summary()
            raise
        finally:
            log_event("ai_cycle_cost", tracker.summary(), cycle_id=tracker.cycle_id)
            _current.reset(token)
    return wrapped


def trade_reason(signal):
    """Return decision evidence as text, without substituting a generic reason."""
    data = signal if isinstance(signal, dict) else signal.model_dump() if hasattr(signal, "model_dump") else {}
    meta = data.get("metadata") or {}
    for value in (data.get("rationale"), meta.get("rationale"), meta.get("reasoning"),
                  meta.get("entry_reason"), meta.get("setup_explanation")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    setup = meta.get("typed_setup") or data.get("setup") or {}
    return json.dumps(setup, sort_keys=True, default=str) if setup else None
