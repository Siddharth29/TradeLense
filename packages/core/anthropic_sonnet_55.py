"""Sonnet 5.5 Messages API options and response handling."""

from typing import Any, Literal, Mapping, get_args

from .config import CLAUDE_SONNET_55

ReasoningEffort = Literal["low", "medium", "high", "xhigh", "max"]
DEFAULT_EFFORT: ReasoningEffort = "medium"


def completion_options(model: str, config: Mapping[str, Any]) -> dict[str, Any]:
    """Forward the saved generation options for the selected model."""
    if model != CLAUDE_SONNET_55:
        return {}
    return {"max_tokens": config.get("max_tokens"), "effort": config.get("effort")}


def request_options(max_tokens: int | None, effort: ReasoningEffort | None) -> dict[str, Any]:
    if max_tokens is None:
        max_tokens = 4096
    if type(max_tokens) is not int or not 1 <= max_tokens <= 128_000:
        raise ValueError("Sonnet 5.5 max_tokens must be an integer from 1 to 128000")
    if effort is None:
        effort = DEFAULT_EFFORT
    if effort not in get_args(ReasoningEffort):
        raise ValueError("Invalid Sonnet 5.5 reasoning effort")

    # extra_body also supports SDK versions predating the output_config keyword.
    return {"max_tokens": max_tokens, "extra_body": {"output_config": {"effort": effort}}}


def response_text(response: Any) -> str:
    if response.stop_reason not in {"end_turn", "stop_sequence"}:
        raise ValueError(f"Sonnet 5.5 did not complete its answer ({response.stop_reason})")

    content = "".join(block.text for block in response.content if block.type == "text")
    if not content.strip():
        raise ValueError("Sonnet 5.5 returned no answer text")
    return content
