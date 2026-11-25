"""Amazon Bedrock client – drop-in alternative to gpt_client.complete().

Uses boto3's ``bedrock-runtime`` Converse API.  The public surface is a single
``complete()`` coroutine whose signature and return contract are **identical**
to ``gpt_client.complete()`` so callers can switch providers without any code
changes.

API shape mirrors the working AWS CLI example:
  aws bedrock-runtime converse \\
    --model-id arn:aws:bedrock:eu-north-1:713939170911:inference-profile/eu.anthropic.claude-sonnet-4-6 \\
    --messages '[{"role":"user","content":[{"text":"..."}]}]' \\
    --inference-config '{"maxTokens":32000,"stopSequences":[]}' \\
    --additional-model-request-fields '{"top_k":250}' \\
    --region eu-north-1

Authentication is resolved by boto3's standard credential chain:
  1. Env vars: AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY (+ optional AWS_SESSION_TOKEN)
  2. ~/.aws/credentials profile
  3. EC2/ECS/Lambda instance role
"""

import json
import time
import asyncio
import re
from typing import Optional, Dict, Any

from .config import (
    AWS_ACCESS_KEY_ID,
    AWS_SECRET_ACCESS_KEY,
    AWS_SESSION_TOKEN,
    AWS_REGION,
    BEDROCK_CLAUDE_SONNET_46,
)
from .logger import get_logger
from .ai_accounting import APICall

from .json_extract import parse_model_json

logger = get_logger(__name__)

# Module-level credential cache — overridable at runtime via set_credentials()
_access_key   = AWS_ACCESS_KEY_ID
_secret_key   = AWS_SECRET_ACCESS_KEY
_session_token = AWS_SESSION_TOKEN
_region       = AWS_REGION


def set_credentials(
    access_key: str,
    secret_key: str,
    session_token: str = "",
    region: str = "eu-north-1",
) -> None:
    """Override AWS credentials at runtime (useful for tests)."""
    global _access_key, _secret_key, _session_token, _region
    _access_key    = access_key
    _secret_key    = secret_key
    _session_token = session_token
    _region        = region


def _make_boto3_client():
    """Return a boto3 bedrock-runtime client for the configured region."""
    import boto3

    kwargs: Dict[str, Any] = {"region_name": _region or "eu-north-1"}
    if _access_key:
        kwargs["aws_access_key_id"] = _access_key
    if _secret_key:
        kwargs["aws_secret_access_key"] = _secret_key
    if _session_token:
        kwargs["aws_session_token"] = _session_token

    return boto3.client("bedrock-runtime", **kwargs)


def _build_json_schema_instruction(response_format: Dict[str, Any]) -> str:
    """Convert OpenAI-style response_format into a system-prompt JSON instruction.

    Bedrock's Converse API has no native structured-output support, so we
    replicate the same injection strategy used by anthropic_client.
    """
    if not response_format:
        return ""

    schema = response_format
    if "json_schema" in response_format:
        inner  = response_format["json_schema"]
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
    model: str = BEDROCK_CLAUDE_SONNET_46,
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
    """Call the Amazon Bedrock Converse API and return parsed JSON or raw text.

    The signature and return semantics are intentionally identical to
    ``gpt_client.complete()`` so callers can swap providers without changes.

    boto3's ``converse()`` is synchronous; we run it in a thread pool via
    ``asyncio.to_thread()`` so the event loop stays unblocked during the call.
    """
    start_time = time.monotonic()
    response_content = None
    response_type    = "error"
    error_message    = None
    tokens_used      = None
    result           = None

    # ── Build system prompt + user message ────────────────────────────
    if system_prompt and user_prompt:
        effective_system = system_prompt
        user_text        = user_prompt
        combined_prompt  = f"System: {system_prompt}\n\nUser: {user_prompt}"
    else:
        effective_system = ""
        user_text        = prompt
        combined_prompt  = prompt

    # Inject JSON schema into system prompt when structured output is requested
    if response_format:
        json_suffix      = _build_json_schema_instruction(response_format)
        effective_system = (effective_system + json_suffix) if effective_system else json_suffix.strip()

    # ── Build the converse() kwargs, matching the working CLI example ──
    #
    #   --inference-config '{"maxTokens":32000,"stopSequences":[]}'
    #   --additional-model-request-fields '{"top_k":250}'
    #
    converse_kwargs: Dict[str, Any] = {
        "modelId": model,
        "messages": [
            {"role": "user", "content": [{"text": user_text}]}
        ],
        "inferenceConfig": {
            "maxTokens":    32000,
            "temperature":  float(temperature),
            "topP":         float(top_p),
            "stopSequences": [],
        },
        # top_k must go in additionalModelRequestFields for Bedrock Claude models
        "additionalModelRequestFields": {
            "top_k": 250,
        },
    }

    if effective_system:
        converse_kwargs["system"] = [{"text": effective_system}]

    try:
        client = _make_boto3_client()

        last_exception = None
        response       = None

        for attempt in range(retries + 1):
            try:
                logger.info(
                    f"Starting Bedrock API call (attempt {attempt + 1}/{retries + 1})",
                    extra={
                        "model":      model,
                        "asset":      asset,
                        "agent_id":   agent_id,
                        "gpt_mode":   gpt_mode,
                        "attempt":    attempt + 1,
                        "region":     _region,
                    },
                )

                # boto3 is synchronous — run in a thread so we don't block the loop
                with APICall("bedrock", model, asset=asset, agent_id=agent_id) as usage_call:
                    response = await asyncio.wait_for(
                        asyncio.to_thread(client.converse, **converse_kwargs),
                        timeout=timeout_seconds,
                    )
                    usage_call.record(response.get("usage"), response_id=response.get("ResponseMetadata", {}).get("RequestId"))
                break  # success

            except asyncio.TimeoutError as e:
                last_exception = e
                if attempt < retries:
                    import random
                    wait_time = random.uniform(1.0, 3.0)
                    logger.warning(
                        f"Bedrock call timeout on attempt {attempt + 1}, "
                        f"retrying in {wait_time:.1f}s",
                    )
                    await asyncio.sleep(wait_time)
                    continue
                logger.error(f"Bedrock call failed after {retries + 1} attempts (timeout)")
                raise

            except Exception as e:
                last_exception = e
                if attempt < retries:
                    import random
                    wait_time = random.uniform(1.0, 3.0)
                    logger.warning(
                        f"Bedrock call error on attempt {attempt + 1}: {e}, "
                        f"retrying in {wait_time:.1f}s",
                    )
                    await asyncio.sleep(wait_time)
                    continue
                raise

        if response is None:
            raise ValueError("No valid response received from Bedrock API")

        # ── Extract text from Converse response ────────────────────────
        # Shape: response["output"]["message"]["content"][0]["text"]
        content     = response["output"]["message"]["content"][0]["text"]
        usage       = response.get("usage", {})
        tokens_used = usage.get("inputTokens", 0) + usage.get("outputTokens", 0)

        response_content = content
        response_type    = "text"

        logger.info("Bedrock call succeeded", extra={"tokens": tokens_used, "model": model})

        # ── Parse JSON — same 3-pass strategy as every other client ───
        # Shared extractor (packages/core/json_extract). The old 3-pass chain ended
        # in a GREEDY regex spanning the first '{' to the last '}', so a response
        # containing an object, then prose, then a corrected object failed every
        # pass and was handed back as raw text.
        parsed = parse_model_json(content)
        if parsed is None:
            result = content
            logger.warning(f"Bedrock response not parseable as JSON - raw text ({len(content)} chars)")
        else:
            response_type = "json"
            result        = parsed

    except Exception as e:
        error_message = f"{type(e).__name__}: {e}"
        logger.error(f"Bedrock call failed ({type(e).__name__}): {e}", exc_info=True)
        result = None

    # ── Latency ────────────────────────────────────────────────────────
    latency_ms = (time.monotonic() - start_time) * 1000.0

    # ── Fire-and-forget MongoDB storage (mirrors every other client) ──
    if store_response and asset and agent_id and prompt_hash:
        async def _store_response_background():
            try:
                from packages.db.repositories import gpt_responses as gpt_repo

                parsed_decision = None
                confidence      = None
                pattern         = None
                direction       = None
                level           = None
                stop_loss       = None
                take_profit     = None
                quantity        = None

                if response_type == "json" and isinstance(result, dict):
                    parsed_decision  = result
                    confidence_raw   = result.get("confidence")
                    if confidence_raw == "high":
                        confidence = 0.8
                    elif confidence_raw == "medium":
                        confidence = 0.6
                    elif confidence_raw == "low":
                        confidence = 0.4
                    elif isinstance(confidence_raw, (int, float)):
                        confidence = float(confidence_raw)
                    pattern     = result.get("pattern")    or result.get("condition")
                    direction   = result.get("direction")
                    level       = result.get("level")      or result.get("entry_price")
                    stop_loss   = (result.get("stop")      or result.get("stop_loss")
                                   or result.get("stoploss"))
                    take_profit = result.get("target")     or result.get("take_profit")
                    quantity    = result.get("quantity")   or result.get("size")

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
                logger.info("Stored Bedrock response in MongoDB")
            except Exception as exc:
                logger.error(f"Failed to store Bedrock response: {exc}", exc_info=True)

        asyncio.create_task(_store_response_background())

    return result
