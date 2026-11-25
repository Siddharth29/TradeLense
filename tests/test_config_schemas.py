from __future__ import annotations
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from packages.db.config_schemas import (
    AdminConfig,
    AgentConfig,
    BrokerConfigMt5,
    BrokerType,
    GptConfig,
    InstrumentConfig,
    Env,
    Mode,
    RiskConfig,
    FeatureFlagsConfig,
    UiPrefsConfig,
    Theme,
)


def test_enum_enforcement() -> None:
    with pytest.raises(ValidationError):
        BrokerConfigMt5(server="s", loginId="l", password="p", mode="invalid")
    with pytest.raises(ValidationError):
        GptConfig(api_key="k", model="unsupported")
    with pytest.raises(ValidationError):
        UiPrefsConfig(theme="blue")


def test_range_checks() -> None:
    with pytest.raises(ValidationError):
        RiskConfig(daily_max_loss=1.5)
    with pytest.raises(ValidationError):
        RiskConfig(daily_max_loss=-0.1)
    with pytest.raises(ValidationError):
        RiskConfig(cooldown_minutes=-1)


def test_unknown_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        BrokerConfigMt5(server="s", loginId="l", password="p", mode=Mode.PAPER, extra=True)
    with pytest.raises(ValidationError):
        FeatureFlagsConfig(enable_backtest=True, extra=True)


def test_agent_config_and_utc_validation() -> None:
    cfg = AgentConfig(
        agent_id="alpha",
        candles_5_count=100,
        candles_15_count=100,
        broker=BrokerType.MT5,
        gpt=GptConfig(api_key="key"),
        risk=RiskConfig(),
        instrument=InstrumentConfig(symbol="XAUUSD"),
        admin=AdminConfig(env=Env.DEV, created_at=datetime.now(timezone.utc)),
        flags=FeatureFlagsConfig(),
    )
    assert cfg.broker is BrokerType.MT5

    ui = UiPrefsConfig()
    assert ui.theme is Theme.LIGHT

    BrokerConfigMt5(server="s", loginId="l", password="p", mode=Mode.PAPER)

    with pytest.raises(ValidationError):
        AdminConfig(env=Env.DEV, created_at=datetime.now())

    with pytest.raises(ValidationError):
        AgentConfig(
            candles_5_count=100,
            candles_15_count=100,
            broker=BrokerType.MT5,
            gpt=GptConfig(api_key="key"),
            risk=RiskConfig(),
            instrument=InstrumentConfig(symbol="XAUUSD"),
            admin=AdminConfig(env=Env.DEV, created_at=datetime.now(timezone.utc)),
            flags=FeatureFlagsConfig(),
        )


def test_feature_flag_defaults() -> None:
    flags = FeatureFlagsConfig()
    assert flags.enable_backtest is False
    assert flags.enable_stream is False
    flags2 = FeatureFlagsConfig(enable_backtest=True)
    assert flags2.enable_backtest is True


def test_risk_config_max_trades() -> None:
    with pytest.raises(ValidationError):
        RiskConfig(max_trades_per_day=-1)
    cfg = RiskConfig(
        max_trades_per_day=5,
        cooldown_minutes=10,
        per_asset={"XAUUSD": {"mt5_risk_per_trade": 0.1}},
    )
    assert cfg.max_trades_per_day == 5
    assert cfg.cooldown_minutes == 10
    assert cfg.per_asset["XAUUSD"].mt5_risk_per_trade == 0.1

    with pytest.raises(ValidationError):
        RiskConfig(per_asset={"XAUUSD": {"mt5_risk_per_trade": -0.1}})
    with pytest.raises(ValidationError):
        RiskConfig(per_asset={"XAUUSD": {"extra": 1}})


def test_agent_config_assistant_id_matches_agent_id() -> None:
    AgentConfig(
        agent_id="alpha",
        candles_5_count=100,
        candles_15_count=100,
        broker=BrokerType.MT5,
        gpt=GptConfig(api_key="key"),
        risk=RiskConfig(),
        instrument=InstrumentConfig(symbol="XAUUSD"),
        admin=AdminConfig(env=Env.DEV, created_at=datetime.now(timezone.utc)),
        flags=FeatureFlagsConfig(),
    )

    with pytest.raises(ValidationError):
        AgentConfig(
            agent_id="alpha",
            assistant_id="beta",
            candles_5_count=100,
            candles_15_count=100,
            broker=BrokerType.MT5,
            gpt=GptConfig(api_key="key"),
            risk=RiskConfig(),
            instrument=InstrumentConfig(symbol="XAUUSD"),
            admin=AdminConfig(env=Env.DEV, created_at=datetime.now(timezone.utc)),
            flags=FeatureFlagsConfig(),
        )
