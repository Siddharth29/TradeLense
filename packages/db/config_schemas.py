from __future__ import annotations

"""Pydantic schemas for persisted configuration data."""

from datetime import datetime, timezone
from enum import Enum

from pydantic import (
    BaseModel,
    Field,
    ConfigDict,
    field_validator,
    model_validator,
    AliasChoices,
)
from typing import Any, Optional, Dict

from packages.core.anthropic_sonnet_55 import DEFAULT_EFFORT, ReasoningEffort
from packages.core.config import CLAUDE_SONNET_55


class BrokerType(str, Enum):
    """Supported broker identifiers."""

    MT5 = "mt5"


class Mode(str, Enum):
    """Broker operating modes."""

    PAPER = "paper"
    LIVE = "live"


class AIProvider(str, Enum):
    """Supported AI provider backends."""

    OPENAI = "openai"
    GEMINI = "gemini"
    ANTHROPIC = "anthropic"
    BEDROCK = "bedrock"


class GptModel(str, Enum):
    """Supported GPT model choices."""

    GPT3_5 = "gpt-3.5-turbo"
    GPT4O = "gpt-4o"
    GPT4O_MINI = "gpt-4o-mini"
    GEMINI_FLASH = "gemini-2.5-flash"
    CLAUDE_HAIKU = "claude-haiku-4-5-20251001"
    CLAUDE_SONNET = "claude-sonnet-4-6"
    # Amazon Bedrock models
    BEDROCK_NOVA_PRO   = "us.amazon.nova-pro-v1:0"
    BEDROCK_NOVA_LITE  = "us.amazon.nova-lite-v1:0"
    BEDROCK_NOVA_MICRO = "us.amazon.nova-micro-v1:0"
    BEDROCK_LLAMA_70B  = "us.meta.llama3-1-70b-instruct-v1:0"
    BEDROCK_CLAUDE_SONNET_46 = "arn:aws:bedrock:eu-north-1:713939170911:inference-profile/eu.anthropic.claude-sonnet-4-6"


class Theme(str, Enum):
    """UI theme preference."""

    LIGHT = "light"
    DARK = "dark"


class Env(str, Enum):
    """Application environment."""

    DEV = "dev"
    PROD = "prod"


class BrokerConfigMt5(BaseModel):
    """Configuration for the MetaTrader5 broker."""

    server: str
    login_id: str = Field(
        validation_alias=AliasChoices("loginId", "login", "login_id"),
        serialization_alias="loginId",
    )
    password: str
    mode: Mode = Field(default=Mode.LIVE)
    wallet_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("walletUrl", "wallet_url"),
        serialization_alias="walletUrl",
    )

    model_config = ConfigDict(extra="forbid", strict=True)


class GptCallConfig(BaseModel):
    """Configuration for GPT API calls with all OpenAI parameters."""

    model: str = Field(default="gpt-4o", description="The model to use for the completion")
    temperature: float = Field(default=1.0, ge=0.0, le=2.0, description="Controls randomness")
    top_p: float = Field(default=1.0, ge=0.0, le=1.0, description="Controls diversity via nucleus sampling")
    max_tokens: int = Field(default=1000, ge=1, le=4096, description="Maximum tokens to generate")
    frequency_penalty: float = Field(default=0.0, ge=-2.0, le=2.0, description="Penalizes new tokens based on frequency")
    presence_penalty: float = Field(default=0.0, ge=-2.0, le=2.0, description="Penalizes new tokens based on presence")
    seed: int | None = Field(default=None, description="Random seed for reproducible outputs")
    stop: list[str] | str | None = Field(default=None, description="Stop sequences")
    stream: bool = Field(default=False, description="Stream the response")
    user: str | None = Field(default=None, description="User identifier for tracking")

    model_config = ConfigDict(extra="forbid", strict=True)


class PromptConfig(BaseModel):
    """Configuration for GPT prompts with associated GPT call settings."""

    id: str
    name: str
    content: str
    description: str | None = None
    gpt_config: GptCallConfig = Field(default_factory=GptCallConfig)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = ConfigDict(extra="forbid", strict=True)


class ModelConfig(BaseModel):
    """Global model configuration stored in the configs collection."""

    model: str = Field(default="claude-sonnet-4-6", description="The model to use")
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    top_p: float = Field(default=0.9, ge=0.0, le=1.0)
    max_tokens: int = Field(default=4096, ge=1, le=32768)
    effort: ReasoningEffort | None = None

    model_config = ConfigDict(extra="forbid", strict=True)

    @model_validator(mode="after")
    def _default_sonnet_effort(self) -> "ModelConfig":
        if self.model == CLAUDE_SONNET_55 and self.effort is None:
            self.effort = DEFAULT_EFFORT
        return self


class GptConfig(BaseModel):
    """Settings for GPT/Gemini/Anthropic/Bedrock usage - handles API keys, prompt selection, and provider."""

    api_key: str | None = None
    gemini_api_key: str | None = None
    anthropic_api_key: str | None = None
    provider: AIProvider = Field(default=AIProvider.OPENAI, description="AI provider: openai, gemini, anthropic, or bedrock")
    prompt_id: str | None = None

    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("provider", mode="before")
    @classmethod
    def _coerce_provider(cls, v: Any) -> AIProvider:
        if isinstance(v, str):
            return AIProvider(v)
        return v

    @model_validator(mode="after")
    def _require_key(cls, m: "GptConfig") -> "GptConfig":
        # API key will be loaded from environment, so this validation is optional
        return m


class RiskConfig(BaseModel):
    """Risk management parameters."""

    class AssetRisk(BaseModel):
        mt5_risk_per_trade: float = Field(0.0, ge=0.0, le=1.0)

        model_config = ConfigDict(extra="forbid", strict=True)

    daily_max_loss: float = Field(
        0.0,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("daily_max_loss", "max_daily_loss"),
        serialization_alias="daily_max_loss",
    )
    max_trades_per_day: int = Field(0, ge=0)
    cooldown_minutes: int = Field(0, ge=0)
    per_asset: dict[str, AssetRisk] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid", strict=True)


class InstrumentConfig(BaseModel):
    """Instrument specific configuration (MT5 symbol)."""

    symbol: str

    model_config = ConfigDict(extra="forbid", strict=True)


class FeatureFlagsConfig(BaseModel):
    """Feature toggle configuration."""

    enable_backtest: bool = False
    enable_stream: bool = False

    model_config = ConfigDict(extra="forbid", strict=True)


class UiPrefsConfig(BaseModel):
    """Optional UI preferences."""

    theme: Theme = Field(default=Theme.LIGHT)

    model_config = ConfigDict(extra="forbid", strict=True)


class AdminConfig(BaseModel):
    """Administrative metadata for the configuration."""

    env: Env
    created_at: datetime

    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("created_at")
    @classmethod
    def _ensure_created_at_utc(cls, v: datetime) -> datetime:
        if v.tzinfo != timezone.utc:
            raise ValueError("created_at must be UTC")
        return v


class AgentConfig(BaseModel):
    """Top-level agent configuration.

    ``agent_id`` is required and must be provided by all persisted configs.
    ``candles_5_count`` and ``candles_15_count`` represent the number of
    historical candles to fetch for their respective intervals.
    """

    agent_id: str
    candles_5_count: int = Field(
        100,
        ge=1,
        validation_alias=AliasChoices("candles_5_count", "candle_count_5m", "candle_count"),
        serialization_alias="candles_5_count",
    )
    candles_15_count: int = Field(
        100,
        ge=1,
        validation_alias=AliasChoices("candles_15_count", "candle_count_15m"),
        serialization_alias="candles_15_count",
    )
    broker: BrokerType
    wallet_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("walletUrl", "wallet_url"),
        serialization_alias="walletUrl",
    )
    gpt: GptConfig
    risk: RiskConfig
    instrument: InstrumentConfig
    flags: FeatureFlagsConfig = Field(default_factory=FeatureFlagsConfig)
    admin: AdminConfig
    ui: UiPrefsConfig | None = None
    dynamic_config: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Dynamic configuration overrides from AI recommendations"
    )

    model_config = ConfigDict(extra="forbid", strict=True)

    @property
    def candle_count_5m(self) -> int:  # pragma: no cover - backward compat
        return self.candles_5_count

    @property
    def candle_count_15m(self) -> int:  # pragma: no cover - backward compat
        return self.candles_15_count
    
    def get_effective_candle_counts(self) -> tuple[int, int]:
        """Get effective candle counts considering dynamic configuration overrides.
        
        Returns:
            tuple[int, int]: (candles_5m_count, candles_15m_count)
        """
        if self.dynamic_config:
            return (
                self.dynamic_config.get("candles_5m_count", self.candles_5_count),
                self.dynamic_config.get("candles_15m_count", self.candles_15_count)
            )
        else:
            return (self.candles_5_count, self.candles_15_count)

    @model_validator(mode="after")
    def _post_init_checks(cls, m: "AgentConfig") -> "AgentConfig":
        missing = []
        if "candles_5_count" not in m.__pydantic_fields_set__:
            missing.append("candles_5_count")
        if "candles_15_count" not in m.__pydantic_fields_set__:
            missing.append("candles_15_count")
        if missing:
            raise ValueError("missing required field(s): " + ", ".join(missing))
        return m


__all__ = [
    "AgentConfig",
    "BrokerConfigMt5",
    "GptCallConfig",
    "PromptConfig",
    "GptConfig",
    "RiskConfig",
    "InstrumentConfig",
    "FeatureFlagsConfig",
    "AdminConfig",
    "UiPrefsConfig",
    "BrokerType",
    "Mode",
    "ModelConfig",
    "GptModel",
    "ExpiryRule",
    "Theme",
    "Env",
]
