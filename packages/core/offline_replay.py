"""Offline application mode."""

import os


def offline_mode() -> bool:
    """Keep external services disabled unless explicitly configured."""
    return os.getenv("TRADELENSE_OFFLINE", "true").strip().lower() != "false"
