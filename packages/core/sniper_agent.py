import json
import asyncio
import re
from openai import AsyncOpenAI
from .logger import get_logger
from .ai_accounting import APICall

from .config import OPENAI_API_KEY, AI_PROVIDER, GEMINI_API_KEY, GEMINI_FLASH

# Load the API key once at import time; individual calls can override via the
# ``api_key`` parameter.
_api_key = OPENAI_API_KEY
logger = get_logger(__name__)


async def _call_sniper_gemini(
    system_prompt: str,
    user_messages: list[str],
    config: dict,
    response_format: dict,
    asset: str | None = None,
) -> dict | list | None:
    """Gemini implementation of the sniper call with identical output contract."""
    from google import genai

    key = GEMINI_API_KEY
    if not key:
        raise ValueError("missing Gemini API key – set GEMINI_API_KEY env var")

    client = genai.Client(api_key=key)

    # Map OpenAI model to Gemini
    model_name = config.get("model", GEMINI_FLASH)
    if model_name.startswith("gpt-"):
        model_name = GEMINI_FLASH

    # Build generation config
    gen_config: dict = {
        "system_instruction": system_prompt,
        "response_mime_type": "application/json",
    }

    # Convert OpenAI response_format schema to Gemini schema
    from .gemini_client import _openai_schema_to_gemini
    gemini_schema = _openai_schema_to_gemini(response_format)
    if gemini_schema:
        gen_config["response_json_schema"] = gemini_schema

    # Only add temperature/top_p for non-gpt-5 models (matching original logic)
    if "gpt-5" not in model_name.lower():
        gen_config["temperature"] = config.get("temperature", 0)
        gen_config["top_p"] = config.get("top_p", 1)

    # Combine user messages
    combined_user = "\n".join(user_messages)

    logger.info(f"Calling Gemini (sniper) with model {model_name}")

    with APICall("gemini", model_name, asset=asset) as usage_call:
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=model_name,
            contents=combined_user,
            config=gen_config,
        )
        usage_call.record(getattr(response, "usage_metadata", None))

    content = response.text
    tokens_used = 0
    if response.usage_metadata:
        tokens_used = (
            (response.usage_metadata.prompt_token_count or 0)
            + (response.usage_metadata.candidates_token_count or 0)
        )
    logger.info(f"Gemini sniper call succeeded, tokens used: {tokens_used}")
    return content  # Return raw text; caller will parse JSON


async def call_sniper(
    c15_json: str,
    c5_json: str,
    timestamp: str,
    api_key: str | None = None,
    prompt_id: str | None = None,
    prompt_content: str | None = None,
    gpt_config: dict | None = None,
    store_response: bool = True,
    asset: str | None = None,
    agent_id: str | None = None,
    prompt_hash: str | None = None,
    broker_type: str | None = None,
    gpt_mode: str | None = None,
) -> dict | list | None:
    """Call the Sniper-AI using chat completion and return its JSON response."""
    key = api_key or _api_key
    if not key:
        raise ValueError("missing api key")
    client = AsyncOpenAI(api_key=key)
    
    # Get GPT configuration from prompt or use defaults
    if gpt_config:
        config = gpt_config
    else:
        # Default configuration - simplified to only essential parameters
        config = {
            "model": "gpt-4o",  # Current GPT-4 model
            "temperature": 0.7,
            "top_p": 0.9,
        }
    
    # Build the prompt with candle data
    if prompt_content:
        # Use custom prompt content if provided
       full_prompt = f"""{prompt_content}

                        Candle Data:
                        5min candles = {c5_json} 
                        15min candles = {c15_json}"""

    else:
        # Default prompt for trading analysis
        full_prompt = f"""Analyze the following candlestick data and provide a trading decision in JSON format.

5-minute candles: {c5_json}
15-minute candles: {c15_json}

Please respond with a JSON object containing:
- direction: "buy", "sell", or "hold"
- pattern: description of the pattern identified
- level: entry price
- stop: stop loss price
- target: take profit price
- confidence: "high", "medium", or "low"
- quantity: suggested position size
- rationale: 2-3 sentences explaining the observed setup, levels and entry trigger

Ensure the response is valid JSON only."""
    
    # Variables for storing response details
    response_id = None
    start_time = asyncio.get_event_loop().time()
    response_content = None
    response_type = "error"
    error_message = None
    tokens_used = None
    
    try:
        logger.info(f"Calling GPT chat completion with model {config['model']}")
        
        # Define response format schema for structured output
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": "trade_signal",
                "schema": {
                    "type": "object",
                    "properties": {
                        "direction": {"type": "string", "enum": ["long", "short"]},
                        "pattern": {"type": "string", "enum": ["retest", "breakout", "BOS", "FVG fill", "orderblock", "liquidity sweep", "compression breakout", "range breakout retest"]},
                        "level": {"type": "number"},
                        "stop": {"type": "number"},
                        "target": {"type": "number"},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                        "status": {"type": "string", "enum": ["waiting"]},
                        "rationale": {"type": "string"}
                    },
                    "required": ["direction", "pattern", "level", "stop", "target", "confidence", "status", "rationale"],
                    "additionalProperties": False
                }
            }
        }
        
        # Prepare chat completion parameters with structured output
        model_name = config.get("model", GEMINI_FLASH if AI_PROVIDER == "gemini" else "gpt-4o")

        # ── Gemini provider branch ─────────────────────────────────
        provider = config.get("provider", AI_PROVIDER)
        if provider == "gemini":
            system_msg = (
                f"You are a discretionary intraday {asset} trader with 20 years of live-screen experience. "
                "Your ONLY job is to analyze c15 and c5 candle data, apply the provided rules, and output a STRICT JSON object. "
                "Explain the observed setup and entry trigger in the rationale JSON field. If no setup qualifies, return []."
            )
            user_msgs = [
                full_prompt,
                f"c15 candles = {c15_json}\nc5 candles = {c5_json}",
            ]
            raw_content = await _call_sniper_gemini(
                system_prompt=system_msg,
                user_messages=user_msgs,
                config=config,
                response_format=response_format,
                asset=asset,
            )
            response_content = raw_content
            response_type = "json"
            # Fall through to the JSON parsing section below
            # Parse the content the same way as OpenAI path
            try:
                result = json.loads(response_content) if response_content else []
            except json.JSONDecodeError:
                logger.warning("Gemini sniper: JSON parse failed, attempting cleanup")
                cleaned = (response_content or "").strip()
                if cleaned.startswith("```json"):
                    cleaned = cleaned[7:]
                if cleaned.endswith("```"):
                    cleaned = cleaned[:-3]
                cleaned = cleaned.strip()
                try:
                    result = json.loads(cleaned)
                except json.JSONDecodeError:
                    logger.error(f"Gemini sniper: all JSON parse attempts failed")
                    return []

            # Same validation as OpenAI path
            if isinstance(result, dict):
                required_fields = ["direction", "pattern", "level", "stop", "target", "confidence", "status"]
                missing_fields = [field for field in required_fields if field not in result]
                if missing_fields:
                    logger.warning(f"Gemini response missing required fields: {missing_fields}")
                    return []
                if result.get("direction") not in ["long", "short"]:
                    return []
                if result.get("confidence") not in ["high", "medium", "low"]:
                    return []
                if result.get("status") != "waiting":
                    return []
                for field in ["level", "stop", "target"]:
                    value = result.get(field)
                    if value is None or not isinstance(value, (int, float)) or value <= 0:
                        return []
                logger.info("Gemini returned valid structured trading signal")
                return result
            return []

        # ── OpenAI provider path (original) ────────────────────────
        completion_params = {
            "model": model_name,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"You are a discretionary intraday {asset} trader with 20 years of live-screen experience. "
                        "Your ONLY job is to analyze c15 and c5 candle data, apply the provided rules, and output a STRICT JSON object. "
                        "Explain the observed setup and entry trigger in the rationale JSON field. If no setup qualifies, return []."
                    )
                },
                {
                    "role": "user",
                    "content": full_prompt 
                },
                {
                    "role": "user",
                    "content": f"c15 candles = {c15_json}\nc5 candles = {c5_json}"
                }
            ],
            "response_format": response_format
        }
        
        # Only add temperature and top_p for models that support them
        if "gpt-5" not in model_name.lower():
            completion_params["temperature"] = config.get("temperature", 0)
            completion_params["top_p"] = config.get("top_p", 1)
        
        # Make chat completion request
        with APICall("openai", model_name, asset=asset, agent_id=agent_id) as usage_call:
            response = await client.chat.completions.create(**completion_params)
            usage_call.record(getattr(response, "usage", None))
        
        # Extract response content
        response_content = response.choices[0].message.content
        tokens_used = response.usage.total_tokens if response.usage else 0
        response_type = "json"  # With structured output, we always get JSON
        
        logger.info(f"GPT chat completion succeeded, tokens used: {tokens_used}")
        
        # Parse the structured JSON response
        try:
            result = json.loads(response_content)
            
            # Handle structured object response
            if isinstance(result, dict):
                # Validate required fields are present for valid signals
                required_fields = ["direction", "pattern", "level", "stop", "target", "confidence", "status"]
                missing_fields = [field for field in required_fields if field not in result]
                
                if missing_fields:
                    logger.warning(f"GPT response missing required fields: {missing_fields}")
                    logger.info("GPT returned incomplete response - no valid trading signal found")
                    return []
                
                # Additional validation for field values
                if result.get("direction") not in ["long", "short"]:
                    logger.warning(f"Invalid direction: {result.get('direction')}")
                    return []
                
                if result.get("confidence") not in ["high", "medium", "low"]:
                    logger.warning(f"Invalid confidence: {result.get('confidence')}")
                    return []
                
                if result.get("status") != "waiting":
                    logger.warning(f"Invalid status: {result.get('status')}")
                    return []
                
                # Validate numeric fields are positive
                numeric_fields = ["level", "stop", "target"]
                for field in numeric_fields:
                    value = result.get(field)
                    if value is None or not isinstance(value, (int, float)) or value <= 0:
                        logger.warning(f"Invalid {field}: {value}")
                        return []
                
                logger.info("GPT returned valid structured trading signal")
                return result
            
            # If we get here, the response format is unexpected
            logger.warning(f"Unexpected response format: {type(result)}")
            return []
            
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse structured JSON response: {e}")
            logger.error(f"Response content: {response_content}")
            return []
    except Exception as e:
        error_message = f"{type(e).__name__}: {e}"
        logger.error(
            f"GPT chat completion failed ({type(e).__name__}): {e}", exc_info=True
        )
        return None
    finally:
        # Store response in MongoDB if requested and we have the required data
        if store_response and asset and agent_id and prompt_hash:
            try:
                from packages.db.repositories import gpt_responses as gpt_repo
                
                # Calculate latency
                latency_ms = (asyncio.get_event_loop().time() - start_time) * 1000.0
                
                # Parse decision if it's a valid JSON response
                parsed_decision = None
                confidence = None
                pattern = None
                direction = None
                level = None
                stop_loss = None
                take_profit = None
                quantity = None
                
                # Try to get the result from the return value
                result = None
                if response_type == "json" and response_content:
                    try:
                        result = json.loads(response_content)
                    except:
                        pass
                
                if result and isinstance(result, dict):
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
                    agent_id=agent_id,
                    model=config["model"],
                    prompt_hash=prompt_hash,
                    prompt_content=full_prompt,
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


def _parse_json_from_text(text: str):
    """Attempt to extract and parse the first JSON object/array from text.

    Strategy:
    1) Look for fenced code blocks ```json ... ``` or ``` ... ``` and try to parse.
    2) Scan for the first balanced {...} or [...] while respecting JSON strings.
    Returns a Python object or None if not found/parsable.
    """
    if not text:
        return None

    # 1) Fenced code blocks
    fence = re.search(r"```(?:json|JSON)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        candidate = fence.group(1).strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            # Continue to next strategy
            pass

    # 2) Balanced braces/brackets scan
    for start_char, end_char in (("{", "}"), ("[", "]")):
        start = text.find(start_char)
        while start != -1:
            depth = 0
            in_string = False
            escape = False
            for i in range(start, len(text)):
                ch = text[i]
                if in_string:
                    if escape:
                        escape = False
                    elif ch == "\\":
                        escape = True
                    elif ch == '"':
                        in_string = False
                    continue
                else:
                    if ch == '"':
                        in_string = True
                        continue
                    if ch == start_char:
                        depth += 1
                    elif ch == end_char:
                        depth -= 1
                        if depth == 0:
                            candidate = text[start : i + 1]
                            try:
                                return json.loads(candidate)
                            except json.JSONDecodeError:
                                break  # try next start position
            start = text.find(start_char, start + 1)

    return None
