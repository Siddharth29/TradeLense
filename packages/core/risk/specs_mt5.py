"""Contract specifications for common MT5 symbols.

Derived from the single source of truth in ``packages.core.asset_registry`` so
adding a new asset is a one-line registry edit (no change here).
"""

from __future__ import annotations

from ..asset_registry import ASSET_PROFILES
from .risk_mt5 import ContractSpecs

# Mapping of symbol identifiers to their contract specifications, built from the
# asset registry.  Only the fields ``ContractSpecs`` needs are projected.
CONTRACT_SPECS = {
    sym: ContractSpecs(
        symbol=sym,
        lot_step=p.lot_step,
        min_lot=p.min_lot,
        max_lot=p.max_lot,
        base_currency=p.base_currency,
        quote_currency=p.quote_currency,
        contract_size=p.contract_size,
    )
    for sym, p in ASSET_PROFILES.items()
}

__all__ = ["CONTRACT_SPECS"]
