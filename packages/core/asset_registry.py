"""Single source of truth for per-asset (instrument) configuration.

Historically, per-asset data was scattered across ~9 modules (assets enum,
contract specs, several ``config.py`` guard dicts, session hours, candle gap
sets, monitor close windows, adapter maintenance windows, prompt one-liners,
frontend).  This module centralizes the *instrument facts* for every supported
symbol into one ``ASSET_PROFILES`` registry, and exposes small builders so the
existing structures can be **derived** from it without changing any consumer.

Design rules:
- **Dependency-free.**  This module imports nothing from the project so any
  module (``config.py``, ``risk/specs_mt5.py``, adapters, …) can import it with
  no circular-import risk.
- **Instrument facts only.**  Behavioral feature-flags (HTF context, ATR stops,
  counter-trend, judge CONTEXT toggles) stay in ``config.py`` and are resolved
  per-asset via ``config.resolve_for_asset``.  This registry does NOT own them.
- **Suffix/alias tolerant.**  Live demo symbols carry a broker suffix (e.g.
  ``BTCUSDm``); legacy crypto names (``BTCUSDT``) are modelled as ``aliases``.

A field set to ``None`` means "no explicit per-asset value — fall back to the
dict's ``default``", which lets us reproduce today's (intentionally
inconsistent) config exactly while still centralizing it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ── Default guard values (the ``"default"`` key of each derived dict) ─────────
# These mirror the literals that were previously hardcoded in config.py so the
# derived dicts behave identically for any symbol not explicitly listed.
DEFAULT_MIN_SL_DISTANCE = 20
DEFAULT_MIN_TARGET_DISTANCE = 30
DEFAULT_MAX_SL_DISTANCE = 1000.0
DEFAULT_MAX_ENTRY_DRIFT: Optional[float] = None
DEFAULT_INSTRUMENT_CONFIG = {"pip_size": 1.0, "pip_value_per_lot": 1.0, "default_lot": 1.0}
DEFAULT_FILTER_MIN_TARGET_POINTS = 100
DEFAULT_FILTER_SLIPPAGE = 1

# Standard broker daily maintenance / rollover window (UTC), used by the monitor
# to sleep instead of burning retries.  Format: (close_h, close_m, open_h, open_m).
STANDARD_MAINTENANCE_WINDOW = (20, 55, 22, 5)


@dataclass(frozen=True)
class AssetProfile:
    """All per-instrument facts for one tradable symbol."""

    # --- identity / classification (required) ---
    symbol: str
    category: str            # "crypto" | "forex" | "metal" | "energy" | "index"
    session: str             # "crypto_24_7" | "weekday_24_5" | "index"
    has_weekend_gap: bool
    base_currency: str
    quote_currency: str

    # --- contract / lot sizing (drives calc_lot_size_by_risk) ---
    contract_size: float

    # --- backtest PnL math ---
    pip_size: float
    pip_value_per_lot: float

    # --- anti-scalp / risk guards, in PRICE units (None ⇒ use dict default) ---
    min_sl_distance: Optional[float]
    min_target_distance: Optional[float]
    max_sl_distance: Optional[float]
    max_entry_drift: Optional[float]
    filter_min_target_points: Optional[float]
    filter_slippage: Optional[float]

    # --- defaulted ---
    lot_step: float = 0.01
    min_lot: float = 0.01
    max_lot: float = 100.0
    default_lot: float = 1.0
    aliases: tuple = ()                 # legacy / equivalent symbol names
    maintenance_window: tuple = STANDARD_MAINTENANCE_WINDOW   # monitor close window
    gap_maintenance_window: Optional[tuple] = None  # wider window for the candle gap estimator (metals)
    daily_break: Optional[tuple] = None  # extra intraday break for indices (close_h,close_m,open_h,open_m UTC)
    one_liner: str = "Trading instrument under analysis."


# ═══════════════════════════════════════════════════════════════════════════
# THE REGISTRY — keys are the clean (suffix-free, upper-case) symbol id.
# ═══════════════════════════════════════════════════════════════════════════
ASSET_PROFILES: dict[str, AssetProfile] = {
    "BTCUSD": AssetProfile(
        symbol="BTCUSD", category="crypto", session="crypto_24_7", has_weekend_gap=False,
        base_currency="BTC", quote_currency="USD", contract_size=1.0,
        pip_size=1.0, pip_value_per_lot=1.0,
        min_sl_distance=100, min_target_distance=200, max_sl_distance=2000.0,
        max_entry_drift=200.0, filter_min_target_points=100, filter_slippage=2,
        aliases=("BTCUSDT",),
        one_liner="Bitcoin priced in USD.",
    ),
    "ETHUSD": AssetProfile(
        symbol="ETHUSD", category="crypto", session="crypto_24_7", has_weekend_gap=False,
        base_currency="ETH", quote_currency="USD", contract_size=1.0,
        pip_size=1.0, pip_value_per_lot=1.0,
        min_sl_distance=5, min_target_distance=10, max_sl_distance=150.0,
        max_entry_drift=10.0, filter_min_target_points=10, filter_slippage=1,
        min_lot=0.1,  # Exness real-account vol_min for ETHUSD (verified 2026-06-09)
        aliases=("ETHUSDT",),
        one_liner="Ethereum priced in USD.",
    ),
    "XAUUSD": AssetProfile(
        symbol="XAUUSD", category="metal", session="weekday_24_5", has_weekend_gap=True,
        base_currency="XAU", quote_currency="USD", contract_size=100.0,
        pip_size=1.0, pip_value_per_lot=1.0,
        min_sl_distance=1, min_target_distance=2, max_sl_distance=50.0,
        max_entry_drift=5.0, filter_min_target_points=5, filter_slippage=1,
        gap_maintenance_window=(20, 30, 23, 30),
        one_liner="Spot gold priced in USD.",
    ),
    "XAGUSD": AssetProfile(
        symbol="XAGUSD", category="metal", session="weekday_24_5", has_weekend_gap=True,
        base_currency="XAG", quote_currency="USD", contract_size=5000.0,
        pip_size=1.0, pip_value_per_lot=1.0,
        min_sl_distance=0.03, min_target_distance=0.06, max_sl_distance=2.0,
        max_entry_drift=0.10, filter_min_target_points=0.05, filter_slippage=0.02,
        gap_maintenance_window=(20, 30, 23, 30),
        one_liner="Spot silver priced in USD.",
    ),
    "GBPUSD": AssetProfile(
        symbol="GBPUSD", category="forex", session="weekday_24_5", has_weekend_gap=True,
        base_currency="GBP", quote_currency="USD", contract_size=100000.0,
        pip_size=0.0001, pip_value_per_lot=10.0,
        min_sl_distance=0.0005, min_target_distance=0.001, max_sl_distance=0.01,
        max_entry_drift=0.0005, filter_min_target_points=0.001, filter_slippage=0.0001,
        one_liner="Forex major pair, British pound vs US dollar.",
    ),
    "EURUSD": AssetProfile(
        symbol="EURUSD", category="forex", session="weekday_24_5", has_weekend_gap=True,
        base_currency="EUR", quote_currency="USD", contract_size=100000.0,
        pip_size=0.0001, pip_value_per_lot=10.0,
        min_sl_distance=0.0005, min_target_distance=0.001, max_sl_distance=0.01,
        max_entry_drift=0.0005, filter_min_target_points=0.001, filter_slippage=0.0001,
        one_liner="Forex major pair, euro vs US dollar.",
    ),
    # ── Onboarded MT5 assets ─────────────────────────────────────────────
    # contract_size / vol_min / vol_max and USTEC's session windows were
    # confirmed against the live Exness ``mt5.symbol_info()`` (real account,
    # 2026-06-09). Empirically: USTEC trades ~24/5 with a daily break
    # 20:55-22:00 UTC, closes Friday ~20:55 UTC, reopens Sunday 22:00 UTC.
    "USOIL": AssetProfile(
        symbol="USOIL", category="energy", session="weekday_24_5", has_weekend_gap=True,
        base_currency="USD", quote_currency="USD", contract_size=1000.0,  # verified: 1 lot = 1000 bbl
        pip_size=1.0, pip_value_per_lot=1.0,
        min_sl_distance=0.10, min_target_distance=0.20, max_sl_distance=5.0,
        max_entry_drift=0.25, filter_min_target_points=0.2, filter_slippage=0.02,
        max_lot=50.0,  # broker vol_max for USOIL
        one_liner="WTI crude oil CFD priced in USD.",
    ),
    "USTEC": AssetProfile(
        symbol="USTEC", category="index", session="index", has_weekend_gap=True,
        base_currency="USD", quote_currency="USD", contract_size=1.0,  # verified: 1 lot, $1/point
        pip_size=1.0, pip_value_per_lot=1.0,
        min_sl_distance=10, min_target_distance=20, max_sl_distance=500.0,
        max_entry_drift=30.0, filter_min_target_points=20, filter_slippage=1,
        maintenance_window=(20, 55, 22, 5),       # monitor close window (standard)
        gap_maintenance_window=(20, 30, 23, 0),   # wide window for the candle gap estimator (DST-tolerant)
        daily_break=(20, 55, 22, 0),              # is_session_open index break — verified 20:55-22:00 UTC
        one_liner="US Tech 100 (NASDAQ-100) index CFD priced in USD.",
    ),
}


# ── Normalization & lookup helpers ───────────────────────────────────────────
def _clean(symbol: str | None) -> str:
    """Upper-case and strip the broker demo ``m`` suffix (e.g. BTCUSDm→BTCUSD)."""
    key = (symbol or "").upper().strip()
    if key and key not in ASSET_PROFILES and key.endswith("M"):
        key = key[:-1]
    return key


def profile_for(symbol: str | None) -> Optional[AssetProfile]:
    """Return the :class:`AssetProfile` for *symbol* (suffix/alias tolerant)."""
    key = _clean(symbol)
    if key in ASSET_PROFILES:
        return ASSET_PROFILES[key]
    for p in ASSET_PROFILES.values():
        if key in p.aliases:
            return p
    return None


def category_of(symbol: str | None) -> Optional[str]:
    p = profile_for(symbol)
    return p.category if p else None


def is_crypto(symbol: str | None) -> bool:
    """Mirror the legacy crypto check (strips ``M.RAW#`` suffixes)."""
    base = (symbol or "").upper().rstrip("M.RAW#")
    return base in CRYPTO_SYMBOLS


# ── Derived collections (consumed by candles / monitor / adapter) ────────────
def _with_aliases(symbols: dict[str, AssetProfile]) -> set[str]:
    out: set[str] = set()
    for sym, p in symbols.items():
        out.add(sym)
        out.update(p.aliases)
    return out


#: Crypto symbols incl. legacy aliases — equals the old ``_24_7_ASSETS``.
CRYPTO_SYMBOLS: frozenset = frozenset(
    _with_aliases({s: p for s, p in ASSET_PROFILES.items() if p.category == "crypto"})
)

#: Every symbol that observes the broker daily maintenance — old ``_BROKER_MAINTENANCE_ASSETS``.
MAINTENANCE_SYMBOLS: frozenset = frozenset(_with_aliases(ASSET_PROFILES))

#: Symbols with a full weekend closure — old ``_WEEKEND_CLOSE_SYMBOLS``.
WEEKEND_CLOSE_SYMBOLS: frozenset = frozenset(
    s for s, p in ASSET_PROFILES.items() if p.has_weekend_gap
)


# ── Builders for the config.py guard dicts ───────────────────────────────────
def guard_dict(field_name: str, default, *, include_aliases: bool = True) -> dict:
    """Build a ``{SYMBOL: value, ..., "default": default}`` dict from a profile field.

    Symbols whose field value is ``None`` are omitted so they fall back to the
    ``"default"`` key (reproducing the historical per-asset gaps).
    """
    out: dict = {}
    for sym, p in ASSET_PROFILES.items():
        val = getattr(p, field_name)
        if val is None:
            continue
        out[sym] = val
        if include_aliases:
            for a in p.aliases:
                out[a] = val
    out["default"] = default
    return out


def instrument_config_dict() -> dict:
    """Build ``BACKTEST_INSTRUMENT_CONFIG`` (pip_size/pip_value_per_lot/default_lot)."""
    out: dict = {}
    for sym, p in ASSET_PROFILES.items():
        entry = {
            "pip_size": p.pip_size,
            "pip_value_per_lot": p.pip_value_per_lot,
            "default_lot": p.default_lot,
        }
        out[sym] = entry
        for a in p.aliases:
            out[a] = entry
    out["default"] = dict(DEFAULT_INSTRUMENT_CONFIG)
    return out


def market_close_windows() -> dict:
    """Build ``MARKET_CLOSE_WINDOWS`` ({symbol: [window]}) for the monitor."""
    return {sym: [p.maintenance_window] for sym, p in ASSET_PROFILES.items()}


def daily_maintenance_windows() -> dict:
    """Build the adapter's wider gap-estimator windows (metals only today)."""
    return {
        sym: p.gap_maintenance_window
        for sym, p in ASSET_PROFILES.items()
        if p.gap_maintenance_window is not None
    }


__all__ = [
    "AssetProfile",
    "ASSET_PROFILES",
    "STANDARD_MAINTENANCE_WINDOW",
    "DEFAULT_MIN_SL_DISTANCE",
    "DEFAULT_MIN_TARGET_DISTANCE",
    "DEFAULT_MAX_SL_DISTANCE",
    "DEFAULT_MAX_ENTRY_DRIFT",
    "DEFAULT_INSTRUMENT_CONFIG",
    "DEFAULT_FILTER_MIN_TARGET_POINTS",
    "DEFAULT_FILTER_SLIPPAGE",
    "profile_for",
    "category_of",
    "is_crypto",
    "CRYPTO_SYMBOLS",
    "MAINTENANCE_SYMBOLS",
    "WEEKEND_CLOSE_SYMBOLS",
    "guard_dict",
    "instrument_config_dict",
    "market_close_windows",
    "daily_maintenance_windows",
]
