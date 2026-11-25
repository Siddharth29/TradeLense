"""Candle representation adapter, separate from agent lifecycle and execution.

Preserves the runner's existing coercion/fallback conventions. Validation belongs
in the candle contract, not in this representation-only adapter.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, Dict

import pandas as pd

CandleConverter = Callable[[Any], Dict[str, Any]]
FrameConverter = Callable[[list], pd.DataFrame]


class CandleDataAdapter:
    """Convert candle representations with injected logging and serialization.

    Callbacks retain the runner's overridden conversion methods during batching
    and JSON conversion; this adapter owns no trade or scheduling state.
    """

    def __init__(self, *, logger: Any, error_reporter: Callable[..., Any],
                 log_context: dict[str, Any], serializer: Callable[..., Any],
                 datetime_type: type[datetime] = datetime) -> None:
        self._logger = logger
        self._error_reporter = error_reporter
        self._log_context = log_context
        self._serializer = serializer
        self._datetime = datetime_type

    def to_dict(self, candle: Any) -> Dict[str, Any]:
        """Convert a candle object to dictionary format for trigger checks."""
        try:
            # CRITICAL FIX: Ensure all numeric fields are converted to float
            # This prevents str-str arithmetic errors in generated code
            def safe_float(value, default=0.0):
                if value is None:
                    return default
                try:
                    if isinstance(value, str):
                        return float(value.strip('"\' '))
                    return float(value)
                except (ValueError, TypeError):
                    return default

            return {
                "time": candle.time.isoformat() if hasattr(candle.time, 'isoformat') else str(candle.time),
                "open": safe_float(candle.open),
                "high": safe_float(candle.high),
                "low": safe_float(candle.low),
                "close": safe_float(candle.close),
                "volume": safe_float(candle.volume) if hasattr(candle, 'volume') else 0.0,
            }
        except Exception as e:
            self._logger.error(f"Failed to convert candle to dict: {e}")
            return {
                "time": self._datetime.now(timezone.utc).isoformat(),
                "open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0, "volume": 0.0
            }

    def to_dataframe(self, c5_raw: list) -> pd.DataFrame:
        """Convert raw candle data to pandas DataFrame for historical analysis."""

        if not c5_raw:
            return pd.DataFrame()

        try:
            # Convert to list of dictionaries
            candle_data = []
            for candle in c5_raw:
                if hasattr(candle, '__dict__'):
                    # Object with attributes
                    candle_dict = {
                        'time': getattr(candle, 'time', None),
                        'open': float(getattr(candle, 'open', 0)),
                        'high': float(getattr(candle, 'high', 0)),
                        'low': float(getattr(candle, 'low', 0)),
                        'close': float(getattr(candle, 'close', 0)),
                        'volume': float(getattr(candle, 'volume', 0))
                    }
                elif isinstance(candle, dict):
                    # Dictionary
                    candle_dict = {
                        'time': candle.get('time'),
                        'open': float(candle.get('open', 0)),
                        'high': float(candle.get('high', 0)),
                        'low': float(candle.get('low', 0)),
                        'close': float(candle.get('close', 0)),
                        'volume': float(candle.get('volume', 0))
                    }
                else:
                    continue

                candle_data.append(candle_dict)

            if not candle_data:
                return pd.DataFrame()

            # Create DataFrame
            df = pd.DataFrame(candle_data)

            # Set time as index if available
            if 'time' in df.columns and not df['time'].isna().all():
                df['time'] = pd.to_datetime(df['time'])
                df.set_index('time', inplace=True)

            return df

        except Exception as e:
            self._error_reporter(
                self._logger.error,
                self._log_context,
                "Error converting candles to DataFrame",
                error=str(e)
            )
            return pd.DataFrame()

    def to_batch(self, c5_raw: list, c15_raw: list, *, to_dict: CandleConverter, to_dataframe: FrameConverter) -> tuple:
        """Convert candles once, minimizing work:
        - Reuse dict arrays if already provided by candle service
        - Build DataFrame only for 5m (needed downstream)
        - Serialize JSON once with fast json.dumps
        """
        try:
            # Reuse dict arrays when possible to avoid per-item conversion
            if c5_raw and isinstance(c5_raw[0], dict):
                c5_dicts = c5_raw
            else:
                c5_dicts = [to_dict(c) for c in (c5_raw or [])]

            if c15_raw and isinstance(c15_raw[0], dict):
                c15_dicts = c15_raw
            else:
                c15_dicts = [to_dict(c) for c in (c15_raw or [])]

            # Convert to DataFrame (only 5m for historical analysis)
            c5_df = to_dataframe(c5_dicts) if c5_dicts else pd.DataFrame()

            # Serialize JSON once; input may already be dict arrays
            c5_json = json.dumps(c5_dicts, default=self._serializer, separators=(",", ":")) if c5_dicts else "[]"
            c15_json = json.dumps(c15_dicts, default=self._serializer, separators=(",", ":")) if c15_dicts else "[]"

            return c5_dicts, c15_dicts, c5_df, c5_json, c15_json

        except Exception as e:
            self._error_reporter(
                self._logger.error,
                self._log_context,
                "Error in batch candle conversion",
                error=str(e)
            )
            # Return empty defaults
            return [], [], pd.DataFrame(), "[]", "[]"

    def to_json(self, data: Any, *, to_dict: CandleConverter) -> str:
        """Safely convert candle data (lists/DF/dicts) to JSON.
        Supports array inputs from candle service and falls back gracefully.
        """
        if isinstance(data, str):
            return data

        # Try DataFrame serialization via helper if available
        try:  # pragma: no cover - defensive imports
            import pandas as pd  # type: ignore
            from packages.core.utils import candles_to_json  # type: ignore
            if isinstance(data, pd.DataFrame):
                return candles_to_json(data)
        except Exception:
            pass

        # Normalize to list of dictionaries
        items: list[dict[str, Any]] = []
        if isinstance(data, list):
            for c in data:
                if isinstance(c, dict):
                    items.append(c)
                else:
                    items.append(to_dict(c))
        elif isinstance(data, dict):
            items = [data]
        else:
            # Unknown type: try best-effort conversion
            try:
                items = [to_dict(data)]
            except Exception:
                items = []

        # Serialize using safe JSON dumper when available
        # Directly use json.dumps; keep safe_json_dumps as a fast path if available
        try:
            return json.dumps(items, default=self._serializer, separators=(",", ":"))
        except Exception:
            return "[]"
