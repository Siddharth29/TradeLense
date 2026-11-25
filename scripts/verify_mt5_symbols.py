"""Read-only MT5 symbol verification for the asset registry.

Run this on the machine where the **trial/demo** MT5 terminal is installed and
logged in (the real account won't connect for testing). It connects to the
already-running terminal via ``mt5.initialize()`` — no credentials needed in
this script — and dumps the broker's authoritative contract facts for each
roster symbol, then flags any mismatch against
``packages.core.asset_registry.ASSET_PROFILES``.

Nothing is traded; only ``symbol_info`` / ``symbol_info_session_*`` are read.

Usage:
    python scripts/verify_mt5_symbols.py
"""

from __future__ import annotations

import sys

try:
    import MetaTrader5 as mt5
except Exception as exc:  # pragma: no cover - environment dependent
    print(f"MetaTrader5 package not available: {exc}")
    sys.exit(1)

from packages.core.asset_registry import ASSET_PROFILES


def _detect_suffix() -> str:
    """Auto-detect a broker symbol suffix (some demo accounts append 'm')."""
    info = mt5.account_info()
    server = (getattr(info, "server", "") or "").lower()
    return "m" if ("trial" in server or "demo" in server) else ""


def main() -> int:
    if not mt5.initialize():
        print("mt5.initialize() failed — is the MT5 terminal installed?", mt5.last_error())
        return 1

    # Use locally populated placeholder credentials when present. If a terminal
    # is already logged in, this is a no-op.
    import json
    import os
    cfg_path = os.path.join(os.path.dirname(__file__), "..", "config", "mt5_config.json")
    try:
        creds = json.load(open(cfg_path))["mt5"]
        mt5.login(int(creds["login"]), password=creds["password"], server=creds["server"])
    except Exception as exc:
        print(f"login skipped/failed ({exc}); using the already-running terminal session")

    acct = mt5.account_info()
    suffix = _detect_suffix()
    print(f"Connected: login={getattr(acct,'login','?')} server={getattr(acct,'server','?')} "
          f"suffix={suffix!r}\n")

    header = f"{'symbol':10} {'broker_sym':12} {'contract':>10} {'vol_min':>8} {'vol_step':>8} {'digits':>6} {'stops_lvl':>9}"
    print(header)
    print("-" * len(header))

    for sym, prof in ASSET_PROFILES.items():
        broker_sym = f"{sym}{suffix}"
        if not mt5.symbol_select(broker_sym, True):
            print(f"{sym:10} {broker_sym:12}  <symbol_select failed — not offered on this account?>")
            continue
        info = mt5.symbol_info(broker_sym)
        if info is None:
            print(f"{sym:10} {broker_sym:12}  <symbol_info returned None>")
            continue
        print(f"{sym:10} {broker_sym:12} {info.trade_contract_size:>10g} "
              f"{info.volume_min:>8g} {info.volume_step:>8g} {info.digits:>6} {info.trade_stops_level:>9}")

        # Reconcile contract_size against the registry.
        if abs(info.trade_contract_size - prof.contract_size) > 1e-9:
            print(f"    ⚠ contract_size mismatch: registry={prof.contract_size} "
                  f"broker={info.trade_contract_size} → update ASSET_PROFILES[{sym!r}].contract_size")
        if abs(info.volume_step - prof.lot_step) > 1e-9:
            print(f"    ⚠ lot_step mismatch: registry={prof.lot_step} broker={info.volume_step}")

        # For the index, print the trading sessions so the daily break can be fixed.
        if prof.session == "index":
            print(f"    session windows for {broker_sym} (use to set USTEC daily_break, UTC):")
            for day in range(7):
                idx = 0
                while True:
                    quote = mt5.symbol_info_session_quote(broker_sym, day, idx)
                    if quote is None:
                        break
                    print(f"      day={day} session{idx}: {quote}")
                    idx += 1

    mt5.shutdown()
    print("\nDone. Reconcile any ⚠ lines into packages/core/asset_registry.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
