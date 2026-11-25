"""Feature flag utilities.

This module provides a minimal interface for reading boolean feature flags
from environment variables. Flags default to ``False`` if unset.
"""

from __future__ import annotations

import os
from typing import Dict

# List of supported feature flag environment variables
_FLAG_NAMES = [
    "LIVE_TRADING",
    "USE_MT5",
    "USE_GPT",
]


def _load_flags() -> Dict[str, bool]:
    """Load feature flags from environment variables."""
    flags: Dict[str, bool] = {}
    for name in _FLAG_NAMES:
        value = os.getenv(name, "")
        flags[name] = value.lower() in {"1", "true", "yes", "y"}
    return flags


# Flags are loaded once at import time
FLAGS = _load_flags()


def is_enabled(name: str) -> bool:
    """Return ``True`` if the named feature flag is enabled.

    Parameters
    ----------
    name: str
        Name of the feature flag. Case-insensitive.
    """
    return FLAGS.get(name.upper(), False)
