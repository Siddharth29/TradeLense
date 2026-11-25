import pytest
from types import SimpleNamespace

from packages.core.sniper_agent import _parse_json_from_text, call_sniper


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Here is data:\n```json\n{\"a\": 1}\n```", {"a": 1}),
        ("Noise before ```json\n[1, 2, 3]\n``` noise after", [1, 2, 3]),
    ],
)
def test_parse_json_from_text_code_fence(text, expected):
    """JSON inside fenced blocks should be parsed."""
    assert _parse_json_from_text(text) == expected


def test_parse_json_from_text_invalid_returns_none():
    """Invalid JSON should return None instead of raising."""
    assert _parse_json_from_text("```json\n{invalid}\n```") is None


def test_parse_json_from_text_handles_braces_in_strings():
    text = 'prefix {"a": "value with { brace }", "b": 2} suffix'
    assert _parse_json_from_text(text) == {"a": "value with { brace }", "b": 2}


def test_parse_json_from_text_without_json():
    assert _parse_json_from_text("no json here") is None


@pytest.mark.asyncio
async def test_call_sniper_timeout(monkeypatch):
    """call_sniper should return None if the assistant never completes."""

    class FakeRuns:
        async def create(self, *args, **kwargs):
            return SimpleNamespace(id="run")

        async def retrieve(self, *args, **kwargs):
            return SimpleNamespace(id="run", status="running")

    class FakeMessages:
        async def create(self, *args, **kwargs):
            pass

    class FakeThreads:
        def __init__(self):
            self.messages = FakeMessages()
            self.runs = FakeRuns()

        async def create(self, *args, **kwargs):
            return SimpleNamespace(id="thread")

    class FakeBeta:
        def __init__(self):
            self.threads = FakeThreads()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.beta = FakeBeta()

    async def fast_sleep(*args, **kwargs):
        return None

    monkeypatch.setattr("packages.core.sniper_agent.AsyncOpenAI", FakeClient)
    monkeypatch.setattr("packages.core.sniper_agent.asyncio.sleep", fast_sleep)
    # Provide a dummy API key to satisfy validation
    monkeypatch.setattr("packages.core.sniper_agent._api_key", "key")

    def fake_build_prompt(*args, **kwargs):
        return "prompt"

    monkeypatch.setattr("packages.core.sniper_agent.build_prompt", fake_build_prompt)

    result = await call_sniper("{}", "{}", "t")
    assert result is None


@pytest.mark.asyncio
async def test_call_sniper_requires_api_key(monkeypatch):
    """An explicit error is raised when no API key is configured."""
    monkeypatch.setattr("packages.core.sniper_agent._api_key", None)
    with pytest.raises(ValueError, match="missing api key"):
        await call_sniper("{}", "{}", "t")
