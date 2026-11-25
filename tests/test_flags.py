import importlib

from common import flags

FLAG_NAMES = [
    "LIVE_TRADING",
    "USE_MT5",
    "USE_GPT",
]


def reload_flags():
    return importlib.reload(flags)


def test_flags_default_false(monkeypatch):
    for name in FLAG_NAMES:
        monkeypatch.delenv(name, raising=False)
    module = reload_flags()
    for name in FLAG_NAMES:
        assert module.is_enabled(name) is False


def test_flags_env_override(monkeypatch):
    for name in FLAG_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("USE_GPT", "1")
    module = reload_flags()
    assert module.is_enabled("USE_GPT") is True
    for name in FLAG_NAMES:
        if name != "USE_GPT":
            assert module.is_enabled(name) is False
