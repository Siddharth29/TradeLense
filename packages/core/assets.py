from enum import Enum


class Asset(str, Enum):
    """Supported MT5 trading assets.

    Kept in sync with ``packages.core.asset_registry.ASSET_PROFILES`` (see the
    sync test in ``tests/test_asset_registry.py``).
    """

    XAUUSD = "XAUUSD"
    XAGUSD = "XAGUSD"
    BTCUSD = "BTCUSD"
    ETHUSD = "ETHUSD"
    GBPUSD = "GBPUSD"
    EURUSD = "EURUSD"
    USOIL = "USOIL"
    USTEC = "USTEC"


# Live MT5 symbols are identical to the broker instrument key.
INSTRUMENT_KEY = {a: a.value for a in Asset}

__all__ = ["Asset", "INSTRUMENT_KEY"]
