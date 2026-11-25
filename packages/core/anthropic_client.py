"""Anthropic API client – drop-in alternative to gpt_client.complete().

Uses the Anthropic Python SDK (``anthropic``) to call Claude models.  The
public surface is a single ``complete()`` coroutine whose signature and
return contract are **identical** to ``gpt_client.complete()`` so callers
can switch providers without any code changes.
"""

import json, time, asyncio, re
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from . import anthropic_sonnet_55
from .config import (
    ANTHROPIC_API_KEY, CLAUDE_SONNET, CLAUDE_SONNET_55,
    ANTHROPIC_MAX_RETRIES, ANTHROPIC_TIMEOUT_SECONDS,
    ANTHROPIC_CONNECT_TIMEOUT_SECONDS,
    PROMPT_CACHE_ENABLED, PROMPT_CACHE_TTL,
)
from .logger import get_logger
from .ai_accounting import APICall
from .prompt_cache import build_policy, apply_cache_control, read_cache_usage

from .json_extract import extract_json_objects, parse_model_json

logger = get_logger(__name__)

_api_key = ANTHROPIC_API_KEY

# Resolved once at import. Stateless and immutable, so it is safe to share
# across the event loop and costs nothing per call.
_CACHE_POLICY = build_policy(PROMPT_CACHE_ENABLED, PROMPT_CACHE_TTL)

# Process-wide cached client.  Re-using one ``AsyncAnthropic`` (and therefore a
# single httpx connection pool with keep-alive) avoids a fresh TLS handshake on
# every call and prevents the connection-pool leak caused by building a new
# client per request.  Rebuilt only when the API key changes.
_client = None
_client_key: Optional[str] = None


def _get_async_client(key: str):
    """Return the cached :class:`AsyncAnthropic`, building it if needed.

    The client is configured with the SDK's native retry + timeout handling so
    transient ``APIConnectionError`` / ``429`` / ``529`` failures recover inside
    a single call (exponential backoff + jitter, honouring ``retry-after``).
    """
    global _client, _client_key
    if _client is None or _client_key != key:
        from anthropic import AsyncAnthropic
        import httpx  # bundled as an anthropic SDK dependency

        _client = AsyncAnthropic(
            api_key=key,
            max_retries=ANTHROPIC_MAX_RETRIES,
            timeout=httpx.Timeout(
                ANTHROPIC_TIMEOUT_SECONDS,
                connect=ANTHROPIC_CONNECT_TIMEOUT_SECONDS,
            ),
        )
        _client_key = key
    return _client


def set_api_key(key: str) -> None:
    """Override the Anthropic API key at runtime (useful for tests)."""
    global _api_key, _client, _client_key
    _api_key = key
    # Invalidate the cached client so the next call rebuilds with the new key.
    _client = None
    _client_key = None


def _build_json_schema_instruction(response_format: Dict[str, Any]) -> str:
    """Extract a JSON schema from the OpenAI-style envelope and return a
    system-prompt suffix instructing the model to respond with valid JSON.

    The trading prompts share this schema instruction across Claude models.
    """
    if not response_format:
        return ""

    schema = response_format
    if "json_schema" in response_format:
        inner = response_format["json_schema"]
        schema = inner.get("schema", inner)
    elif "properties" not in response_format and response_format.get("type") not in (
        "object", "array", "string", "number", "integer", "boolean",
    ):
        return ""

    compact = json.dumps(schema, separators=(",", ":"))
    return (
        "\n\nYou MUST respond with valid JSON only. "
        "No markdown code fences. No explanations. "
        f"JSON schema: {compact}"
    )


async def complete(
    prompt: str,
    model: str = CLAUDE_SONNET,
    store_response: bool = True,
    asset: Optional[str] = None,
    agent_id: Optional[str] = None,
    assistant_id: Optional[str] = None,
    prompt_hash: Optional[str] = None,
    broker_type: Optional[str] = None,
    gpt_mode: Optional[str] = None,
    response_format: Optional[Dict[str, Any]] = None,
    timeout_seconds: int = 60,
    retries: int = 1,
    system_prompt: Optional[str] = None,
    user_prompt: Optional[str] = None,
    temperature: float = 0.2,
    top_p: float = 0.9,
    max_tokens: int | None = None,
    effort: anthropic_sonnet_55.ReasoningEffort | None = None,
) -> dict | str | None:
    """Call the Anthropic Messages API and return parsed JSON or raw text.

    The signature and return semantics are intentionally identical to
    ``gpt_client.complete()`` so the two are interchangeable.
    """
    key = _api_key
    if not key:
        raise ValueError("missing Anthropic API key – set ANTHROPIC_API_KEY env var")

    start_time = time.monotonic()
    response_content = None
    response_type = "error"
    error_message = None
    tokens_used = None
    result = None
    combined_prompt = f"System: {system_prompt}\n\nUser: {user_prompt}" if system_prompt and user_prompt else prompt

    try:
        client = _get_async_client(key)

        # ── Build messages + system prompt ─────────────────────────
        messages = []
        effective_system = ""

        if system_prompt and user_prompt:
            effective_system = system_prompt
            messages = [{"role": "user", "content": user_prompt}]
        else:
            messages = [{"role": "user", "content": prompt}]

        # Inject JSON schema instruction into system prompt
        if response_format:
            json_suffix = _build_json_schema_instruction(response_format)
            effective_system = (effective_system + json_suffix) if effective_system else json_suffix.strip()

        # ── Build API kwargs ───────────────────────────────────────
        kwargs: Dict[str, Any] = {
            "model": model,
            "messages": messages,
        }
        if model == CLAUDE_SONNET_55:
            kwargs.update(anthropic_sonnet_55.request_options(max_tokens, effort))
        else:
            # The legacy Claude API path uses temperature alone for sampling.
            kwargs.update(max_tokens=4096, temperature=float(temperature))
        if effective_system:
            kwargs["system"] = effective_system

        # ── Prompt cache breakpoint ────────────────────────────────
        # Total function: on any failure this returns ``kwargs`` untouched and
        # logs a WARNING, so a caching problem can never cost a cycle. Pure
        # in-memory work — no I/O, no added latency on the hot path.
        kwargs = apply_cache_control(
            kwargs,
            _CACHE_POLICY,
            context={"asset": asset, "agent_id": agent_id, "gpt_mode": gpt_mode},
        )

        # ── Retry loop ─────────────────────────────────────────────
        last_exception = None
        response = None

        for attempt in range(retries + 1):
            try:
                logger.info(
                    f"Starting Anthropic API call (attempt {attempt + 1}/{retries + 1})",
                    extra={
                        "model": model,
                        "asset": asset,
                        "agent_id": agent_id,
                        "gpt_mode": gpt_mode,
                        "max_tokens": kwargs["max_tokens"],
                        "effort": kwargs.get("extra_body", {}).get("output_config", {}).get("effort"),
                        "attempt": attempt + 1,
                        # SDK retries transient connection/429/529 errors beneath
                        # this app-level attempt, so the effective attempt budget
                        # is (retries + 1) * (sdk_max_retries + 1).
                        "sdk_max_retries": ANTHROPIC_MAX_RETRIES,
                    },
                )

                # ``with_options`` reuses the shared connection pool while
                # honouring a per-call timeout override.  The outer
                # ``asyncio.wait_for`` is a hard ceiling — generous enough to let
                # the SDK's internal retries complete, but guaranteeing the
                # coroutine can never hang the event loop (this project has a
                # history of event-loop freezes).
                per_call_client = (
                    client.with_options(timeout=float(timeout_seconds))
                    if timeout_seconds
                    else client
                )
                overall_deadline = max(float(timeout_seconds) + 60.0, 120.0)
                with APICall("anthropic", model, asset=asset, agent_id=agent_id, cache_ttl=getattr(_CACHE_POLICY, "_ttl", None)) as usage_call:
                    response = await asyncio.wait_for(
                        per_call_client.messages.create(**kwargs),
                        timeout=overall_deadline,
                    )
                    usage_call.record(getattr(response, "usage", None), response_id=getattr(response, "id", None), response_model=getattr(response, "model", None))
                break  # success
            except asyncio.TimeoutError as e:
                last_exception = e
                if attempt < retries:
                    import random
                    wait_time = random.uniform(1.0, 3.0)
                    logger.warning(
                        f"Anthropic call timeout on attempt {attempt + 1}, "
                        f"retrying in {wait_time:.1f}s",
                    )
                    await asyncio.sleep(wait_time)
                    continue
                logger.error(
                    f"Anthropic call failed after {retries + 1} attempts (timeout)"
                )
                raise
            except Exception as e:
                last_exception = e
                if attempt < retries:
                    import random
                    wait_time = random.uniform(1.0, 3.0)
                    logger.warning(
                        f"Anthropic call error on attempt {attempt + 1}: {e}, "
                        f"retrying in {wait_time:.1f}s",
                    )
                    await asyncio.sleep(wait_time)
                    continue
                raise

        if response is None:
            raise ValueError("No valid response received from Anthropic API")

        # ── Extract text content ───────────────────────────────────
        tokens_used = response.usage.input_tokens + response.usage.output_tokens
        if model == CLAUDE_SONNET_55:
            content = anthropic_sonnet_55.response_text(response)
        else:
            content = response.content[0].text

        response_content = content
        response_type = "text"

        # Cache counters ride along on every success line. A
        # ``cache_read_input_tokens`` stuck at 0 across repeated calls is the
        # only reliable signal that something upstream is invalidating the
        # prefix — watch this, not the invoice.
        logger.info(
            "Anthropic call succeeded",
            extra={"tokens": tokens_used, **read_cache_usage(response)},
        )

        # ── Parse JSON (shared extractor — see packages/core/json_extract) ──
        # Was a 3-pass chain ending in a GREEDY `re.search(r"\{.*\}", DOTALL)`,
        # which spans from the first `{` to the last `}` and therefore fails
        # whenever a model emits an object, reconsiders in prose, then emits a
        # corrected object. Measured 2026-08-07: ~9% of trend-expert calls,
        # each one discarding a whole cycle behind the misleading error
        # "did not return JSON, got: <class 'str'>".
        parsed = parse_model_json(content)
        if parsed is None:
            result = content
            stop_reason = getattr(response, "stop_reason", None)
            logger.warning(
                "Anthropic response could not be parsed as JSON — returning raw text "
                f"(stop_reason={stop_reason}, chars={len(content)}, "
                f"objects_found={len(list(extract_json_objects(content)))})"
            )
        else:
            response_type = "json"
            result = parsed

    except Exception as e:
        error_message = f"{type(e).__name__}: {e}"
        logger.error(f"Anthropic call failed ({type(e).__name__}): {e}", exc_info=True)
        result = None

    # ── Latency ────────────────────────────────────────────────────
    latency_ms = (time.monotonic() - start_time) * 1000.0

    # ── Fire-and-forget MongoDB storage (same as gpt_client) ──────
    if store_response and asset and agent_id and prompt_hash:
        async def _store_response_background():
            try:
                from packages.db.repositories import gpt_responses as gpt_repo

                parsed_decision = None
                confidence = None
                pattern = None
                direction = None
                level = None
                stop_loss = None
                take_profit = None
                quantity = None

                if response_type == "json" and isinstance(result, dict):
                    parsed_decision = result
                    confidence_raw = result.get("confidence")
                    if confidence_raw == "high":
                        confidence = 0.8
                    elif confidence_raw == "medium":
                        confidence = 0.6
                    elif confidence_raw == "low":
                        confidence = 0.4
                    elif isinstance(confidence_raw, (int, float)):
                        confidence = float(confidence_raw)
                    pattern = result.get("pattern") or result.get("condition")
                    direction = result.get("direction")
                    level = result.get("level") or result.get("entry_price")
                    stop_loss = (
                        result.get("stop")
                        or result.get("stop_loss")
                        or result.get("stoploss")
                    )
                    take_profit = result.get("target") or result.get("take_profit")
                    quantity = result.get("quantity") or result.get("size")

                await gpt_repo.store_gpt_response(
                    asset=asset,
                    symbol=asset,
                    agent_id=agent_id,
                    model=model,
                    prompt_hash=prompt_hash,
                    prompt_content=combined_prompt,
                    response_content=response_content or "",
                    response_type=response_type,
                    latency_ms=latency_ms,
                    tokens_used=tokens_used,
                    error_message=error_message,
                    parsed_decision=parsed_decision,
                    confidence=confidence,
                    pattern=pattern,
                    direction=direction,
                    level=level,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    quantity=quantity,
                    broker_type=broker_type,
                    gpt_mode=gpt_mode,
                )
                logger.info("Stored Anthropic response in MongoDB")
            except Exception as exc:
                logger.error(f"Failed to store Anthropic response: {exc}", exc_info=True)

        asyncio.create_task(_store_response_background())

    return result
