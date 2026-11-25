"""Ghost-position guardian: force-close any broker position this app did not open.

Rationale
---------
Accounts get blown up by *manual* trades placed outside this system's risk rules.
Every order this app sends is stamped with :data:`MT5_MAGIC_NUMBER`
(packages/adapters/mt5_adapter.py), and MetaTrader stamps a position's ``magic``
atomically at fill. So a position whose magic differs from ours was opened by a
human (manual trade = magic ``0``) or another bot — a *ghost*. The guardian polls
open positions on a tight loop and force-closes every ghost at market.

This is the exact ownership signal the live TradeMonitor already trusts to decide
which positions it manages (packages/core/services/trade_monitor.py), so the
guardian can never disagree with the manager about what is "ours".

Safety posture
--------------
* Only acts on an ARMED LIVE account (``live_trading_allowed()``). When unarmed
  it is fully inert — there are no real ghosts to kill.
* Gated by ``MT5_GHOST_KILL_ENABLED``. ``MT5_GHOST_KILL_DRY_RUN`` detects + logs
  ghosts without closing them, so detection can be verified before arming.
* Fails toward SPARING for ownership: a position is killed only on an *explicit*
  foreign magic. A missing/unreadable magic is treated as ours and left alone —
  detection never errs toward closing our own (or an ambiguous) position.
* Never raises: a broker hiccup must not wedge the loop. Each ghost is closed
  independently — one failure never blocks the others.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Iterable, List, Mapping, Optional

from packages.core.config import (
    MT5_MAGIC_NUMBER,
    MT5_GHOST_KILL_ENABLED,
    MT5_GHOST_KILL_DRY_RUN,
    MT5_GHOST_KILL_INTERVAL_SECONDS,
    MT5_GHOST_KILL_STARTUP_GRACE_SECONDS,
    MT5_GHOST_KILL_SYMBOLS,
    MT5_GHOST_KILL_ALLOWED_MAGICS,
)
from packages.core.services.mt5_credentials import live_trading_allowed

logger = logging.getLogger(__name__)

# Hard floor on the poll interval. A literal 0 would busy-loop the worker thread
# and hammer the MT5 terminal with IPC, starving the real trading calls — so even
# "as fast as possible" is clamped here. 20ms ≈ 50 sweeps/sec: near-instant
# detection, trivial load.
_MIN_INTERVAL_SECONDS = 0.02


def allowed_magics() -> set[int]:
    """The set of magic numbers that count as 'ours' and are never killed."""
    extra = {int(m) for m in MT5_GHOST_KILL_ALLOWED_MAGICS}
    return {int(MT5_MAGIC_NUMBER), *extra}


def _scope_symbols() -> Optional[set[str]]:
    """Configured symbol scope, or None for 'all symbols'."""
    scope = {str(s).upper() for s in MT5_GHOST_KILL_SYMBOLS}
    return scope or None


def is_ghost(
    pos: Mapping[str, Any],
    allowed: Iterable[int],
    symbols: Optional[Iterable[str]] = None,
) -> bool:
    """Return True when *pos* was NOT opened by this app and is in scope.

    Errs toward sparing: a missing or unparseable ``magic`` is treated as ours,
    so an ambiguous record is never force-closed. Only an explicit foreign magic
    (manual = ``0``, or another bot) marks a ghost.
    """
    allowed_set = {int(a) for a in allowed}
    magic = pos.get("magic")
    if magic is None:
        return False  # ambiguous → never kill
    try:
        if int(magic) in allowed_set:
            return False  # ours → never kill
    except (TypeError, ValueError):
        return False  # unparseable → never kill

    if symbols is not None:
        scope = {str(s).upper() for s in symbols}
        if scope:
            sym = str(pos.get("symbol") or "").upper()
            if sym not in scope:
                return False  # out of scope → leave alone
    return True


def find_ghosts(
    positions: Iterable[Mapping[str, Any]],
    allowed: Optional[Iterable[int]] = None,
    symbols: Optional[Iterable[str]] = None,
) -> List[Mapping[str, Any]]:
    """Filter *positions* down to the ghosts (pure; no side effects)."""
    allow = list(allowed) if allowed is not None else list(allowed_magics())
    return [p for p in positions if is_ghost(p, allow, symbols)]


async def sweep_ghosts(adapter: Any, dry_run: Optional[bool] = None) -> List[dict]:
    """Run ONE detect+kill pass. Returns the list of ghost positions acted on.

    Never raises. In dry-run mode ghosts are detected and logged but not closed.
    Each ghost close is attempted independently so one failure never blocks the
    rest.
    """
    dry = MT5_GHOST_KILL_DRY_RUN if dry_run is None else dry_run
    acted: List[dict] = []
    try:
        positions = await asyncio.to_thread(adapter.positions_raw)
    except Exception as e:
        logger.debug(f"ghost_guard: positions fetch failed (skipping sweep): {e}")
        return acted

    ghosts = find_ghosts(positions, allowed_magics(), _scope_symbols())
    if not ghosts:
        return acted

    for g in ghosts:
        ticket = g.get("ticket")
        info = {
            "ticket": ticket,
            "symbol": g.get("symbol"),
            "magic": g.get("magic"),
            "volume": g.get("volume"),
            "dry_run": dry,
        }
        if dry:
            logger.warning(
                f"ghost_guard[DRY-RUN]: would KILL foreign position {info['symbol']} "
                f"ticket={ticket} magic={info['magic']} vol={info['volume']}",
                extra=info,
            )
            acted.append(info)
            continue

        logger.critical(
            f"ghost_guard: KILLING foreign position {info['symbol']} ticket={ticket} "
            f"magic={info['magic']} vol={info['volume']} (not opened by this app)",
            extra=info,
        )
        try:
            await asyncio.to_thread(adapter.close_position_by_ticket, int(ticket))
            logger.critical(
                f"ghost_guard: closed foreign position {info['symbol']} ticket={ticket}",
                extra=info,
            )
            acted.append(info)
        except Exception as e:
            logger.error(
                f"ghost_guard: FAILED to close foreign position {info['symbol']} "
                f"ticket={ticket}: {e}",
                extra=info,
            )
    return acted


async def ghost_guard_loop(adapter: Any, app: Any = None) -> None:
    """Background loop: kill ghosts on an armed live account every interval.

    Survives any per-iteration error; only cancellation stops it. Inert (keeps
    looping but does nothing) while the account is unarmed or the link is down,
    so arming/connecting later starts protection with no restart.
    """
    if not MT5_GHOST_KILL_ENABLED:
        logger.info("ghost_guard: disabled (MT5_GHOST_KILL_ENABLED=false) — not starting")
        return

    await asyncio.sleep(max(0.0, float(MT5_GHOST_KILL_STARTUP_GRACE_SECONDS)))
    interval = max(_MIN_INTERVAL_SECONDS, float(MT5_GHOST_KILL_INTERVAL_SECONDS))
    logger.info(
        "ghost_guard: started (interval=%ss, dry_run=%s, scope=%s)",
        interval, MT5_GHOST_KILL_DRY_RUN, _scope_symbols() or "ALL",
    )
    while True:
        try:
            await asyncio.sleep(interval)
            # Only act on a real, armed live link. Inert when unarmed.
            if not live_trading_allowed():
                continue
            try:
                connected = await asyncio.to_thread(adapter.is_connected)
            except Exception:
                connected = False
            if not connected:
                continue
            await sweep_ghosts(adapter)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # never let the guardian die
            logger.error(f"ghost_guard: sweep iteration failed: {e}")


__all__ = [
    "allowed_magics",
    "is_ghost",
    "find_ghosts",
    "sweep_ghosts",
    "ghost_guard_loop",
]
