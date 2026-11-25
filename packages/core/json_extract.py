"""Robust JSON extraction from LLM text output.

Why this exists
---------------
Every model client (anthropic / gpt / gemini / bedrock) carried the same
three-pass parse: ``json.loads`` → strip markdown fences → ``re.search(r"\\{.*\\}",
DOTALL)``. That third pass is **greedy**, so it spans from the first ``{`` in the
response to the last ``}`` — which is only correct when the response contains
exactly one JSON object.

Observed 2026-08-07 (backtest ``6a761abb``, ~9% of trend-expert calls): the model
emitted a complete object, then second-guessed itself in prose and emitted a
corrected one::

    {"proposals":[...]}

    Wait, I need to recompute properly and output valid JSON.

    {"proposals":[{"signal":"flat", ...}]}

Pass 1 and 2 fail with ``Extra data: line 3 column 1``. Pass 3's greedy regex
swallows *both* objects plus the prose between them and fails identically. The
client then returns the raw string, and ``call_expert`` raises

    Expert trend did not return JSON, got: <class 'str'>

— an error message that describes the symptom and hides the cause, which is why
this went undiagnosed. The whole cycle is discarded: in live that is a lost
trading decision, in a backtest a silently missing iteration.

The fix is to scan for *balanced* top-level objects rather than regex for the
outermost braces, and prefer the LAST one that parses — when a model corrects
itself, the correction is the answer.
"""

from __future__ import annotations

import json
from typing import Any, Iterator, Optional

__all__ = ["extract_json_objects", "parse_model_json"]


def extract_json_objects(text: str) -> Iterator[str]:
    """Yield each balanced top-level ``{...}`` span in *text*, in order.

    Brace counting is string-aware: braces inside JSON string literals (and
    escaped quotes within them) do not affect depth, so a rationale containing
    ``"}"`` or ``"{"`` cannot split an object. Unterminated spans are skipped —
    a truncated tail yields nothing rather than a partial object.
    """
    depth = 0
    start = -1
    in_string = False
    escaped = False

    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    yield text[start : i + 1]
                    start = -1


def parse_model_json(content: str) -> Optional[Any]:
    """Parse JSON out of a model response, or return ``None``.

    Order:
      1. the whole string (the overwhelmingly common case);
      2. the whole string with markdown fences stripped;
      3. balanced top-level objects, **last one first** — a model that emits a
         second object after prose is correcting the first, so the last
         parseable object is its final answer.

    Returns the parsed value, or ``None`` when nothing parses. Callers keep
    their existing "not a dict ⇒ error" contract; this only widens what counts
    as parseable.
    """
    if not isinstance(content, str):
        return None

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    cleaned = content.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()
    if cleaned != content:
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

    candidates = list(extract_json_objects(cleaned or content))
    for candidate in reversed(candidates):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    return None
