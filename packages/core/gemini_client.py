"""Gemini API client – drop-in alternative to gpt_client.complete().

Uses the Google Gen AI SDK (``google-genai``) to call Gemini models.  The
public surface is a single ``complete()`` coroutine whose signature and
return contract are **identical** to ``gpt_client.complete()`` so callers
can switch providers without any code changes.
"""

import json, time, asyncio, re
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from .config import GEMINI_API_KEY, GEMINI_FLASH
from .logger import get_logger
from .ai_accounting import APICall

from .json_extract import parse_model_json

logger = get_logger(__name__)

# Module-level key cache (mirrors gpt_client pattern)
_api_key = GEMINI_API_KEY


def set_api_key(key: str) -> None:
    """Override the Gemini API key at runtime (useful for tests)."""
    global _api_key
    _api_key = key


def _openai_schema_to_gemini(response_format: Dict[str, Any]) -> dict:
    """Convert OpenAI ``response_format`` (json_schema) to a plain JSON Schema
    dict that the Gemini ``response_json_schema`` parameter accepts.

    OpenAI wraps the real schema inside::

        {"type": "json_schema", "json_schema": {"name": "...", "schema": {...}}}

    Gemini just needs the inner ``{...}`` schema object.
    """
    if not response_format:
        return {}

    # Already a plain schema (has "type"/"properties" at top level)
    if "properties" in response_format or response_format.get("type") in (
        "object", "array", "string", "number", "integer", "boolean",
    ):
        return _strip_unsupported_keys(response_format)

    # OpenAI structured-output envelope
    inner = response_format.get("json_schema", {})
    schema = inner.get("schema", inner)

    # The schema may have the ``name`` key at the top level alongside
    # ``type``, ``properties``, etc.  Gemini doesn't accept ``name`` at the
    # schema root so strip it.
    return _strip_unsupported_keys(schema)


def _strip_unsupported_keys(schema: dict) -> dict:
    """Recursively remove keys Gemini's structured-output doesn't support."""
    UNSUPPORTED = {"name", "strict", "additionalProperties"}
    cleaned: dict = {}
    for k, v in schema.items():
        if k in UNSUPPORTED:
            continue
        if isinstance(v, dict):
            cleaned[k] = _strip_unsupported_keys(v)
        elif isinstance(v, list):
            cleaned[k] = [
                _strip_unsupported_keys(item) if isinstance(item, dict) else item
                for item in v
            ]
        else:
            cleaned[k] = v
    return cleaned


async def complete(
    prompt: str,
    model: str = GEMINI_FLASH,
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
) -> dict | str | None:
    """Call the Google Gemini API and return parsed JSON or raw text.

    The signature and return semantics are intentionally identical to
    ``gpt_client.complete()`` so the two are interchangeable.
    """
    from google import genai

    key = _api_key
    if not key:
        raise ValueError("missing Gemini API key – set GEMINI_API_KEY env var")

    start_time = time.monotonic()
    response_content = None
    response_type = "error"
    error_message = None
    tokens_used = None
    result = None

    try:
        client = genai.Client(api_key=key)

        # ── Build contents string ──────────────────────────────────
        if system_prompt and user_prompt:
            combined_prompt = f"System: {system_prompt}\n\nUser: {user_prompt}"
        else:
            combined_prompt = prompt

        # ── Build generation config ────────────────────────────────
        gen_config: Dict[str, Any] = {
            "temperature": float(temperature),
            "top_p": float(top_p),
        }

        if response_format:
            gen_config["response_mime_type"] = "application/json"
            gemini_schema = _openai_schema_to_gemini(response_format)
            if gemini_schema:
                gen_config["response_json_schema"] = gemini_schema

        # ── Build contents ─────────────────────────────────────────
        contents = []
        if system_prompt and user_prompt:
            # Gemini doesn't have a dedicated system role in contents;
            # pass system_instruction separately.
            gen_config["system_instruction"] = system_prompt
            contents = user_prompt
        else:
            contents = prompt

        # ── Retry loop ─────────────────────────────────────────────
        last_exception = None
        response = None

        for attempt in range(retries + 1):
            try:
                logger.info(
                    f"Starting Gemini API call (attempt {attempt + 1}/{retries + 1})",
                    extra={
                        "model": model,
                        "asset": asset,
                        "agent_id": agent_id,
                        "gpt_mode": gpt_mode,
                        "attempt": attempt + 1,
                    },
                )

                # Run the synchronous SDK call in a thread to stay async
                with APICall("gemini", model, asset=asset, agent_id=agent_id) as usage_call:
                    response = await asyncio.wait_for(
                        asyncio.to_thread(
                            client.models.generate_content,
                            model=model,
                            contents=contents,
                            config=gen_config,
                        ),
                        timeout=timeout_seconds,
                    )
                    usage_call.record(getattr(response, "usage_metadata", None), response_id=getattr(response, "response_id", None), response_model=getattr(response, "model_version", None))
                break  # success
            except asyncio.TimeoutError as e:
                last_exception = e
                if attempt < retries:
                    import random
                    wait_time = random.uniform(1.0, 3.0)
                    logger.warning(
                        f"Gemini call timeout on attempt {attempt + 1}, "
                        f"retrying in {wait_time:.1f}s",
                    )
                    await asyncio.sleep(wait_time)
                    continue
                logger.error(
                    f"Gemini call failed after {retries + 1} attempts (timeout)"
                )
                raise
            except Exception as e:
                last_exception = e
                if attempt < retries:
                    import random
                    wait_time = random.uniform(1.0, 3.0)
                    logger.warning(
                        f"Gemini call error on attempt {attempt + 1}: {e}, "
                        f"retrying in {wait_time:.1f}s",
                    )
                    await asyncio.sleep(wait_time)
                    continue
                raise

        if response is None:
            raise ValueError("No valid response received from Gemini API")

        # ── Extract text content ───────────────────────────────────
        content = response.text
        tokens_used = 0
        if response.usage_metadata:
            tokens_used = (
                (response.usage_metadata.prompt_token_count or 0)
                + (response.usage_metadata.candidates_token_count or 0)
            )

        response_content = content
        response_type = "text"

        logger.info("Gemini call succeeded", extra={"tokens": tokens_used})

        # ── Parse JSON (same 3-pass strategy as gpt_client) ────────
        # Shared extractor (packages/core/json_extract). The old 3-pass chain ended
        # in a GREEDY regex spanning the first '{' to the last '}', so a response
        # containing an object, then prose, then a corrected object failed every
        # pass and was handed back as raw text.
        parsed = parse_model_json(content)
        if parsed is None:
            result = content
            logger.warning(f"Gemini response not parseable as JSON - raw text ({len(content)} chars)")
        else:
            response_type = "json"
            result = parsed

    except Exception as e:
        error_message = f"{type(e).__name__}: {e}"
        logger.error(f"Gemini call failed ({type(e).__name__}): {e}", exc_info=True)
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
                logger.info("Stored Gemini response in MongoDB")
            except Exception as exc:
                logger.error(f"Failed to store Gemini response: {exc}", exc_info=True)

        asyncio.create_task(_store_response_background())

    return result
