from common.config import Config


def test_config_loads_env(monkeypatch):
    monkeypatch.setenv("GPT__API_KEY", "abc123")
    Config._instance = None  # reset singleton
    cfg = Config.get()
    assert cfg.gpt.api_key == "abc123"
