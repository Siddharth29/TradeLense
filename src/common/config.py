from __future__ import annotations

"""Application configuration utilities.

Provides a typed configuration object backed by environment variables and an
optional ``.env`` file. Settings are grouped into logical sections for
brokers, GPT, logging and risk management. Use :func:`Config.get` to retrieve a
singleton instance of the configuration.
"""

from pathlib import Path
from typing import ClassVar, Optional, Dict, Any

from pydantic import BaseModel, Field, ConfigDict


class BrokerConfig(BaseModel):
    """Broker related settings."""

    live_trading: bool = Field(False, description="Enable live trading")


class GPTConfig(BaseModel):
    """GPT / Gemini model configuration."""

    api_key: Optional[str] = Field(default=None, description="OpenAI API key")
    gemini_api_key: Optional[str] = Field(default=None, description="Google Gemini API key")
    provider: str = Field(default="openai", description="AI provider: openai or gemini")
    model_3: str = Field("gpt-3.5-turbo", description="Default GPT-3 model")
    model_4: str = Field("gpt-4o", description="Default GPT-4 model")
    gemini_model: str = Field("gemini-2.5-flash", description="Default Gemini model")
    
    model_config = ConfigDict(protected_namespaces=())


class LoggingConfig(BaseModel):
    """Logging configuration."""

    level: str = Field("INFO", description="Logging level")
    path: Path = Field(Path("app.log"), description="Log file path")


class RiskConfig(BaseModel):
    """Risk management parameters."""

    max_drawdown: float = Field(0.2, description="Maximum portfolio drawdown")
    risk_per_trade: float = Field(0.01, description="Fraction of capital at risk per trade")


class Config(BaseModel):
    """Application configuration container."""

    brokers: BrokerConfig = Field(default_factory=BrokerConfig)
    gpt: GPTConfig = Field(default_factory=GPTConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)

    model_config = ConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="allow"
    )

    _instance: ClassVar[Optional["Config"]] = None

    @classmethod
    def get(cls) -> "Config":
        """Return a cached configuration instance."""
        if cls._instance is None:
            cls._instance = cls.model_validate({})
            # Load environment variables
            import os
            env_vars = {}
            for key, value in os.environ.items():
                if "__" in key:
                    section, prop = key.lower().split("__", 1)
                    if hasattr(cls._instance, section):
                        section_dict = env_vars.setdefault(section, {})
                        section_dict[prop] = value
            
            # Apply environment variables to the config
            for section, props in env_vars.items():
                if hasattr(cls._instance, section):
                    section_obj = getattr(cls._instance, section)
                    for prop, value in props.items():
                        if hasattr(section_obj, prop):
                            # Convert value to the correct type
                            field_type = type(getattr(section_obj, prop))
                            if field_type is bool:
                                value = value.lower() in ("true", "1", "yes")
                            elif field_type is float:
                                value = float(value)
                            elif field_type is int:
                                value = int(value)
                            elif field_type is Path:
                                value = Path(value)
                            setattr(section_obj, prop, value)
                    
        return cls._instance


__all__ = [
    "BrokerConfig",
    "GPTConfig",
    "LoggingConfig",
    "RiskConfig",
    "Config",
]
