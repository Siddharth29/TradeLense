import pytest

from packages.core import gpt_client


@pytest.mark.asyncio
async def test_complete_requires_api_key(monkeypatch):
    """gpt_client.complete should fail fast when no API key is provided."""

    # Remove any configured key and ensure a network call would fail if made.
    gpt_client.set_api_key(None)

    async def fail_post(*args, **kwargs):  # pragma: no cover - safety
        raise AssertionError("network call attempted")

    monkeypatch.setattr(
        "packages.core.gpt_client.httpx.AsyncClient.post", fail_post
    )

    with pytest.raises(ValueError, match="missing api key"):
        await gpt_client.complete("prompt")
