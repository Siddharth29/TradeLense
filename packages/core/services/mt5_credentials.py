"""Local MT5 credential cache (plaintext, single-user local box).

Single source of truth for the cached MT5 login that the dashboard router
auto-connects with on startup and keeps alive (see
``apps/backend/routers/mt5.py`` and the FastAPI startup handler in
``apps/backend/api.py``).

Credential files under ``<repo>/config/`` match the schema that
``mt5_historical_fetcher._load_mt5_config`` already reads::

    {"mt5": {"server": "...", "login": "...", "password": "...",
             "allow_live": false}}

* ``mt5_config.local.json`` — optional operator credentials. **Gitignored.**
  Preferred over the example config and the only file this module ever writes.
* ``mt5_config.json`` — committed placeholder configuration. Its credential
  fields are empty in the public repository.
* ``mt5_config.demo.local.json`` — gitignored override for explicit Demo mode.

Plaintext on disk is an explicit, accepted choice for a single-user local
machine (per the production decision). Every function fails soft — a missing or
malformed file yields ``None``/``False`` and never raises into the caller.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"
# Operator credentials (gitignored). Preferred for read; the only file written.
_LOCAL_PATH = _CONFIG_DIR / "mt5_config.local.json"
_DEMO_LOCAL_PATH = _CONFIG_DIR / "mt5_config.demo.local.json"
# Committed placeholder — read-only fallback when populated locally.
_DEFAULT_PATH = _CONFIG_DIR / "mt5_config.json"


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def selected_mt5_mode() -> str:
    """Return the launcher mode, or an empty string for the legacy startup path."""
    return os.getenv("TRADELENS_MT5_MODE", "").strip().lower()


def load_mt5_credentials() -> Optional[Dict[str, Any]]:
    """Return cached ``{server, login, password, allow_live}`` or ``None``.

    An explicit launcher mode selects one file and checks its server label.
    Unset mode retains the local override and default fallback.
    """
    mode = selected_mt5_mode()
    if mode == "demo":
        path = _DEMO_LOCAL_PATH if _DEMO_LOCAL_PATH.exists() else _DEFAULT_PATH
        data = _read_json(path) or {}
    elif mode == "live":
        data = _read_json(_LOCAL_PATH) or {}
    elif not mode:
        data = _read_json(_LOCAL_PATH) or _read_json(_DEFAULT_PATH) or {}
    else:
        return None
    mt5 = data.get("mt5")
    if not isinstance(mt5, dict):
        return None
    server = mt5.get("server")
    login = mt5.get("login") or mt5.get("loginId")
    password = mt5.get("password")
    if not (server and login and password):
        return None
    server = str(server)
    if mode:
        server_name = server.casefold()
        demo_server = "demo" in server_name or "trial" in server_name
        live_server = "real" in server_name or "live" in server_name
        if (mode == "demo" and (not demo_server or live_server)) or (
            mode == "live" and (not live_server or demo_server)
        ):
            return None
    return {
        "server": server,
        "login": str(login),
        "password": str(password),
        "allow_live": mode != "demo" and bool(mt5.get("allow_live", False)),
    }


def save_mt5_credentials(
    *, server: str, login: str, password: str, allow_live: Optional[bool] = None
) -> None:
    """Persist creds to the gitignored local override, preserving other keys.

    Writes ``mt5_config.local.json`` so real creds never reach a tracked file.
    ``allow_live`` is preserved unless explicitly passed (connecting must not
    silently arm real-money trading — that is a separate, deliberate switch).
    """
    data = _read_json(_LOCAL_PATH) or {}
    mt5 = data.get("mt5") if isinstance(data.get("mt5"), dict) else {}
    mt5.update({"server": str(server), "login": str(login), "password": str(password)})
    if allow_live is not None:
        mt5["allow_live"] = bool(allow_live)
    elif "allow_live" not in mt5:
        mt5["allow_live"] = False
    data["mt5"] = mt5
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(_LOCAL_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def has_cached_credentials() -> bool:
    """True if a usable credential set exists (local override or default)."""
    return load_mt5_credentials() is not None


def live_trading_allowed() -> bool:
    """True only if real-money trading is *explicitly armed*.

    Armed via the ``MT5_ALLOW_LIVE`` config flag (env) OR the local cred file's
    ``allow_live``. Defaults False so a crash/restart can never silently place
    real orders. This is the single gate every real write path consults.
    """
    if selected_mt5_mode() == "demo":
        return False
    try:
        from packages.core.config import MT5_ALLOW_LIVE
    except Exception:
        MT5_ALLOW_LIVE = False
    if MT5_ALLOW_LIVE:
        return True
    creds = load_mt5_credentials()
    return bool(creds and creds.get("allow_live"))


__all__ = [
    "selected_mt5_mode",
    "load_mt5_credentials",
    "save_mt5_credentials",
    "has_cached_credentials",
    "live_trading_allowed",
]
