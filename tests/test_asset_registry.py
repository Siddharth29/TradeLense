"""Regression tests for TradeLense demo limits and instrument contracts."""

import pytest

from packages.core import config as cfg
from packages.core.config import resolve_for_asset
from packages.core.risk.specs_mt5 import CONTRACT_SPECS
from packages.core.risk import calc_lot_size_by_risk
from packages.core.assets import Asset
from packages.core import asset_registry as ar


# Per-symbol limits are expressed in each instrument's price units.
DEMO_LIMITS = {
    # Symbol: min stop, min target, max stop, max entry drift, filter target, slippage
    "BTCUSD": (100, 200, 2000.0, 200.0, 100, 2),
    "ETHUSD": (5, 10, 150.0, 10.0, 10, 1),
    "XAUUSD": (1, 2, 50.0, 5.0, 5, 1),
    "XAGUSD": (0.03, 0.06, 2.0, 0.10, 0.05, 0.02),
    "GBPUSD": (0.0005, 0.001, 0.01, 0.0005, 0.001, 0.0001),
    "EURUSD": (0.0005, 0.001, 0.01, 0.0005, 0.001, 0.0001),
    "USOIL": (0.10, 0.20, 5.0, 0.25, 0.2, 0.02),
    "USTEC": (10, 20, 500.0, 30.0, 20, 1),
}
LEGACY_INSTRUMENT = {
    "BTCUSD": {"pip_size": 1.0, "pip_value_per_lot": 1.0, "default_lot": 1.0},
    "ETHUSD": {"pip_size": 1.0, "pip_value_per_lot": 1.0, "default_lot": 1.0},
    "XAUUSD": {"pip_size": 1.0, "pip_value_per_lot": 1.0, "default_lot": 1.0},
    "GBPUSD": {"pip_size": 0.0001, "pip_value_per_lot": 10.0, "default_lot": 1.0},
    "EURUSD": {"pip_size": 0.0001, "pip_value_per_lot": 10.0, "default_lot": 1.0},
    "default": {"pip_size": 1.0, "pip_value_per_lot": 1.0, "default_lot": 1.0},
}
# Legacy CONTRACT_SPECS contract sizes for carry-over assets.
LEGACY_CONTRACT_SIZE = {
    "BTCUSD": 1.0, "ETHUSD": 1.0, "GBPUSD": 100000.0,
    "XAUUSD": 100.0, "XAGUSD": 5000.0, "EURUSD": 100000.0,
}


@pytest.mark.parametrize("dict_name,column,default", [
    ("BACKTEST_MIN_SL_DISTANCE", 0, 20),
    ("BACKTEST_MIN_TARGET_DISTANCE", 1, 30),
    ("BACKTEST_MAX_SL_DISTANCE", 2, 1000.0),
    ("BACKTEST_MAX_ENTRY_DRIFT", 3, None),
    ("TRADING_FILTER_MIN_TARGET_POINTS", 4, 100),
    ("TRADING_FILTER_SLIPPAGE", 5, 1),
])
def test_derived_guards_match_demo_limits(dict_name, column, default):
    derived = getattr(cfg, dict_name)
    assert derived["default"] == default
    for symbol, limits in DEMO_LIMITS.items():
        assert derived[symbol] == limits[column]
    for alias, symbol in (("BTCUSDT", "BTCUSD"), ("ETHUSDT", "ETHUSD")):
        assert derived[alias] == derived[symbol]
        assert resolve_for_asset(derived, alias.lower() + "m") == derived[symbol]


def test_derived_instrument_config_preserves_contract_units():
    for symbol, expected in LEGACY_INSTRUMENT.items():
        assert cfg.BACKTEST_INSTRUMENT_CONFIG[symbol] == expected


def test_eurusd_filter_uses_explicit_demo_settings():
    assert resolve_for_asset(cfg.TRADING_FILTER_MIN_TARGET_POINTS, "eurusdm") == 0.001
    assert resolve_for_asset(cfg.TRADING_FILTER_SLIPPAGE, "eurusdm") == 0.0001


def test_contract_specs_carryover_unchanged():
    for sym, size in LEGACY_CONTRACT_SIZE.items():
        assert sym in CONTRACT_SPECS, f"{sym} missing from CONTRACT_SPECS"
        assert CONTRACT_SPECS[sym].contract_size == size


def test_unwanted_symbols_pruned_from_contract_specs():
    """USDJPY/GBPJPY were unexposed and are intentionally pruned."""
    assert "USDJPY" not in CONTRACT_SPECS
    assert "GBPJPY" not in CONTRACT_SPECS


def test_crypto_classification_and_suffix_tolerance():
    assert ar.is_crypto("BTCUSD") is True
    assert ar.is_crypto("BTCUSDm") is True       # demo suffix
    assert ar.is_crypto("ETHUSDT") is True        # legacy alias
    assert ar.is_crypto("XAUUSD") is False
    assert ar.is_crypto("EURUSD") is False
    # Old _24_7_ASSETS set is reproduced (incl. aliases)
    assert ar.CRYPTO_SYMBOLS == frozenset({"BTCUSD", "BTCUSDT", "ETHUSD", "ETHUSDT"})


def test_profile_lookup_suffix_tolerant():
    assert ar.profile_for("XAUUSDm").symbol == "XAUUSD"
    assert ar.profile_for("gbpusd").symbol == "GBPUSD"
    assert ar.profile_for("BTCUSDT").symbol == "BTCUSD"  # alias
    assert ar.profile_for("UNKNOWN") is None


# ── Onboarding: roster + new assets ──────────────────────────────────────────
def test_enum_matches_registry():
    """The Asset enum and the registry must stay in lock-step."""
    assert {a.value for a in Asset} == set(ar.ASSET_PROFILES)


def test_full_roster():
    assert set(ar.ASSET_PROFILES) == {
        "BTCUSD", "ETHUSD", "XAUUSD", "XAGUSD",
        "GBPUSD", "EURUSD", "USOIL", "USTEC",
    }


@pytest.mark.parametrize("sym", ["XAGUSD", "USOIL", "USTEC"])
def test_new_assets_fully_wired(sym):
    # contract specs present (so live lot sizing is correct, not the crude fallback)
    assert sym in CONTRACT_SPECS
    # the ATR-stop ceiling is load-bearing now (ATR_STOP_ENABLED=True): a real
    # per-asset max-SL must exist, not the generic 1000 default.
    assert resolve_for_asset(cfg.BACKTEST_MAX_SL_DISTANCE, sym) != cfg.BACKTEST_MAX_SL_DISTANCE["default"]
    # guard + instrument config resolve to explicit per-asset values
    assert resolve_for_asset(cfg.BACKTEST_MIN_SL_DISTANCE, sym) is not None
    assert sym in cfg.BACKTEST_INSTRUMENT_CONFIG


def test_ustec_is_index_usoil_is_energy():
    assert ar.ASSET_PROFILES["USTEC"].category == "index"
    assert ar.ASSET_PROFILES["USTEC"].session == "index"
    assert ar.ASSET_PROFILES["USOIL"].category == "energy"
    assert ar.ASSET_PROFILES["USOIL"].session == "weekday_24_5"


def test_lot_sizing_new_assets_within_bounds():
    for sym, sl, price in [("USOIL", 0.5, 75.0), ("USTEC", 40, 20000.0), ("XAGUSD", 0.1, 30.0)]:
        lot = calc_lot_size_by_risk(
            balance=10_000, risk_pct=0.01, sl_points=sl, symbol=sym,
            specs=CONTRACT_SPECS, entry_price=price,
        )
        spec = CONTRACT_SPECS[sym]
        assert spec.min_lot <= lot <= spec.max_lot
