"""Unit tests for trade schema validation."""

import pytest
from pydantic import ValidationError

from packages.core.schemas_trade import MultiExpertSignal, TRADE_JSON_SCHEMA


class TestMultiExpertSignal:
    """Test MultiExpertSignal pydantic model validation."""

    def test_valid_buy_signal(self):
        """Test valid buy signal creation."""
        data = {
            "signal": "buy",
            "entry": 100.0,
            "stop": 95.0,
            "target": 110.0,
            "ttl_minutes": 90,
            "confidence": 0.8,
        }
        signal = MultiExpertSignal(**data)
        assert signal.signal == "buy"
        assert signal.entry == 100.0
        assert signal.stop == 95.0
        assert signal.target == 110.0
        assert signal.ttl_minutes == 90
        assert signal.confidence == 0.8
        assert signal.active_window_c5 is None
        assert signal.active_window_c15 is None
        assert signal.rationale is None

    def test_valid_buy_signal_with_legacy_fields(self):
        """Test that legacy active_window fields are accepted as optional."""
        data = {
            "signal": "buy",
            "entry": 100.0,
            "stop": 95.0,
            "target": 110.0,
            "ttl_minutes": 90,
            "confidence": 0.8,
            "active_window_c5": 90,
            "active_window_c15": 40,
        }
        signal = MultiExpertSignal(**data)
        assert signal.active_window_c5 == 90
        assert signal.active_window_c15 == 40

    def test_valid_sell_signal(self):
        """Test valid sell signal creation."""
        data = {
            "signal": "sell",
            "entry": 100.0,
            "stop": 105.0,
            "target": 90.0,
            "ttl_minutes": 180,
            "confidence": 0.7,
            "rationale": "Strong resistance break"
        }
        signal = MultiExpertSignal(**data)
        assert signal.signal == "sell"
        assert signal.rationale == "Strong resistance break"

    def test_valid_flat_signal(self):
        """Test valid flat signal creation (no entry/stop/target needed)."""
        data = {
            "signal": "flat",
            "ttl_minutes": 90,
            "confidence": 0.3,
        }
        signal = MultiExpertSignal(**data)
        assert signal.signal == "flat"
        assert signal.entry is None
        assert signal.stop is None
        assert signal.target is None

    def test_invalid_signal_type(self):
        """Test rejection of invalid signal types."""
        data = {
            "signal": "hold",
            "ttl_minutes": 90,
            "confidence": 0.8,
        }
        with pytest.raises(ValidationError) as exc_info:
            MultiExpertSignal(**data)
        assert "signal" in str(exc_info.value)

    def test_invalid_ttl_minutes(self):
        """Test rejection of invalid TTL values."""
        data = {
            "signal": "buy",
            "entry": 100.0,
            "stop": 95.0,
            "target": 110.0,
            "ttl_minutes": 75,
            "confidence": 0.8,
        }
        with pytest.raises(ValidationError) as exc_info:
            MultiExpertSignal(**data)
        assert "ttl_minutes" in str(exc_info.value)

    def test_invalid_confidence_range(self):
        """Test rejection of confidence values outside [0,1] range."""
        data = {
            "signal": "buy",
            "entry": 100.0,
            "stop": 95.0,
            "target": 110.0,
            "ttl_minutes": 90,
            "confidence": 1.5,
        }
        with pytest.raises(ValidationError) as exc_info:
            MultiExpertSignal(**data)
        assert "confidence" in str(exc_info.value)

        data["confidence"] = -0.1
        with pytest.raises(ValidationError):
            MultiExpertSignal(**data)

    def test_extra_fields_ignored(self):
        """Extra LLM fields are silently dropped (extra='ignore' by design —
        rejecting whole proposals over a stray field cost real trades)."""
        data = {
            "signal": "buy",
            "entry": 100.0,
            "stop": 95.0,
            "target": 110.0,
            "ttl_minutes": 90,
            "confidence": 0.8,
            "extra_field": "not_allowed"
        }
        sig = MultiExpertSignal(**data)
        assert sig.signal == "buy"
        assert not hasattr(sig, "extra_field")

    def test_missing_required_fields(self):
        """Test rejection when required fields are missing."""
        data = {
            "signal": "buy",
            "entry": 100.0,
        }
        with pytest.raises(ValidationError) as exc_info:
            MultiExpertSignal(**data)
        error_str = str(exc_info.value)
        assert "ttl_minutes" in error_str
        assert "confidence" in error_str

    def test_non_flat_requires_prices(self):
        """Non-flat signals must have entry, stop, and target."""
        with pytest.raises(ValidationError, match="requires entry, stop, and target"):
            MultiExpertSignal(
                signal="buy",
                entry=None,
                stop=100.0,
                target=110.0,
                ttl_minutes=90,
                confidence=0.8,
            )


class TestTradeJsonSchema:
    """Test TRADE_JSON_SCHEMA structure and completeness."""

    def test_schema_structure(self):
        """Test that JSON schema has correct structure."""
        assert isinstance(TRADE_JSON_SCHEMA, dict)
        assert TRADE_JSON_SCHEMA["type"] == "object"
        assert "properties" in TRADE_JSON_SCHEMA
        assert "required" in TRADE_JSON_SCHEMA
        assert TRADE_JSON_SCHEMA["additionalProperties"] is False

    def test_required_fields_match_model(self):
        """Test that required fields in JSON schema are the minimal set."""
        expected_required = ["signal", "ttl_minutes", "confidence"]
        assert set(TRADE_JSON_SCHEMA["required"]) == set(expected_required)

    def test_signal_enum_values(self):
        """Test signal enum values match model."""
        signal_prop = TRADE_JSON_SCHEMA["properties"]["signal"]
        assert signal_prop["type"] == "string"
        assert set(signal_prop["enum"]) == {"buy", "sell", "flat"}

    def test_ttl_enum_values(self):
        """Test TTL enum values match model."""
        ttl_prop = TRADE_JSON_SCHEMA["properties"]["ttl_minutes"]
        assert ttl_prop["type"] == "integer"
        # Pinned to config, not a literal: the enum moved on 2026-08-06 and a
        # hardcoded copy here is exactly the drift the change was fixing.
        from packages.core.config import TTL_MINUTES_ALLOWED
        assert set(ttl_prop["enum"]) == set(TTL_MINUTES_ALLOWED)

    def test_confidence_range(self):
        """Test confidence range constraints."""
        confidence_prop = TRADE_JSON_SCHEMA["properties"]["confidence"]
        assert confidence_prop["type"] == "number"
        assert confidence_prop["minimum"] == 0
        assert confidence_prop["maximum"] == 1

    def test_optional_fields_nullable(self):
        """Test that optional fields allow null values."""
        for field in ["entry", "stop", "target", "rationale"]:
            field_prop = TRADE_JSON_SCHEMA["properties"][field]
            assert "null" in field_prop["type"], f"{field} should allow null"

    def test_import_works(self):
        """Test that schema can be imported successfully."""
        from packages.core.schemas_trade import TRADE_JSON_SCHEMA
        assert TRADE_JSON_SCHEMA is not None
        assert isinstance(TRADE_JSON_SCHEMA, dict)
