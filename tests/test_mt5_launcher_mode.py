import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from apps.backend.routers import mt5
from packages.adapters.mt5_adapter import MT5Adapter
from packages.core import config
from packages.core.services import mt5_credentials
from scripts import start_agents


@pytest.fixture
def credential_files(monkeypatch, tmp_path):
    demo = tmp_path / "demo.json"
    live = tmp_path / "live.json"
    for path, server, login in ((demo, "Broker-Trial", "123"), (live, "Broker-Real", "456")):
        path.write_text(json.dumps({"mt5": {
            "server": server, "login": login, "password": "fixture-only", "allow_live": True,
        }}), encoding="utf-8")
    monkeypatch.setattr(mt5_credentials, "_DEFAULT_PATH", demo)
    monkeypatch.setattr(mt5_credentials, "_LOCAL_PATH", live)
    monkeypatch.setattr(mt5_credentials, "_DEMO_LOCAL_PATH", tmp_path / "demo.local.json")
    return demo, live


def test_launcher_selects_credentials_without_changing_files(credential_files, monkeypatch):
    original = [path.read_bytes() for path in credential_files]
    monkeypatch.setattr(config, "MT5_ALLOW_LIVE", True)
    monkeypatch.setenv("TRADELENS_MT5_MODE", "demo")
    assert mt5_credentials.load_mt5_credentials()["login"] == "123"
    assert not mt5_credentials.live_trading_allowed()
    monkeypatch.setenv("TRADELENS_MT5_MODE", "live")
    assert mt5_credentials.load_mt5_credentials()["login"] == "456"
    assert mt5_credentials.live_trading_allowed()
    assert [path.read_bytes() for path in credential_files] == original


def test_selected_mode_refuses_missing_or_wrong_account_file(credential_files, monkeypatch):
    demo, live = credential_files
    monkeypatch.setenv("TRADELENS_MT5_MODE", "live")
    live.unlink()
    assert mt5_credentials.load_mt5_credentials() is None
    monkeypatch.setenv("TRADELENS_MT5_MODE", "demo")
    demo.write_text('{"mt5":{"server":"Broker-Real","login":"456","password":"fixture-only"}}',
                    encoding="utf-8")
    assert mt5_credentials.load_mt5_credentials() is None


def test_demo_uses_local_override_when_present(credential_files, monkeypatch):
    monkeypatch.setenv("TRADELENS_MT5_MODE", "demo")
    override = mt5_credentials._DEMO_LOCAL_PATH
    override.write_text(json.dumps({"mt5": {
        "server": "Broker-Trial7", "login": "789", "password": "fixture-only",
    }}), encoding="utf-8")
    assert mt5_credentials.load_mt5_credentials()["login"] == "789"
    override.write_text("{", encoding="utf-8")
    assert mt5_credentials.load_mt5_credentials() is None


@pytest.mark.asyncio
async def test_status_and_agent_starter_require_selected_account(credential_files, monkeypatch):
    monkeypatch.setenv("TRADELENS_MT5_MODE", "demo")
    broker = MT5Adapter()
    account = SimpleNamespace(server="Broker-Real", login=456, trade_mode=2)
    broker._mt5 = SimpleNamespace(ACCOUNT_TRADE_MODE_DEMO=0, account_info=lambda: account)
    monkeypatch.setattr(broker, "is_connected", lambda: True)
    monkeypatch.setattr(mt5, "adapter", broker)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(broker_tokens={})))

    assert await mt5.mt5_status(request) == {"connected": False, "mode": "demo"}
    monkeypatch.setattr(start_agents, "MT5_MODE", "demo")
    monkeypatch.setattr(start_agents, "_request", Mock(return_value=(200, {"connected": True})))
    assert not start_agents._mt5_connected()

    broker._mt5.account_info = Mock(side_effect=RuntimeError("terminal unavailable"))
    assert await mt5.mt5_status(request) == {"connected": False, "mode": "demo"}
    broker._mt5.account_info = lambda: account
    account.server, account.login, account.trade_mode = "Broker-Trial", 123, 0
    status = await mt5.mt5_status(request)
    monkeypatch.setattr(start_agents, "_request", Mock(return_value=(200, status)))
    assert start_agents._mt5_connected()


@pytest.mark.asyncio
async def test_dashboard_reconnect_uses_launcher_choice(credential_files, monkeypatch):
    monkeypatch.setenv("TRADELENS_MT5_MODE", "demo")
    login = Mock()
    monkeypatch.setattr(mt5, "adapter", SimpleNamespace(login=login))
    saved_brokers = Mock(side_effect=AssertionError("Launcher reconnect must use selected credentials"))
    monkeypatch.setattr(mt5.config_repo, "get_current", saved_brokers)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    assert await mt5.mt5_connect(request) == {"status": "ok"}
    login.assert_called_once_with(server="Broker-Trial", login="123", password="fixture-only")
    assert request.app.state.broker_tokens["mt5"]["login"] == "123"
