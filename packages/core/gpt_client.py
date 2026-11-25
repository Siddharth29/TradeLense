import json, httpx
import time
import asyncio
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from .config import (
    MODEL_4, OPENAI_API_KEY, AI_PROVIDER, GEMINI_FLASH, CLAUDE_SONNET, BEDROCK_CLAUDE_SONNET_46,
    GPT_HTTP_CONNECT_TIMEOUT, GPT_HTTP_WRITE_TIMEOUT, GPT_HTTP_POOL_TIMEOUT,
    GPT_HTTP_MAX_CONNECTIONS, GPT_HTTP_MAX_KEEPALIVE, GPT_HTTP_KEEPALIVE_EXPIRY,
)
from .logger import get_logger
from .ai_accounting import APICall

from .json_extract import parse_model_json

logger = get_logger(__name__)

# Active AI provider — can be overridden at runtime via set_provider()
_active_provider = AI_PROVIDER


def set_provider(provider: str) -> None:
    """Switch the active AI provider ('openai', 'gemini', 'anthropic', or 'bedrock')."""
    global _active_provider
    provider = provider.strip().lower()
    if provider not in ("openai", "gemini", "anthropic", "bedrock"):
        raise ValueError(f"Unsupported AI provider: {provider}")
    _active_provider = provider


def get_provider() -> str:
    """Return the currently active AI provider."""
    return _active_provider


def _is_bedrock_model(model: str) -> bool:
    """Return True if the model string looks like an Amazon Bedrock model ID.

    Accepts two formats:
    - ARN:        arn:aws:bedrock:<region>:<account>:inference-profile/...
    - Short ID:   us.amazon.nova-pro-v1:0  (contains ":" + known prefix)
    """
    # ARN format used for inference profiles (e.g. eu.anthropic.claude-sonnet-4-6)
    if model.startswith("arn:aws:bedrock:"):
        return True
    # Short-ID format: must contain ":" version suffix and a known vendor prefix
    if ":" not in model:
        return False
    bedrock_prefixes = ("us.", "eu.", "ap.", "amazon.", "meta.", "anthropic.claude-3", "cohere.", "mistral.")
    return any(model.startswith(p) for p in bedrock_prefixes)


# Cache the loaded API key so callers can override it via ``set_api_key`` in
# tests without repeatedly touching the environment.
_api_key = OPENAI_API_KEY

def set_api_key(key: str) -> None:
    """Set the OpenAI API key for this module."""
    global _api_key
    _api_key = key


# Live trading optimization: reuse a single AsyncClient across calls so
# TLS handshake + DNS resolution + TCP connection are amortised across the
# many GPT calls each cycle makes.  Each per-cycle call previously paid
# ~150-300 ms for handshake; with keep-alive it drops to ~10-20 ms.
#
# The client is loop-bound and lazily created per running event loop, so
# tests that spin up new loops (or the FastAPI app under uvicorn) each get
# their own properly-scoped instance.
_clients_by_loop: "dict[int, httpx.AsyncClient]" = {}


def _get_async_client(timeout_seconds: int) -> httpx.AsyncClient:
    """Return a long-lived httpx.AsyncClient bound to the running loop.

    Connection pool is shared across all GPT calls in the same event loop,
    enabling HTTP keep-alive and saving the TLS handshake on each call.
    """
    loop = asyncio.get_event_loop()
    loop_id = id(loop)
    client = _clients_by_loop.get(loop_id)

    desired_read_timeout = float(timeout_seconds)
    if client is not None and not client.is_closed:
        # If the existing client's read timeout is still adequate, reuse it.
        existing_read = getattr(client.timeout, "read", None)
        if existing_read is not None and existing_read >= desired_read_timeout:
            return client
        # Otherwise close and recreate with a wider timeout.
        try:
            asyncio.create_task(client.aclose())
        except Exception:  # pragma: no cover - defensive
            pass

    timeout_config = httpx.Timeout(
        connect=GPT_HTTP_CONNECT_TIMEOUT,
        read=desired_read_timeout,
        write=GPT_HTTP_WRITE_TIMEOUT,
        pool=GPT_HTTP_POOL_TIMEOUT,
    )
    limits = httpx.Limits(
        max_connections=GPT_HTTP_MAX_CONNECTIONS,
        max_keepalive_connections=GPT_HTTP_MAX_KEEPALIVE,
        keepalive_expiry=GPT_HTTP_KEEPALIVE_EXPIRY,
    )
    client = httpx.AsyncClient(timeout=timeout_config, limits=limits)
    _clients_by_loop[loop_id] = client
    return client


async def aclose_clients() -> None:
    """Close all pooled HTTP clients (call on shutdown)."""
    global _clients_by_loop
    pending = list(_clients_by_loop.values())
    _clients_by_loop = {}
    for client in pending:
        try:
            if not client.is_closed:
                await client.aclose()
        except Exception:  # pragma: no cover - defensive
            pass


async def complete(
    prompt: str, 
    model: str = MODEL_4,
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
    effort: str | None = None,
) -> dict | str | None:
    """Call the AI completions endpoint asynchronously.

    When the active provider is ``gemini``, delegates to
    ``gemini_client.complete()`` which has the same signature & return
    contract.  Otherwise uses the original OpenAI httpx path.

    Raises ``ValueError`` if the API key is missing so callers can handle the
    configuration error before any network request is attempted.
    """

    # ── Auto-detect provider from model name ─────────────────────
    effective_provider = _active_provider
    if model.startswith("claude-"):
        effective_provider = "anthropic"
    elif model.startswith("gemini-"):
        effective_provider = "gemini"
    elif model.startswith("gpt-"):
        effective_provider = "openai"
    elif _is_bedrock_model(model):
        effective_provider = "bedrock"

    # ── Provider routing ───────────────────────────────────────────
    if effective_provider == "gemini":
        from . import gemini_client
        # Map OpenAI model names to Gemini equivalents when provider is gemini
        gemini_model = model
        if model.startswith("gpt-"):
            gemini_model = GEMINI_FLASH  # default Gemini model
        return await gemini_client.complete(
            prompt=prompt,
            model=gemini_model,
            store_response=store_response,
            asset=asset,
            agent_id=agent_id,
            assistant_id=assistant_id,
            prompt_hash=prompt_hash,
            broker_type=broker_type,
            gpt_mode=gpt_mode,
            response_format=response_format,
            timeout_seconds=timeout_seconds,
            retries=retries,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            top_p=top_p,
        )

    if effective_provider == "anthropic":
        from . import anthropic_client
        from .anthropic_sonnet_55 import completion_options
        anthropic_model = model
        if model.startswith("gpt-") or model.startswith("gemini-"):
            anthropic_model = CLAUDE_SONNET
        return await anthropic_client.complete(
            prompt=prompt,
            model=anthropic_model,
            store_response=store_response,
            asset=asset,
            agent_id=agent_id,
            assistant_id=assistant_id,
            prompt_hash=prompt_hash,
            broker_type=broker_type,
            gpt_mode=gpt_mode,
            response_format=response_format,
            timeout_seconds=timeout_seconds,
            retries=retries,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            top_p=top_p,
            **completion_options(anthropic_model, {"max_tokens": max_tokens, "effort": effort}),
        )

    if effective_provider == "bedrock":
        from . import bedrock_client
        bedrock_model = model
        if not _is_bedrock_model(model):
            # Provider was explicitly set to bedrock but model name is not a
            # recognised Bedrock ID — fall back to the EU Claude Sonnet 4.6 profile.
            bedrock_model = BEDROCK_CLAUDE_SONNET_46
        return await bedrock_client.complete(
            prompt=prompt,
            model=bedrock_model,
            store_response=store_response,
            asset=asset,
            agent_id=agent_id,
            assistant_id=assistant_id,
            prompt_hash=prompt_hash,
            broker_type=broker_type,
            gpt_mode=gpt_mode,
            response_format=response_format,
            timeout_seconds=timeout_seconds,
            retries=retries,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            top_p=top_p,
        )

    # ── OpenAI path (original implementation) ──────────────────────
    key = _api_key
    if not key:
        raise ValueError("missing api key")

    start_time = time.monotonic()
    response_id = None
    error_message = None
    response_content = None
    response_type = "error"
    tokens_used = None

    try:
        headers = {"Authorization": f"Bearer {key}"}
        
        # Build messages array with proper system/user separation
        messages = []
        
        # Use new system/user prompt separation if provided
        if system_prompt and user_prompt:
            messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": user_prompt})
            # Use combined prompt for logging/storage compatibility
            combined_prompt = f"System: {system_prompt}\n\nUser: {user_prompt}"
        else:
            # Fallback to legacy single prompt format
            messages.append({"role": "user", "content": prompt})
            combined_prompt = prompt
        
        payload = {
            "model": model,
            "messages": messages,
            "temperature": float(temperature),
            "top_p": float(top_p),
        }

        if response_format:
            payload["response_format"] = response_format

        # Retry logic for timeout and network errors
        last_exception = None
        resp = None  # Initialize resp to avoid UnboundLocalError
        data = None  # Parsed JSON response — set inside the retry loop
        
        for attempt in range(retries + 1):
            try:
                logger.info(
                    f"Starting GPT API call (attempt {attempt + 1}/{retries + 1})",
                    extra={
                        "timeout_seconds": timeout_seconds,
                        "model": model,
                        "asset": asset,
                        "agent_id": agent_id,
                        "gpt_mode": gpt_mode,
                        "attempt": attempt + 1,
                        "max_attempts": retries + 1
                    }
                )

                # Live-trading optimisation: reuse a pooled AsyncClient so we
                # keep TCP/TLS connections alive across the many GPT calls
                # each cycle makes.  Per-call timeout is supplied via the
                # request-level ``timeout`` argument.
                client = _get_async_client(timeout_seconds)
                request_timeout = httpx.Timeout(
                    connect=10.0,
                    read=float(timeout_seconds),
                    write=10.0,
                    pool=5.0,
                )
                with APICall("openai", model, asset=asset, agent_id=agent_id) as usage_call:
                    resp = await client.post(
                        "https://api.openai.com/v1/chat/completions",
                        headers=headers,
                        json=payload,
                        timeout=request_timeout,
                    )
                    resp.raise_for_status()
                    # Validate response body is parseable JSON before accepting
                    data = resp.json()
                    usage_call.record(data.get("usage"), response_id=data.get("id"), response_model=data.get("model"))
                    if "choices" not in data or not data["choices"]:
                        raise ValueError("GPT response missing 'choices'")
                break  # Success, exit retry loop
            except (httpx.ReadTimeout, httpx.ConnectTimeout) as e:
                last_exception = e
                if attempt < retries:
                    # Jittered backoff: wait 1-3 seconds before retry
                    import random
                    wait_time = random.uniform(1.0, 3.0)
                    logger.warning(
                        f"GPT call timeout on attempt {attempt + 1}, retrying in {wait_time:.1f}s",
                        extra={
                            "attempt": attempt + 1,
                            "max_retries": retries,
                            "timeout_seconds": timeout_seconds,
                            "model": model,
                            "asset": asset,
                            "agent_id": agent_id,
                            "gpt_mode": gpt_mode,
                        }
                    )
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    # Final attempt failed
                    logger.error(
                        f"GPT call failed after {retries + 1} attempts due to timeout",
                        extra={
                            "timeout_seconds": timeout_seconds,
                            "model": model,
                            "asset": asset,
                            "agent_id": agent_id,
                            "gpt_mode": gpt_mode,
                            "error_type": "ReadTimeout",
                        }
                    )
                    raise e
            except (json.JSONDecodeError, ValueError) as e:
                # Response arrived but body was empty/corrupt — retry the request
                last_exception = e
                if attempt < retries:
                    import random
                    wait_time = random.uniform(1.0, 3.0)
                    logger.warning(
                        f"GPT response parse error on attempt {attempt + 1}, retrying in {wait_time:.1f}s: {e}",
                        extra={
                            "attempt": attempt + 1,
                            "max_retries": retries,
                            "model": model,
                            "asset": asset,
                            "error_type": type(e).__name__,
                        }
                    )
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    logger.error(
                        f"GPT call failed after {retries + 1} attempts ({type(e).__name__}): {e}",
                        extra={"model": model, "asset": asset}
                    )
                    raise e
            except Exception as e:
                last_exception = e
                if attempt < retries and hasattr(e, 'response') and hasattr(e.response, 'status_code'):
                    # Retry on 5xx server errors
                    if 500 <= e.response.status_code < 600:
                        wait_time = random.uniform(2.0, 5.0)
                        logger.warning(
                            f"GPT call server error on attempt {attempt + 1}, retrying in {wait_time:.1f}s",
                            extra={
                                "attempt": attempt + 1,
                                "status_code": e.response.status_code,
                                "model": model,
                                "asset": asset,
                                "agent_id": agent_id,
                                "gpt_mode": gpt_mode,
                            }
                        )
                        await asyncio.sleep(wait_time)
                        continue
                # For non-retryable errors or final attempt, break and handle below
                break

        # Check if we have a valid response before processing
        if resp is None or data is None:
            raise ValueError("No valid response received from GPT API")

        # data was already parsed inside the retry loop
        content = data["choices"][0]["message"]["content"]
        tokens_used = data.get("usage", {}).get("total_tokens", 0)
        response_content = content
        response_type = "text"
        
        logger.info("GPT call succeeded", extra={"tokens": tokens_used})

        # Shared extractor (packages/core/json_extract). The old 3-pass chain
        # ended in a GREEDY re.search spanning the first '{' to the last '}', so a
        # response containing an object, then prose, then a corrected object failed
        # every pass and was returned as raw text — discarding the whole cycle.
        parsed_content = parse_model_json(content)
        if parsed_content is None:
            logger.error(f"No parseable JSON in GPT response: {content[:200]}...")
            response_type = "text"
            result = content
        else:
            response_type = "json"
            result = parsed_content

    except Exception as e:  # pragma: no cover - defensive
        error_message = f"{type(e).__name__}: {e}"
        
        # Enhanced error logging for HTTP errors
        if hasattr(e, 'response') and hasattr(e.response, 'status_code'):
            if e.response.status_code == 400:
                try:
                    error_detail = e.response.json() if hasattr(e.response, 'json') else str(e.response.text)
                    logger.error(
                        f"GPT call failed with 400 Bad Request: {error_detail}",
                        extra={
                            "status_code": e.response.status_code,
                            "error_detail": error_detail,
                            "model": model,
                            "asset": asset,
                            "agent_id": agent_id,
                            "gpt_mode": gpt_mode,
                            "timeout_seconds": timeout_seconds,
                            "retries": retries,
                        }
                    )
                except:
                    logger.error(f"GPT call failed (HTTPStatusError): {e}", exc_info=True)
            else:
                logger.error(f"GPT call failed ({type(e).__name__}): {e}", exc_info=True)
        else:
            logger.error(f"GPT call failed ({type(e).__name__}): {e}", exc_info=True)
        
        result = None

    # Calculate latency
    latency_ms = (time.monotonic() - start_time) * 1000.0

    # Store response in MongoDB if requested and we have the required data - fire-and-forget to reduce latency
    if store_response and asset and agent_id and prompt_hash:
        # Create background task to avoid blocking the main response
        async def _store_response_background():
            try:
                from packages.db.repositories import gpt_responses as gpt_repo
                
                # Parse decision if it's a valid JSON response
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
                    # Convert string confidence to float for storage
                    if confidence_raw == "high":
                        confidence = 0.8
                    elif confidence_raw == "medium":
                        confidence = 0.6
                    elif confidence_raw == "low":
                        confidence = 0.4
                    elif isinstance(confidence_raw, (int, float)):
                        confidence = float(confidence_raw)
                    else:
                        confidence = None
                    pattern = result.get("pattern") or result.get("condition")
                    direction = result.get("direction")
                    level = result.get("level") or result.get("entry_price")
                    stop_loss = result.get("stop") or result.get("stop_loss") or result.get("stoploss")
                    take_profit = result.get("target") or result.get("take_profit")
                    quantity = result.get("quantity") or result.get("size")
                
                response_id = await gpt_repo.store_gpt_response(
                    asset=asset,
                    symbol=asset,  # Use asset as symbol for now
                    agent_id=agent_id,
                    # assistant_id is no longer used - removed for compatibility
                    model=model,
                    prompt_hash=prompt_hash,
                    prompt_content=combined_prompt,  # Use combined prompt for storage
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
                
                logger.info(f"Stored GPT response with ID: {response_id}")
                
            except Exception as e:
                logger.error(f"Failed to store GPT response: {e}", exc_info=True)
        
        # Fire-and-forget background task
        asyncio.create_task(_store_response_background())

    return result
