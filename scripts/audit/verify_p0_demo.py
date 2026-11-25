"""Read-only P0 broker inspection. Never sends orders, changes login or launches MT5.

Run against an already-running dedicated demo terminal:
  python scripts/audit/verify_p0_demo.py --terminal PATH --expected-login LOGIN --symbol BTCUSDm
For an existing demo test position, also supply --ticket, --entry, --sl, --quantity.
Without a running terminal this reports blocked; offline tests are not demo fills.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path


def inspect_demo(mt5, expected_login, symbol, *, ticket=None, entry=None, sl=None, quantity=None):
    """Inspect only the verified account; callable with a fake SDK in tests."""
    account = mt5.account_info()
    if account is None:
        raise RuntimeError("Account information unavailable")
    if account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
        raise RuntimeError("Account is not a demo; refusing inspection")
    if account.login != expected_login:
        raise RuntimeError("Account identity does not match the expected demo")
    positions = mt5.positions_get(ticket=ticket) if ticket else mt5.positions_get(symbol=symbol)
    if positions is None:
        raise RuntimeError("Position query failed; this is not an empty account")
    tick = mt5.symbol_info_tick(symbol)
    if tick is None or any(not math.isfinite(v) or v <= 0 for v in (tick.bid, tick.ask)):
        raise RuntimeError("Executable quote unavailable")
    if tick.ask < tick.bid:
        raise RuntimeError("Crossed bid/ask quote")
    now = datetime.now(timezone.utc)
    stamp = float(getattr(tick, "time_msc", 0) or 1000 * getattr(tick, "time", 0)) / 1000
    age = now.timestamp() - stamp
    if not math.isfinite(age) or not -2 <= age <= 30:
        raise RuntimeError("Quote is stale or its timestamp is invalid")
    result = {
        "status": "read_only_checks_passed", "as_of": now.isoformat(),
        "demo_verified": True, "expected_account_matched": True,
        "symbol": symbol, "bid": tick.bid, "ask": tick.ask, "quote_age_seconds": age,
        "positions": [{"ticket": p.ticket, "symbol": p.symbol, "entry": p.price_open,
                       "sl": p.sl, "tp": p.tp, "quantity": p.volume} for p in positions],
        "fill_validation": "not_exercised", "orders_sent": 0,
    }
    if ticket:
        if any(v is None or not math.isfinite(v) or v <= 0 for v in (entry, sl, quantity)):
            raise ValueError("Ticket inspection requires positive expected entry, SL and quantity")
        if len(positions) != 1 or positions[0].ticket != ticket or positions[0].symbol != symbol:
            raise RuntimeError("Expected demo position is absent or ambiguous")
        p = positions[0]
        for key, actual, expected in (("entry", p.price_open, entry), ("SL", p.sl, sl), ("quantity", p.volume, quantity)):
            if not math.isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-10):
                raise RuntimeError(f"Broker {key} differs from expected confirmed state")
        result["fill_validation"] = "existing_position_matches_expected_state"
    # A terminal-wide login switch during reads invalidates the whole snapshot.
    after = mt5.account_info()
    if after is None or after.login != account.login or after.server != account.server or after.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
        raise RuntimeError("Account changed during inspection")
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--terminal", help="Path of an already-running dedicated demo terminal64.exe")
    ap.add_argument("--expected-login", type=int)
    ap.add_argument("--symbol", default="BTCUSDm")
    ap.add_argument("--ticket", type=int)
    for name in ("entry", "sl", "quantity"):
        ap.add_argument(f"--{name}", type=float)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    result = {"status": "blocked", "orders_sent": 0}
    sdk = None
    attached = False
    try:
        import psutil
        terminals = set()
        for process in psutil.process_iter(["name", "exe"]):
            if (process.info["name"] or "").lower() in ("terminal64.exe", "terminal.exe") and process.info["exe"]:
                terminals.add(os.path.normcase(os.path.abspath(process.info["exe"])))
        if not terminals:
            raise RuntimeError("No running MT5 terminal; open a dedicated demo terminal first")
        requested = os.path.normcase(os.path.abspath(args.terminal)) if args.terminal else None
        if requested is None or requested not in terminals:
            raise RuntimeError("Specify --terminal for the already-running dedicated demo terminal")
        if not args.expected_login:
            raise RuntimeError("Specify --expected-login to verify the dedicated demo account")
        import MetaTrader5 as sdk
        # Explicit running path; no credential fallback or account switching.
        attached = sdk.initialize(requested, timeout=10000)
        if not attached:
            raise RuntimeError("Could not attach to the specified running terminal")
        result = inspect_demo(sdk, args.expected_login, args.symbol, ticket=args.ticket,
                              entry=args.entry, sl=args.sl, quantity=args.quantity)
    except Exception as exc:
        result["reason"] = str(exc)
    finally:
        if attached:
            sdk.shutdown()  # Detach this Python connection, not the terminal.
    serialized = json.dumps(result, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0 if result["status"] == "read_only_checks_passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
