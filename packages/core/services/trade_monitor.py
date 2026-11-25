from __future__ import annotations

import asyncio
import functools
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from ..config import (
    SETTLEMENT_CANDLES,
    TRADE_MONITOR_EXPIRY_HOURS,
    TP2_PARTIAL_FRACTION,
    RUNNER_MAX_HOURS,
    MONITOR_RECOVERY_ENABLED,
    MONITOR_RECOVERY_MAX_ATTEMPTS,
    MONITOR_RECOVERY_RETRY_SECONDS,
    MT5_MAGIC_NUMBER,
)
from ..trade_monitor import (
    evaluate_trade,
    maybe_extend_expiry,
    MOVE_SL_TO_BE,
    TRAIL_SL,
    EXIT_TP,
    EXIT_SL,
    PARTIAL_TP2,
)
from ..asset_registry import profile_for
from ..scheduler import await_next_bar_close
from ..config import CSM_CANDLE_BUFFER_SEC as MONITOR_CANDLE_BUFFER_SEC
from packages.adapters.mt5_adapter import MT5Adapter, wait_for_bar
from packages.core.services.code_sniper_monitor import _is_market_closed, _is_market_closing_soon
from packages.db.repositories.monitor_state import (
    upsert_monitor_state,
    update_monitor_state,
    clear_monitor_state,
    get_open_monitor_states,
)


# Process-global guard: a given broker position is monitored by exactly ONE
# task. Recovery on startup, an accidental double-queue, or two per-asset
# monitors can never double-manage the same ticket — which would otherwise
# double-book partial closes. Keyed by position ticket.
_ACTIVE_TICKETS: set[str] = set()
# Same membership, keyed by asset — answers "is SOME live monitor task already
# managing this symbol's position?" without needing the ticket. The runner's
# LP-13 orphan check needs exactly that: `broker.positions()` returns
# `Position(symbol, quantity, average_price)` with no ticket, so an asset-level
# key is the only thing both sides can agree on. Safe because the system holds
# the single-position-per-symbol invariant (see `_recover_open_trades`).
_ACTIVE_ASSETS: Dict[str, str] = {}
# Recovery runs once per asset per process.
_RECOVERED_ASSETS: set[str] = set()


def _ticket_key(item: Dict[str, Any]) -> str:
    """Stable per-position identity used for de-dup and persistence."""
    pt = str(item.get("position_ticket") or "").strip()
    if pt:
        return pt
    ts = item.get("trade_state") or {}
    return f"{item.get('asset')}:{ts.get('entry')}"


def _asset_key(value: Any) -> str:
    """Normalise an asset/symbol to the key used by `_ACTIVE_ASSETS`.

    Broker symbols may carry a trial suffix ('XAUUSDm', 'USTECm') or a raw
    suffix ('XAUUSD.raw'); the agent side always uses the bare asset.
    Uppercasing and trimming those suffixes makes both sides land on the same
    key.

    The trailing-'M' rule is deliberately narrow — strip only when at least 5
    characters remain — because no asset in this system's roster (XAUUSD,
    XAGUSD, BTCUSD, EURUSD, GBPUSD, USTEC, USOIL) ends in M. A future base
    symbol that does would need an explicit exclusion here.
    """
    s = str(value or "").strip().upper()
    for suffix in (".RAW", ".PRO", ".ECN"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    if s.endswith("M") and len(s) >= 6:
        s = s[:-1]
    return s


def _item_asset_key(item: Dict[str, Any]) -> str:
    ts = item.get("trade_state") or {}
    return _asset_key(item.get("symbol") or ts.get("asset") or item.get("asset"))


def managed_ticket_for(asset: str) -> str | None:
    """Ticket of the live monitor task managing *asset*, or None.

    Public because the agent runner must not build a second TradeMonitor for a
    position that is already managed (LP-13 defect, 2026-07-27: seven parallel
    monitors in one day, one of which emitted a duplicate exit that corrupted a
    17-day-old trade record).
    """
    return _ACTIVE_ASSETS.get(_asset_key(asset))


def is_asset_managed(asset: str) -> bool:
    """True when a live monitor task already owns this asset's position."""
    return managed_ticket_for(asset) is not None


def _usd_pnl(asset: str, direction: str, entry: float, exit_price: float, qty: float) -> float:
    """USD P&L for a price move, including the instrument's contract size.

    The old ``(close − entry) × lots`` formula under-reported XAU by 100×,
    GBP by 100,000× and USOIL by 1,000× (docs/engineering/2026-06-11_performance-limiters.md §1.1).
    Unknown assets fall back to contract size 1.0 (BTC-style, legacy-exact).
    """
    profile = profile_for(asset)
    contract = float(getattr(profile, "contract_size", 1.0) or 1.0) if profile else 1.0
    sign = 1.0 if direction == "long" else -1.0
    return sign * (float(exit_price) - float(entry)) * float(qty) * contract


def _norm_side(direction: str) -> str:
    """Store sides in the parents' vocabulary (buy/sell, not long/short)."""
    return {"long": "buy", "short": "sell"}.get((direction or "").lower(), direction)


class TradeMonitor:
    """Background trade monitor evaluating rules on candle closes."""

    def __init__(self, candle_service: Any, broker: Any, journal: Any | None = None, on_trade_closed: Any = None, owner_asset: str | None = None) -> None:
        self.candle_service = candle_service
        self.broker = broker
        self.journal = journal
        self.on_trade_closed = on_trade_closed  # Callback function when trade is closed
        self._owner_asset = owner_asset  # symbol this monitor recovers on startup
        self._queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
        self._task = asyncio.create_task(self._run())
        self._monitor_tasks: set[asyncio.Task] = set()  # Track all monitor tasks
        self._shutdown_event = asyncio.Event()
        self._shutdown = False
        # Crash-recovery: re-adopt this asset's still-open broker positions into
        # the monitor on startup (positions survive a restart; in-memory
        # monitoring does not). Scoped to owner_asset so each per-asset monitor
        # only recovers its own trades, keeping recovery lifecycle-correct.
        self._recovery_task: asyncio.Task | None = None
        if MONITOR_RECOVERY_ENABLED and owner_asset:
            self._recovery_task = asyncio.create_task(self._recover_open_trades())

    # public ---------------------------------------------------------------
    def queue_trade(
        self,
        symbol: str,
        trade_state: Dict[str, Any],
        rules: Dict[str, Any],
        expiry: datetime,
        prompt_hash: str = "",
        gpt_mode: str = "",
        latency_ms: float = 0.0,
        trade_id: str = "",
        cycle_id: str = "",
        gpt_response_id: str = "",
        position_ticket: str = "",
        agent_id: str = "",
    ) -> None:
        """Enqueue a trade for monitoring until ``expiry``.

        ``cycle_id`` / ``gpt_response_id`` / ``position_ticket`` link every
        record this monitor writes (partials, exits) back to the parent
        decision and the exact broker position.
        """

        payload = {
            "asset": symbol,
            "trade_state": dict(trade_state),
            "rules": dict(rules),
            "expiry": expiry,
            "prompt_hash": prompt_hash,
            "gpt_mode": gpt_mode,
            "latency_ms": latency_ms,
            "trade_id": trade_id,
            "cycle_id": cycle_id,
            "gpt_response_id": gpt_response_id,
            "position_ticket": position_ticket,
            "agent_id": agent_id,
        }
        self._queue.put_nowait(payload)

    # internal -------------------------------------------------------------
    async def _run(self) -> None:
        """Main monitoring loop that processes queued trades."""
        try:
            while not self._shutdown:
                try:
                    # Wait for items with timeout to allow periodic shutdown checks
                    item = await asyncio.wait_for(self._queue.get(), timeout=1.0)

                    # De-dup: never monitor the same broker position twice
                    # (recovery + a live re-queue, or two per-asset monitors).
                    _key = _ticket_key(item)
                    if _key in _ACTIVE_TICKETS:
                        from packages.core.logger import get_logger
                        get_logger(__name__).info(
                            f"Duplicate monitor request for ticket={_key} — already managed, skipping"
                        )
                        continue
                    _ACTIVE_TICKETS.add(_key)
                    _akey = _item_asset_key(item)
                    if _akey:
                        _ACTIVE_ASSETS[_akey] = _key

                    # Create and track monitor task
                    monitor_task = asyncio.create_task(self._monitor(item))
                    self._monitor_tasks.add(monitor_task)

                    # Clean up completed tasks
                    monitor_task.add_done_callback(self._monitor_tasks.discard)
                    # Release the ticket when the task ends (even on cancellation),
                    # so a soft restart can re-adopt it cleanly.
                    monitor_task.add_done_callback(lambda t, k=_key: _ACTIVE_TICKETS.discard(k))
                    # Same for the asset key — but only if it still points at
                    # THIS ticket, so a fast re-adopt under a new ticket is not
                    # clobbered by the old task's teardown.
                    def _release_asset(_t, a=_akey, k=_key):
                        if a and _ACTIVE_ASSETS.get(a) == k:
                            _ACTIVE_ASSETS.pop(a, None)
                    monitor_task.add_done_callback(_release_asset)
                    
                except asyncio.TimeoutError:
                    # Timeout is expected - continue to check shutdown
                    continue
                except Exception as e:
                    from packages.core.logger import get_logger
                    logger = get_logger(__name__)
                    logger.error(f"Error in trade monitor main loop: {e}")
                    
        except asyncio.CancelledError:
            # Task was cancelled - this is expected during shutdown
            pass
        finally:
            # Cancel all pending monitor tasks
            for task in list(self._monitor_tasks):
                if not task.done():
                    task.cancel()
            
            # Wait for all monitor tasks to complete
            if self._monitor_tasks:
                await asyncio.gather(*self._monitor_tasks, return_exceptions=True)
            
            self._shutdown_event.set()

    async def _recover_open_trades(self) -> None:
        """Re-adopt this asset's still-open broker positions on startup.

        Broker positions survive a process crash/restart; the in-memory
        monitoring loop does not. Without re-adoption a recovered position keeps
        only its native SL/TP and silently loses TP1/TP2/BE/trail management.

        Recovery is the join of two sources:
          1. ``monitor_state`` rows (full, faithful trade_state + rules) — the
             primary path, restoring exactly where management left off.
          2. live broker positions with no row (persistence lost / pre-existing)
             — a degraded but safe path that re-attaches SL-exit + timeout +
             pre-close gap protection using only what the broker reports.

        Only positions stamped with this system's magic number are touched.
        """
        from packages.core.logger import get_logger
        logger = get_logger(__name__)

        asset = (self._owner_asset or "").strip()
        if not asset:
            return
        norm = asset.upper()
        if norm in _RECOVERED_ASSETS:
            return
        # Recovery applies only to the live MT5 broker.
        if not isinstance(self.broker, MT5Adapter):
            return
        _RECOVERED_ASSETS.add(norm)

        broker = self.broker

        # Wait for the broker terminal to warm up after a cold start.
        live_raw = None
        norm_sym = None
        for attempt in range(MONITOR_RECOVERY_MAX_ATTEMPTS):
            try:
                if broker._mt5 is not None:
                    norm_sym = broker._normalize_symbol(asset)
                    raw = await asyncio.to_thread(broker._mt5.positions_get, symbol=norm_sym)
                    if raw is None:
                        raise RuntimeError("Recovery position snapshot unavailable")
                    live_raw = list(raw)
                    break
            except Exception as e:
                logger.warning(
                    f"recovery: broker not ready for {asset} (attempt {attempt + 1}/"
                    f"{MONITOR_RECOVERY_MAX_ATTEMPTS}): {e}"
                )
            await asyncio.sleep(MONITOR_RECOVERY_RETRY_SECONDS)

        if live_raw is None:
            logger.error(f"recovery: broker never became ready for {asset} — skipping")
            # LP-13 fix (2026-07-27): un-mark the asset so a LATER monitor
            # construction (next cycle / next strategy) retries recovery instead
            # of silently never recovering for the life of the process.
            _RECOVERED_ASSETS.discard(norm)
            return

        # Map live positions by ticket, filtered to this system's magic.
        live_by_ticket: Dict[str, Dict[str, Any]] = {}
        for p in live_raw:
            d = p._asdict() if hasattr(p, "_asdict") else (dict(p) if isinstance(p, dict) else None)
            if not d:
                continue
            try:
                if int(d.get("magic", MT5_MAGIC_NUMBER)) != int(MT5_MAGIC_NUMBER):
                    continue
            except Exception:
                pass
            live_by_ticket[str(d.get("ticket"))] = d

        # Reticketing is valid only when the broker's stable identifier matches.
        # A sole same-symbol position can belong to a newer, unrelated trade.
        live_by_identifier = {str(p.get("identifier")): p for p in live_by_ticket.values()
                              if p.get("identifier")}

        adopted: set[str] = set()

        # 1) Re-adopt from persisted state.
        try:
            docs = await get_open_monitor_states()
        except Exception as e:
            logger.error(f"recovery: failed to load monitor_state for {asset}: {e}")
            _RECOVERED_ASSETS.discard(norm)
            docs = []

        for doc in docs:
            d_asset = str(doc.get("asset", "")).upper().strip()
            d_symbol = str(doc.get("symbol", "")).upper().strip()
            if d_asset != norm and d_symbol != norm:
                continue
            ticket = str(doc.get("position_ticket", ""))
            live = live_by_ticket.get(ticket)
            if live is None:
                live = live_by_identifier.get(ticket)
            if live is None:
                # Position closed while we were down. LP-13 fix (2026-07-27):
                # close the BOOKS too, not just the monitor row — previously the
                # row was cleared with no exit accounting, so the Mongo trade
                # stayed 'open' and RECENT_TRADES never learned the outcome
                # (07-14 USTEC −$6.82, 07-24 EURUSD −$6.42). Resolve the real
                # closing deal (96h lookback covers weekend orphans) and route
                # it through the NORMAL exit bookkeeping (Mongo close +
                # trade_exit event + RECENT_TRADES). Only a real broker deal is
                # recorded — no theoretical P&L is invented; without a deal the
                # row remains available for subsequent reconciliation.
                #
                # 2026-07-28: a legacy row can carry a composite key
                # ("XAUUSD:4517.5") instead of a broker ticket. That used to
                # raise inside int() and be swallowed by the except below,
                # reverting to the pre-fix bare clear. Such a ticket is now
                # treated as ABSENT and the deal is resolved by
                # symbol + magic + "not before this row was created" — still a
                # real broker deal, never an invented one.
                numeric_ticket = ticket if str(ticket).strip().isdigit() else None
                row_created = doc.get("created_at") or doc.get("updated_at")
                if isinstance(row_created, datetime) and row_created.tzinfo is None:
                    row_created = row_created.replace(tzinfo=timezone.utc)
                try:
                    deal = None
                    if isinstance(self.broker, MT5Adapter):
                        # Exact ticket ⇒ plain positional call (the narrowing
                        # kwargs are meaningless and would only widen the
                        # surface every caller/double has to implement).
                        if numeric_ticket:
                            call = functools.partial(
                                self.broker.get_last_exit_deal,
                                str(doc.get("symbol") or asset), numeric_ticket, 96.0,
                            )
                        else:
                            call = functools.partial(
                                self.broker.get_last_exit_deal,
                                str(doc.get("symbol") or asset), None, 96.0,
                                magic=int(MT5_MAGIC_NUMBER),
                                not_before=row_created,
                            )
                        deal = await asyncio.to_thread(call)
                    if deal and not numeric_ticket:
                        logger.warning(
                            f"recovery: {asset} row ticket={ticket!r} is not a broker "
                            f"ticket — matched closing deal position_id="
                            f"{deal.get('position_id')} by symbol+magic+time window"
                        )
                    if deal:
                        from packages.db.repositories.comprehensive_logging import (
                            update_comprehensive_trade_exit,
                        )
                        pnl = float(deal.get("profit") or 0.0)
                        exit_px = float(deal.get("price") or 0.0)
                        # Prefer the ticket the BROKER reports on the closing
                        # deal: when the row key was non-numeric this is the
                        # only exact linkage available, and it keeps the exit
                        # off the (now guarded) symbol fallback.
                        link_ticket = (
                            numeric_ticket
                            or (str(deal.get("position_id")) if deal.get("position_id") else None)
                        )
                        await self._record_trade_exit(
                            symbol=str(doc.get("symbol") or asset),
                            exit_price=exit_px,
                            exit_reason="RECOVERED_GONE",
                            pnl=pnl,
                            trade_closed_at=deal.get("time"),
                            trade_id=str(doc.get("trade_id") or "") or None,
                            position_ticket=link_ticket,
                        )
                        logger.info(
                            f"recovery: {asset} ticket={ticket} closed while unmanaged — "
                            f"synthetic trade_exit recorded (pnl={pnl:.2f} @ {exit_px}, "
                            f"reason={deal.get('reason')})"
                        )
                    else:
                        logger.warning(
                            f"recovery: {asset} ticket={ticket} gone but no closing deal "
                            f"found in 96h lookback — retaining unresolved state"
                        )
                        _RECOVERED_ASSETS.discard(norm)
                        continue
                except Exception as e:
                    logger.error(f"recovery: synthetic exit failed for {asset} ticket={ticket}: {e}")
                    _RECOVERED_ASSETS.discard(norm)
                    continue
                await clear_monitor_state(ticket, "RECOVERED_GONE")
                logger.info(f"recovery: {asset} ticket={ticket} no longer open — cleared")
                continue

            live_ticket = str(live.get("ticket") or ticket)
            if live_ticket in _ACTIVE_TICKETS or live_ticket in adopted:
                continue

            trade_state = dict(doc.get("trade_state") or {})
            # Broker state wins over a checkpoint saved before the last fill or
            # stop acknowledgement. Keep the original risk anchor for R metrics.
            trade_state.update(quantity=float(live["volume"]), sl=float(live.get("sl") or 0.) or None)
            rules = dict(doc.get("rules") or {})
            expiry = doc.get("expiry")
            if isinstance(expiry, datetime) and expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if not isinstance(expiry, datetime):
                expiry = datetime.now(timezone.utc) + timedelta(hours=TRADE_MONITOR_EXPIRY_HOURS)

            if live_ticket != ticket:
                # Re-key the persisted row to the real position ticket.
                await clear_monitor_state(ticket, "RECOVERED_RETICKET")

            self.queue_trade(
                symbol=str(doc.get("symbol") or asset),
                trade_state=trade_state,
                rules=rules,
                expiry=expiry,
                prompt_hash=str(doc.get("prompt_hash") or ""),
                gpt_mode=str(doc.get("gpt_mode") or ""),
                latency_ms=float(doc.get("latency_ms") or 0.0),
                trade_id=str(doc.get("trade_id") or ""),
                cycle_id=str(doc.get("cycle_id") or ""),
                gpt_response_id=str(doc.get("gpt_response_id") or ""),
                position_ticket=live_ticket,
                agent_id=str(doc.get("agent_id") or ""),
            )
            adopted.add(live_ticket)
            logger.info(
                f"recovery: re-adopted {asset} ticket={live_ticket} "
                f"entry={trade_state.get('entry')} sl={trade_state.get('sl')} "
                f"tp1_hit={trade_state.get('tp1_hit')} tp2_hit={trade_state.get('tp2_hit')}"
            )

        # 2) Degraded re-adoption: live positions with no persisted row.
        for ticket, d in live_by_ticket.items():
            if ticket in adopted or ticket in _ACTIVE_TICKETS:
                continue
            pos_type = int(d.get("type", 0))  # 0=BUY, 1=SELL
            direction = "long" if pos_type == 0 else "short"
            entry = float(d.get("price_open") or 0.0)
            sl = float(d.get("sl") or 0.0) or None
            vol = float(d.get("volume") or 0.0)
            trade_state = {
                "direction": direction,
                "entry": entry,
                "sl": sl,
                "initial_stop": sl,
                "sl_moved": False,
                "tp1_hit": False,
                "tp2_hit": False,
                "quantity": vol,
                "asset": asset,
            }
            # No TP data → evaluate_trade safely manages SL-exit + trailing only;
            # pre-close gap protection and the 4h timeout still apply.
            rules = {"tp1": None, "tp": None, "move_sl_to_be": False, "runner_enabled": False}
            expiry = datetime.now(timezone.utc) + timedelta(hours=TRADE_MONITOR_EXPIRY_HOURS)
            self.queue_trade(
                symbol=asset,
                trade_state=trade_state,
                rules=rules,
                expiry=expiry,
                position_ticket=ticket,
                agent_id="recovered",
            )
            logger.warning(
                f"recovery(degraded): adopted unpersisted {asset} ticket={ticket} "
                f"dir={direction} entry={entry} sl={sl} qty={vol} — "
                f"SL/trail/timeout guard only (no TP1/TP2 data available)"
            )

    async def _record_trade_exit(self, **fields):
        from packages.db.repositories.comprehensive_logging import update_comprehensive_trade_exit
        from ..operational_ledger import broker_position_cost
        from ..logger import get_logger
        if isinstance(self.broker, MT5Adapter) and (getattr(self.broker, "_credentials", {}) or {}).get("login"):
            try:
                cost = await asyncio.to_thread(broker_position_cost, self.broker, fields.get("position_ticket"))
                if cost["status"] != "final":
                    raise ValueError("Closing broker charges have not reconciled")
                fields["broker_cost"] = cost
            except Exception as exc:
                get_logger(__name__).error(f"Broker charges pending for {fields.get('position_ticket')}: {exc}")
                fields["broker_cost"] = {"total": None, "status": "pending", "error": str(exc)}
        return await update_comprehensive_trade_exit(**fields)

    async def _resolve_real_exit(
        self,
        symbol: str,
        position_ticket: str | int | None,
        *,
        asset: str,
        direction: str,
        entry: float,
        planned_exit: float,
        quantity: float,
    ) -> tuple[float, float, datetime | None, bool]:
        """Resolve the *actual* exit fill after an app-initiated close.

        The app's TP2 / SL / timeout / pre-close logic closes the position at
        market — the planned SL/TP level is only the *intent*, not the fill.
        Recording that planned level together with a price-derived theoretical
        P&L (``_usd_pnl``) overstated realised P&L whenever the market gapped
        past the level or the close slipped, so the recorded exit reason was
        right but the money was wrong and the dashboard total drifted away from
        the real account equity. This reads the real MT5 closing deal
        (ticket-scoped) and returns its fill price + realised money, exactly
        like the external-close path already does.

        Must be called *after* the broker close. Falls back to the planned
        level and the contract-size-correct theoretical P&L for non-MT5 brokers
        (tests) or when the deal can't be read back yet.

        Returns ``(exit_price, pnl, closed_at, used_real_deal)``.
        """
        from packages.core.logger import get_logger
        logger = get_logger(__name__)

        theoretical = _usd_pnl(asset, direction, entry, planned_exit, quantity)
        if not isinstance(self.broker, MT5Adapter):
            return planned_exit, theoretical, None, False

        deal = None
        for _attempt in range(3):
            try:
                deal = await asyncio.to_thread(
                    self.broker.get_last_exit_deal, symbol, position_ticket or None
                )
            except Exception as e:
                logger.warning(f"get_last_exit_deal failed for {symbol}: {e}")
                deal = None
            if deal:
                break
            await asyncio.sleep(0.3)

        if not deal:
            if (getattr(self.broker, "_credentials", {}) or {}).get("login"):
                from ..operational_execution import ExitNotConfirmed
                raise ExitNotConfirmed("Broker has not confirmed the whole position exit")
            logger.warning(
                f"No MT5 closing deal found for {symbol} (ticket={position_ticket}) "
                f"after close — recording planned-level P&L={theoretical:.2f}, "
                f"which may differ from the broker"
            )
            return planned_exit, theoretical, None, False

        exit_px = float(deal.get("price") or 0.0) or planned_exit
        profit = deal.get("profit")
        pnl = float(profit) if profit is not None else theoretical
        return exit_px, pnl, deal.get("time"), True

    async def _monitor(self, item: Dict[str, Any]) -> None:
        import os
        from ..management_feed import ManagementEventFeed, position_recorder
        feed = None
        mode = os.environ.get("FAST_POSITION_MODE", "shadow")
        if mode not in ("off", "shadow", "live"):
            import logging
            logging.getLogger(__name__).error("Invalid FAST_POSITION_MODE; retaining bar management")
            mode = "off"
        connected = isinstance(self.broker, MT5Adapter) and (getattr(self.broker, "_credentials", {}) or {}).get("login")
        if connected and mode != "off":
            async def bar_source():
                service, symbol = self.candle_service, item["asset"]
                if isinstance(service, MT5Adapter):
                    return await wait_for_bar(service, symbol, "M5")
                await await_next_bar_close("5m", "UTC")
                await asyncio.sleep(MONITOR_CANDLE_BUFFER_SEC)
                if hasattr(service, "get_latest_closed_candle"):
                    last = await service.get_latest_closed_candle(symbol, "5m")
                else:
                    c5, _ = await service.fetch(symbol, n5=1, n15=1)
                    last = c5[-1] if c5 else None
                if last is None:
                    raise RuntimeError("No closed management bar")
                return last if isinstance(last, dict) else {k: getattr(last, k) for k in ("time", "close", "high", "low")}
            try:
                record = await asyncio.to_thread(position_recorder, self.broker, item.get("position_ticket"))
                feed = ManagementEventFeed(self.broker, item["asset"], bar_source, mode=mode, record=record)
            except Exception:
                # Existing native protection/bar owner must remain active even
                # when the shadow journal cannot initialize.
                import logging
                logging.getLogger(__name__).exception("Fast feed unavailable; retaining existing bar management")
        item["_event_feed"] = feed
        item["_operational"] = bool(connected)
        item["_needs_reconciliation"] = bool(connected)
        try:
            from ..operational_execution import ExitNotConfirmed
            while not self._shutdown:
                try:
                    await self._monitor_impl(item)
                    break
                except ExitNotConfirmed:
                    await asyncio.sleep(2)
        finally:
            if feed:
                await feed.close()
            item.pop("_event_feed", None)

    async def _partial_exit(self, item, fraction, kind):
        if not item.get("_operational"):
            return await asyncio.to_thread(self.broker.partial_close, item["asset"], fraction)
        from ..operational_execution import managed_partial
        try:
            return await asyncio.to_thread(managed_partial, self.broker, item.get("position_ticket"), item["asset"], fraction, kind,
                                           item["trade_state"].get("quantity"))
        except Exception:
            item["_needs_reconciliation"] = True
            raise

    async def _close_position(self, item):
        if item.get("_operational"):
            return await asyncio.to_thread(self.broker.close_position_by_ticket, int(item["position_ticket"]))
        return await asyncio.to_thread(self.broker.close, item["asset"])

    async def _modify_stop(self, item, price):
        import math
        started = datetime.now(timezone.utc)
        payload = {"requested_sl": price, "requested_at": started.isoformat()}
        try:
            if item.get("_operational"):
                applied = await asyncio.to_thread(self.broker.modify_sl, item["asset"], price, position_ticket=item["position_ticket"])
                if isinstance(applied, bool) or not isinstance(applied, (int, float)) or not math.isfinite(applied) or applied <= 0:
                    raise RuntimeError("Stop update has no confirmed broker price")
            else:
                applied = await asyncio.to_thread(self.broker.modify_sl, item["asset"], price)
            payload["applied_sl"] = applied
            return applied
        except Exception as exc:
            payload["error"] = str(exc)
            raise
        finally:
            payload["received_at"] = datetime.now(timezone.utc).isoformat()
            feed = item.get("_event_feed")
            if feed and feed.record:
                try:
                    await asyncio.to_thread(feed.record, "stop_receipt", payload)
                except Exception:
                    pass  # A trace write must not roll back an acknowledged SL.

    async def _monitor_impl(self, item: Dict[str, Any]) -> None:
        symbol = item["asset"]
        trade_state = item["trade_state"]
        trade_state.setdefault("original_quantity", trade_state.get("quantity"))
        rules = item["rules"]
        expiry: datetime = item["expiry"]
        hard_expiry = expiry if rules.get("hard_max_age_minutes") else None
        service = self.candle_service
        
        # Settlement: skip SL evaluation on first N candles after entry
        # (matches backtest behaviour — prevents same-bar knockout)
        candle_count = 0
        
        # Enhanced logging for trade monitoring
        from packages.core.logger import get_logger
        logger = get_logger(__name__)
        
        # Validate trade state - CRITICAL for proper monitoring
        required_fields = ["direction", "entry"]
        missing_fields = [field for field in required_fields if not trade_state.get(field)]
        
        if missing_fields:
            logger.error(
                f"CRITICAL: Invalid trade state for {symbol} - missing required fields: {missing_fields}",
                extra={
                    "symbol": symbol,
                    "trade_state": trade_state,
                    "missing_fields": missing_fields,
                    "error": "Cannot monitor trade without complete trade state"
                }
            )
            return

        if not trade_state.get("sl"):
            logger.error(
                f"CRITICAL: {symbol} has no confirmed stop; retaining exit/timeout monitoring"
            )
        
        # Enhanced trade monitoring with reliability checks
        start_time = datetime.now(timezone.utc)
        missed_candles = 0
        max_missed_candles = 3  # Allow up to 3 missed candles before alerting
        last_candle_time = None

        # Latest-candle OHLC sentinels. The pre-close and expiry branches below
        # reference ``close`` to compute an exit/PnL fallback; on the very first
        # iteration (e.g. a position queued just before a session close) that
        # happens before any candle is fetched. Initialising them keeps those
        # paths on their intended entry-price fallback instead of raising
        # UnboundLocalError, which would silently kill the monitor task.
        close = high = low = None

        logger.info(
            f"ENHANCED: Starting reliable trade monitoring for {symbol}",
            extra={
                "symbol": symbol,
                "direction": trade_state.get("direction"),
                "entry": trade_state.get("entry"),
                "sl": trade_state.get("sl"),
                "tp1": rules.get("tp1"),
                "tp": rules.get("tp"),
                "expiry": expiry.isoformat(),
                "monitoring_interval": "5min",
                "start_time": start_time.isoformat(),
                "max_missed_candles": max_missed_candles
            }
        )

        # Persist this position's full management state so it can be re-adopted
        # after a crash/restart (the broker keeps the position; memory does not).
        pos_ticket = _ticket_key(item)
        try:
            await upsert_monitor_state(
                position_ticket=pos_ticket,
                symbol=symbol,
                asset=trade_state.get("asset", symbol),
                trade_state=trade_state,
                rules=rules,
                expiry=expiry,
                prompt_hash=item.get("prompt_hash", ""),
                gpt_mode=item.get("gpt_mode", ""),
                latency_ms=item.get("latency_ms", 0.0),
                trade_id=item.get("trade_id", ""),
                cycle_id=item.get("cycle_id", ""),
                gpt_response_id=item.get("gpt_response_id", ""),
                agent_id=item.get("agent_id", ""),
            )
        except Exception as e:
            logger.error(f"Failed to persist monitor_state for {symbol}: {e}")

        while datetime.now(timezone.utc) < expiry and not self._shutdown:
            current_time = datetime.now(timezone.utc)
            candle_received = False
            is_quote = False

            # PRE-CLOSE CHECK: force-close position before market gap
            closing_soon, close_reason = _is_market_closing_soon(symbol, now=current_time)
            if closing_soon:
                last_price = float(close) if close is not None else trade_state.get("entry", 0)
                entry_price = trade_state.get("entry", 0)
                direction = trade_state.get("direction", "long")
                quantity = trade_state.get("quantity", 0)
                exit_reason = f"PRE_CLOSE_{close_reason.upper()}"

                logger.info(
                    f"RISK: Market closing soon for {symbol} ({close_reason}) — "
                    f"force-closing position to avoid gap risk",
                    extra={
                        "symbol": symbol,
                        "close_reason": close_reason,
                        "direction": direction,
                        "entry": entry_price,
                        "last_price": last_price,
                    },
                )

                # Close at market FIRST, then record the real fill (price +
                # realised money) from the MT5 deal so the books match the account.
                if hasattr(self.broker, "close"):
                    try:
                        await self._close_position(item)
                        logger.info(f"Pre-close position closed successfully for {symbol}")
                    except Exception as e:
                        logger.error(f"Failed to pre-close position for {symbol}: {e}")

                exit_px, pnl, deal_time, _real = await self._resolve_real_exit(
                    symbol,
                    item.get("position_ticket") or None,
                    asset=trade_state.get("asset", symbol),
                    direction=direction,
                    entry=entry_price,
                    planned_exit=last_price,
                    quantity=quantity,
                )

                try:
                    from packages.db.repositories.comprehensive_logging import update_comprehensive_trade_exit
                    await self._record_trade_exit(
                        symbol=symbol,
                        exit_price=exit_px,
                        exit_reason=exit_reason,
                        pnl=pnl,
                        trade_closed_at=deal_time,
                        trade_id=item.get("trade_id") or None,
                        position_ticket=item.get("position_ticket") or None,
                    )
                except Exception as e:
                    logger.error(f"Failed to update pre-close exit in MongoDB for {symbol}: {e}")

                if self.on_trade_closed:
                    try:
                        self.on_trade_closed(symbol, exit_reason, exit_px)
                    except Exception as e:
                        logger.error(f"Error in pre-close callback for {symbol}: {e}")
                await clear_monitor_state(pos_ticket, exit_reason)
                break

            # Pre-check market close BEFORE attempting any fetch
            closed, wait_sec = _is_market_closed(symbol, now=current_time)
            if closed:
                sleep_sec = max(wait_sec + 30, 60)
                logger.info(
                    f"Market closed for {symbol} — sleeping {sleep_sec}s until reopen (pre-check)",
                    extra={"symbol": symbol, "sleep_sec": sleep_sec},
                )
                await asyncio.sleep(sleep_sec)
                continue

            try:
                if item.get("_needs_reconciliation"):
                    from ..operational_execution import reconcile_management
                    patch = await asyncio.to_thread(reconcile_management, self.broker, item.get("position_ticket"), trade_state)
                    trade_state.update(patch)
                    item["_needs_reconciliation"] = False
                if isinstance(service, MT5Adapter) or item.get("_event_feed"):
                    # Enhanced MT5 adapter monitoring with reliability checks
                    try:
                        feed = item.get("_event_feed")
                        bar = await asyncio.wait_for(feed.next(trade_state, rules) if feed else wait_for_bar(service, symbol, "M5"), timeout=330.0)
                        if bar.get("source") == "heartbeat":
                            continue
                        is_quote = bar.get("source") == "quote"
                        close = bar.get("close")
                        high = bar.get("high") 
                        low = bar.get("low")
                        bar_time = bar.get("time")
                        
                        if None in {close, high, low}:
                            logger.error(f"CRITICAL: MT5 bar has null values for {symbol}: close={close}, high={high}, low={low}")
                            missed_candles += 1
                            continue
                        
                        # DUPLICATE GUARD (2026-08-27, audit H2). `evaluate_trade`
                        # is stateful: `trail_consec` counts CONSECUTIVE confirming
                        # closes and the first ladder step needs three, so
                        # re-feeding one bar lets a step arm on two real bars and
                        # tightens the stop earlier than the rule allows.
                        # `candle_count` also drives the SETTLEMENT_CANDLES window,
                        # so a double count brings the stop live a bar early — the
                        # same-bar knockout that window exists to prevent.
                        if not is_quote and last_candle_time and bar_time and bar_time <= last_candle_time:
                            logger.debug(
                                f"Skipping already-evaluated bar for {symbol}",
                                extra={
                                    "symbol": symbol,
                                    "bar_time": str(bar_time),
                                    "last_evaluated": str(last_candle_time),
                                },
                            )
                            continue

                        # Validate bar time progression
                        if not is_quote and last_candle_time and bar_time:
                            time_gap = (bar_time - last_candle_time).total_seconds() / 60
                            if time_gap > 10:  # More than 10 minutes gap
                                logger.warning(f"Large time gap in candles for {symbol}: {time_gap:.1f} minutes")
                        
                        if not is_quote:
                            last_candle_time = bar_time
                        candle_received = True
                        missed_candles = 0  # Reset counter on successful receive
                        
                        logger.debug(
                            f"RELIABLE: MT5 bar received for {symbol}",
                            extra={
                                "symbol": symbol,
                                "close": close,
                                "high": high,
                                "low": low,
                                "bar_time": bar_time.isoformat() if bar_time else None,
                                "missed_candles": missed_candles,
                                "monitoring_health": "good"
                            }
                        )
                    except asyncio.TimeoutError:
                        missed_candles += 1
                        closed, wait_sec = _is_market_closed(symbol)
                        if closed:
                            logger.info(
                                f"Market closed for {symbol} — sleeping {wait_sec + 30}s until reopen (timeout during close)",
                                extra={"symbol": symbol, "sleep_sec": wait_sec + 30},
                            )
                            await asyncio.sleep(max(wait_sec + 30, 60))
                            continue
                        logger.error(f"CRITICAL: MT5 bar timeout for {symbol} - missed candle #{missed_candles}")
                        if missed_candles >= max_missed_candles:
                            logger.error(f"CRITICAL: Too many missed candles for {symbol} ({missed_candles}) - monitoring reliability compromised")
                        backoff = min(30, 2 ** min(missed_candles, 5))
                        await asyncio.sleep(backoff)
                        continue
                    except RuntimeError as e:
                        if "market closed" in str(e).lower():
                            _, wait_sec = _is_market_closed(symbol)
                            sleep_sec = max(wait_sec + 30, 60)
                            logger.info(
                                f"Market closed for {symbol} — sleeping {sleep_sec}s until reopen",
                                extra={"symbol": symbol, "sleep_sec": sleep_sec},
                            )
                            await asyncio.sleep(sleep_sec)
                            continue
                        # Not a market-close error — treat as normal error
                        missed_candles += 1
                        logger.error(f"CRITICAL: MT5 bar error for {symbol}: {e} - missed candle #{missed_candles}")
                        backoff = min(30, 2 ** min(missed_candles, 5))
                        await asyncio.sleep(backoff)
                        continue
                    except Exception as e:
                        missed_candles += 1
                        # Check market-close window before spamming retries
                        closed, wait_sec = _is_market_closed(symbol)
                        if closed:
                            logger.info(
                                f"Market closed for {symbol} — sleeping {wait_sec + 30}s until reopen (error during close: {e})",
                                extra={"symbol": symbol, "sleep_sec": wait_sec + 30},
                            )
                            await asyncio.sleep(max(wait_sec + 30, 60))
                            continue
                        logger.error(f"CRITICAL: MT5 bar error for {symbol}: {e} - missed candle #{missed_candles}")
                        backoff = min(30, 2 ** min(missed_candles, 5))
                        await asyncio.sleep(backoff)
                        continue
                else:
                    # Enhanced service monitoring with reliability checks.
                    # LIVE-TRADING OPTIMISATION: use the cache-aware
                    # ``get_latest_closed_candle`` so we only fetch the 5m
                    # candle the monitor actually needs (skips the redundant
                    # 15m round-trip that ``fetch(n5=1, n15=1)`` triggered).
                    try:
                        await asyncio.wait_for(await_next_bar_close("5m", "UTC"), timeout=330.0)  # 5.5 minutes
                        # Let the broker finalise the bar before asking for it.
                        # CodeSniperMonitor has always done this
                        # (CSM_CANDLE_BUFFER_SEC); this loop fetched on the
                        # boundary, which is exactly when a feed is most likely to
                        # hand back the PREVIOUS bar.
                        await asyncio.sleep(MONITOR_CANDLE_BUFFER_SEC)
                        last = None
                        if hasattr(service, "get_latest_closed_candle"):
                            last = await asyncio.wait_for(
                                service.get_latest_closed_candle(symbol, "5m"),
                                timeout=30.0,
                            )
                        else:
                            # Fallback for services that don't implement the
                            # cache-aware helper (preserves legacy behaviour).
                            c5, _ = await asyncio.wait_for(
                                service.fetch(symbol, n5=1, n15=1),
                                timeout=30.0,
                            )
                            last = c5[-1] if c5 else None

                        if last is None:
                            missed_candles += 1
                            logger.error(f"CRITICAL: No candles received for {symbol} - missed candle #{missed_candles}")
                            continue

                        if isinstance(last, dict):
                            close = last.get("close")
                            high = last.get("high")
                            low = last.get("low")
                            bar_time = last.get("time")
                        else:
                            close = getattr(last, "close", None)
                            high = getattr(last, "high", None)
                            low = getattr(last, "low", None)
                            bar_time = getattr(last, "time", None)
                        
                        if None in {close, high, low}:
                            missed_candles += 1
                            logger.error(f"CRITICAL: Service bar has null values for {symbol}: close={close}, high={high}, low={low}")
                            continue
                        
                        # DUPLICATE GUARD (2026-08-27, audit H2). `evaluate_trade`
                        # is stateful: `trail_consec` counts CONSECUTIVE confirming
                        # closes and the first ladder step needs three, so
                        # re-feeding one bar lets a step arm on two real bars and
                        # tightens the stop earlier than the rule allows.
                        # `candle_count` also drives the SETTLEMENT_CANDLES window,
                        # so a double count brings the stop live a bar early — the
                        # same-bar knockout that window exists to prevent.
                        if last_candle_time and bar_time and bar_time <= last_candle_time:
                            logger.debug(
                                f"Skipping already-evaluated bar for {symbol}",
                                extra={
                                    "symbol": symbol,
                                    "bar_time": str(bar_time),
                                    "last_evaluated": str(last_candle_time),
                                },
                            )
                            continue

                        # Validate bar time progression  
                        if last_candle_time and bar_time:
                            time_gap = (bar_time - last_candle_time).total_seconds() / 60
                            if time_gap > 10:  # More than 10 minutes gap
                                logger.warning(f"Large time gap in service candles for {symbol}: {time_gap:.1f} minutes")
                        
                        last_candle_time = bar_time
                        candle_received = True
                        missed_candles = 0  # Reset counter on successful receive
                        
                        logger.debug(
                            f"RELIABLE: Service bar received for {symbol}",
                            extra={
                                "symbol": symbol,
                                "close": close,
                                "high": high,
                                "low": low,
                                "bar_time": bar_time.isoformat() if bar_time else None,
                                "missed_candles": missed_candles,
                                "monitoring_health": "good"
                            }
                        )
                    except asyncio.TimeoutError:
                        missed_candles += 1
                        closed, wait_sec = _is_market_closed(symbol)
                        if closed:
                            logger.info(
                                f"Market closed for {symbol} — sleeping {wait_sec + 30}s until reopen (timeout during close)",
                                extra={"symbol": symbol, "sleep_sec": wait_sec + 30},
                            )
                            await asyncio.sleep(max(wait_sec + 30, 60))
                            continue
                        logger.error(f"CRITICAL: Service bar timeout for {symbol} - missed candle #{missed_candles}")
                        if missed_candles >= max_missed_candles:
                            logger.error(f"CRITICAL: Too many missed candles for {symbol} ({missed_candles}) - monitoring reliability compromised")
                        backoff = min(30, 2 ** min(missed_candles, 5))
                        await asyncio.sleep(backoff)
                        continue
                    except Exception as e:
                        missed_candles += 1
                        closed, wait_sec = _is_market_closed(symbol)
                        if closed:
                            logger.info(
                                f"Market closed for {symbol} — sleeping {wait_sec + 30}s until reopen (error during close: {e})",
                                extra={"symbol": symbol, "sleep_sec": wait_sec + 30},
                            )
                            await asyncio.sleep(max(wait_sec + 30, 60))
                            continue
                        logger.error(f"CRITICAL: Service bar error for {symbol}: {e} - missed candle #{missed_candles}")
                        backoff = min(30, 2 ** min(missed_candles, 5))
                        await asyncio.sleep(backoff)
                        continue
                
                # Enhanced trade evaluation with reliability checks
                if candle_received:
                    candle_count += int(not is_quote)
                    is_entry = candle_count <= SETTLEMENT_CANDLES

                    # Detect positions closed externally by MT5 native SL/TP
                    if isinstance(self.broker, MT5Adapter) and self.broker._mt5 is not None:
                        try:
                            norm_sym = self.broker._normalize_symbol(symbol)
                            # An unrelated/manual position on the same symbol
                            # does not mean THIS monitored ticket is still open.
                            ticket_value = str(item.get("position_ticket") or "").strip()
                            position_filter = (
                                {"ticket": int(ticket_value)}
                                if ticket_value.isdigit() and int(ticket_value) > 0
                                else {"symbol": norm_sym}
                            )
                            pos_list = await asyncio.to_thread(
                                self.broker._mt5.positions_get, **position_filter
                            )
                            if pos_list is None:
                                # MT5 returns None for a failed query, whereas
                                # an empty tuple means no positions. A failed
                                # read must not fabricate a close, erase durable
                                # state and abandon an open, TP-less runner.
                                logger.error(
                                    f"MT5 position query failed for {symbol} — "
                                    "retaining monitoring state; retrying next bar"
                                )
                                continue
                            pos_gone = hasattr(pos_list, '__len__') and len(pos_list) == 0
                            if pos_gone:
                                # Get actual exit data from MT5 deal history —
                                # ticket-scoped so we read THIS position's deal.
                                deal = await asyncio.to_thread(
                                    self.broker.get_last_exit_deal,
                                    symbol,
                                    item.get("position_ticket") or None,
                                )

                                if deal:
                                    exit_px = deal["price"]
                                    exit_reason = f"MT5_{deal['reason']}"
                                    mt5_pnl = deal["profit"]
                                    deal_time = deal.get("time")
                                else:
                                    if item.get("_operational"):
                                        from ..operational_execution import ExitNotConfirmed
                                        raise ExitNotConfirmed("Native exit history has not reconciled")
                                    exit_px = float(close)
                                    exit_reason = "MT5_NATIVE_CLOSE"
                                    mt5_pnl = None
                                    deal_time = None

                                # Compute PnL from deal if available, else from prices
                                entry_price = trade_state.get("entry", 0)
                                direction = trade_state.get("direction", "long")
                                quantity = trade_state.get("quantity", 0)
                                if mt5_pnl is not None:
                                    pnl = mt5_pnl
                                else:
                                    pnl = _usd_pnl(trade_state.get("asset", symbol),
                                                   direction, entry_price, exit_px, quantity)
                                # A native close after the TP2 partial is the
                                # runner ending on its ratcheted stop — label it
                                # so analytics can separate it from a real SL.
                                if trade_state.get("tp2_hit") and exit_reason == "MT5_SL":
                                    exit_reason = "MT5_RUNNER_TRAIL"

                                logger.info(
                                    f"Position for {symbol} no longer exists in MT5 "
                                    f"— closed externally ({exit_reason})",
                                    extra={
                                        "symbol": symbol,
                                        "exit_price": exit_px,
                                        "exit_reason": exit_reason,
                                        "pnl": pnl,
                                        "deal_time": str(deal_time) if deal_time else None,
                                        "candle_count": candle_count,
                                    },
                                )

                                try:
                                    from packages.db.repositories.comprehensive_logging import update_comprehensive_trade_exit
                                    await self._record_trade_exit(
                                        symbol=symbol,
                                        exit_price=exit_px,
                                        exit_reason=exit_reason,
                                        pnl=pnl,
                                        trade_closed_at=deal_time,
                                        trade_id=item.get("trade_id") or None,
                                        position_ticket=item.get("position_ticket") or None,
                                    )
                                except Exception as e:
                                    logger.error(f"Failed to update MongoDB for externally closed trade {symbol}: {e}")

                                if self.on_trade_closed:
                                    try:
                                        self.on_trade_closed(symbol, exit_reason, exit_px)
                                    except Exception as e:
                                        logger.error(f"Error in trade closed callback for {symbol}: {e}")
                                await clear_monitor_state(pos_ticket, exit_reason)
                                break
                        except Exception as e:
                            logger.warning(f"Failed to verify MT5 position for {symbol}: {e}")
                            continue  # Preserve state; never infer an exit from a failed read.

                    # Green-TTL extension (2026-08-06). Checked on each fresh
                    # bar, before the exit evaluation, so a position that is
                    # winning and already risk-free is never closed purely
                    # because a 4h clock ran out. See config.TTL_EXTEND_WHEN_GREEN
                    # (exhibit: 07-17 XAU timed out at −0.17R, then ran +3.04R).
                    # Quote sampling must not change the bar-confirmed TTL
                    # policy, especially when fast management is only shadowed.
                    new_expiry = expiry if is_quote else maybe_extend_expiry(
                        trade_state, float(close), expiry,
                        start_time=start_time, now=current_time,
                    )
                    if hard_expiry:
                        new_expiry = min(new_expiry, hard_expiry)
                    if new_expiry != expiry:
                        logger.info(
                            f"TTL extended for {symbol} — position green and risk-free: "
                            f"{expiry.isoformat()} -> {new_expiry.isoformat()}",
                            extra={
                                "symbol": symbol,
                                "old_expiry": expiry.isoformat(),
                                "new_expiry": new_expiry.isoformat(),
                                "close": float(close),
                                "sl": trade_state.get("sl"),
                                "entry": trade_state.get("entry"),
                            },
                        )
                        expiry = new_expiry
                        await update_monitor_state(
                            pos_ticket, trade_state=trade_state, expiry=expiry
                        )

                    feed = item.get("_event_feed")
                    if is_quote:
                        action = bar.get("quote_action")
                    else:
                        eval_rules = {**rules, "tp1": None, "tp": None} if feed and feed.mode == "live" else rules
                        action = evaluate_trade(trade_state, float(close), float(high), float(low), eval_rules,
                                                is_entry_candle=is_entry or bool(feed and feed.mode == "live"))
                    
                    logger.debug(
                        f"RELIABLE: Trade evaluation for {symbol}",
                        extra={
                            "symbol": symbol,
                            "close": close,
                            "high": high,
                            "low": low,
                            "action": action,
                            "direction": trade_state.get("direction"),
                            "entry": trade_state.get("entry"),
                            "sl": trade_state.get("sl"),
                            "tp1": rules.get("tp1"),
                            "tp": rules.get("tp"),
                            "candle_time": last_candle_time.isoformat() if last_candle_time else None,
                            "monitoring_duration": (current_time - start_time).total_seconds() / 60,
                            "missed_candles_total": missed_candles
                        }
                    )
                else:
                    # Skip evaluation if no valid candle received
                    logger.warning(f"Skipping trade evaluation for {symbol} - no valid candle received in this cycle")
                    continue
                
                if action == TRAIL_SL:
                    new_sl = trade_state.pop("_pending_trail_sl", trade_state.get("entry"))
                    trail_label = trade_state.pop("_trail_label", "trail")
                    # Step-state snapshot from the evaluator (fixed/continuous
                    # steps only; the chandelier self-heals and sets none).
                    trail_rollback = trade_state.pop("_pending_trail_rollback", None)
                    old_sl = trade_state.get("sl")

                    # Safety: never move SL backward (away from profit)
                    direction = trade_state.get("direction")
                    if direction == "long" and old_sl is not None and new_sl < old_sl:
                        logger.warning(f"TRAIL_SL: Refusing backward SL move for LONG {symbol}: {old_sl} -> {new_sl}")
                        continue
                    if direction == "short" and old_sl is not None and new_sl > old_sl:
                        logger.warning(f"TRAIL_SL: Refusing backward SL move for SHORT {symbol}: {old_sl} -> {new_sl}")
                        continue

                    logger.info(f"Trailing SL for {symbol}: {trail_label}  old_sl={old_sl} -> new_sl={new_sl}")
                    broker_ok = False
                    applied_sl = None
                    if hasattr(self.broker, "modify_sl"):
                        try:
                            applied_sl = await self._modify_stop(item, new_sl)
                            broker_ok = True
                        except Exception as e:
                            logger.error(f"Failed to modify SL for {symbol}: {e}")
                    elif hasattr(self.broker, "move_sl_to_be"):
                        try:
                            await asyncio.to_thread(self.broker.move_sl_to_be, symbol)
                            broker_ok = True
                        except Exception as e:
                            logger.error(f"Failed to trail SL for {symbol}: {e}")
                    else:
                        broker_ok = True  # No broker method, update state anyway

                    # Only update in-memory state if broker confirmed
                    if broker_ok:
                        # The adapter may clamp the SL to the broker's minimum
                        # stop distance — track the level the broker holds.
                        if isinstance(applied_sl, (int, float)) and applied_sl > 0:
                            new_sl = float(applied_sl)
                        trade_state["sl"] = new_sl
                        # Record the trail step on the trade doc (SL → 1 → 2 → 3)
                        # for the Trading Analysis table. Failure-isolated.
                        try:
                            from packages.db.repositories.comprehensive_logging import append_sl_trail
                            await append_sl_trail(
                                symbol, new_sl, label=trail_label,
                                position_ticket=item.get("position_ticket") or None,
                                trade_id=item.get("trade_id") or None,
                            )
                        except Exception:
                            pass
                    elif trail_rollback:
                        # Broker rejected the move: restore the step state so
                        # the same level re-fires next candle instead of being
                        # silently skipped until the next (higher) threshold.
                        trade_state.update(trail_rollback)
                        logger.warning(
                            f"TRAIL_SL: broker rejected SL move for {symbol} — trail step restored for retry next candle"
                        )
                    # Persist the trail cursor + new SL so recovery resumes here.
                    await update_monitor_state(pos_ticket, trade_state=trade_state, expiry=expiry)
                    continue

                if action == MOVE_SL_TO_BE:
                    # Move SL to BE + 0.5R (locks in half-risk as profit)
                    # Matches backtest trade_sim behaviour
                    entry = trade_state.get("entry")
                    initial_stop = trade_state.get("initial_stop") or trade_state.get("sl")
                    risk = abs(entry - initial_stop) if entry and initial_stop else 0
                    direction = trade_state.get("direction")
                    if direction == "long":
                        be_price = entry + 0.5 * risk
                    else:
                        be_price = entry - 0.5 * risk
                    logger.info(f"Moving SL to BE+0.5R for {symbol} at {be_price} (entry={entry}, risk={risk})")
                    
                    # Step 1: partial close 50% at TP1 (must succeed before moving SL)
                    partial_ok = False
                    min_lot_skip = False
                    # Live MT5 broker: partial close 50% of position at TP1
                    if hasattr(self.broker, "partial_close"):
                        try:
                            closed = await self._partial_exit(item, 0.5, "tp1")
                            # partial_close returns the FILLED volume, or False
                            # when the position is at min-lot and cannot be
                            # split — do NOT record a phantom partial or
                            # decrement in-memory quantity then.
                            partial_ok = isinstance(closed, (int, float)) and not isinstance(closed, bool) and 0 < float(closed) <= float(trade_state.get("quantity", 0))
                            min_lot_skip = closed is False
                            # The broker floors to lot_step and clamps to
                            # min_lot, so 0.07 * 0.5 fills 0.03, not 0.035.
                            # Book what filled (audit C3).
                            filled_qty = float(closed) if partial_ok else 0.0
                            if partial_ok:
                                logger.info(f"TP1 partial close 50% executed for {symbol}")
                            else:
                                logger.info(
                                    f"TP1 partial skipped for {symbol} — position at min-lot, cannot split"
                                )
                        except Exception as e:
                            logger.error(f"Failed to partial close 50% for {symbol}: {e}")

                        if partial_ok:
                            # Record partial exit in MongoDB (contract-size-correct
                            # P&L + full linkage to the parent decision).
                            try:
                                from packages.db.repositories.comprehensive_logging import store_comprehensive_trade
                                partial_qty = filled_qty
                                entry_price = trade_state.get("entry", 0)
                                direction = trade_state.get("direction", "long")
                                partial_pnl = _usd_pnl(
                                    trade_state.get("asset", symbol), direction,
                                    entry_price, float(close), partial_qty,
                                )
                                await store_comprehensive_trade(
                                    symbol=symbol,
                                    asset=trade_state.get("asset", symbol),
                                    quantity=partial_qty,
                                    price=float(close),
                                    entry_price=entry_price,
                                    agent_id=item.get("agent_id") or item.get("prompt_hash", ""),
                                    prompt_hash=item.get("prompt_hash", ""),
                                    signal_received_at=start_time,
                                    trade_executed_at=datetime.now(timezone.utc),
                                    exit_price=float(close),
                                    trade_type="live",
                                    side=_norm_side(direction),
                                    broker_type="MT5",
                                    gpt_mode=item.get("gpt_mode", ""),
                                    trade_closed_at=datetime.now(timezone.utc),
                                    pnl=partial_pnl,
                                    trade_status="closed",
                                    exit_reason="TP1_PARTIAL",
                                    stop_loss=trade_state.get("initial_stop") or trade_state.get("sl"),
                                    take_profit=rules.get("tp1"),
                                    cycle_id=item.get("cycle_id") or None,
                                    gpt_response_id=item.get("gpt_response_id") or None,
                                    position_ticket=item.get("position_ticket") or None,
                                    parent_trade_id=item.get("trade_id") or None,
                                )
                                logger.info(f"MT5 LIVE: TP1 partial exit recorded for {symbol}", extra={
                                    "symbol": symbol, "partial_qty": partial_qty,
                                    "exit_price": float(close), "pnl": partial_pnl,
                                })
                            except Exception as e:
                                logger.error(f"Failed to record MT5 partial exit for {symbol}: {e}")
                    
                    # Step 2: move SL to BE+0.5R ONLY if partial close succeeded
                    # min-lot skip: no volume was closed (nothing to record),
                    # but still protect the position with the BE+0.5R move and
                    # mark TP1 handled so this branch doesn't re-fire forever.
                    if not partial_ok and not min_lot_skip:
                        logger.error(f"Skipping SL move for {symbol} — partial close failed, keeping original SL to protect full position")
                    else:
                        if partial_ok:
                            trade_state["quantity"] = max(
                                0.0, trade_state.get("quantity", 0) - filled_qty
                            )
                        # TP1 is consumed by the successful partial (or the
                        # honest min-lot skip) regardless of whether the SL
                        # move below succeeds — if tp1_hit stayed False after a
                        # failed modify_sl, MOVE_SL_TO_BE would re-fire next
                        # candle and partial-close the remaining volume again.
                        trade_state["tp1_hit"] = True
                        sl_ok = False
                        applied_be = None
                        if hasattr(self.broker, "modify_sl"):
                            try:
                                applied_be = await self._modify_stop(item, be_price)
                                sl_ok = True
                            except Exception as e:
                                logger.error(f"Failed to move SL to BE+0.5R for {symbol}: {e}")
                        elif hasattr(self.broker, "move_sl_to_be"):
                            try:
                                await asyncio.to_thread(self.broker.move_sl_to_be, symbol)
                                sl_ok = True
                            except Exception as e:
                                logger.error(f"Failed to move SL to BE for {symbol}: {e}")
                        else:
                            sl_ok = True

                        # Only update in-memory state if broker operations confirmed
                        if sl_ok:
                            if isinstance(applied_be, (int, float)) and applied_be > 0:
                                be_price = float(applied_be)
                            trade_state["sl"] = be_price
                            trade_state["sl_moved"] = True
                            # Record the breakeven move on the trade doc's trail
                            # history for the Trading Analysis table.
                            try:
                                from packages.db.repositories.comprehensive_logging import append_sl_trail
                                await append_sl_trail(
                                    symbol, be_price, label="BE+0.5R",
                                    position_ticket=item.get("position_ticket") or None,
                                    trade_id=item.get("trade_id") or None,
                                )
                            except Exception:
                                pass

                    if self.journal and hasattr(self.journal, "log_action"):
                        self.journal.log_action({"action": action}, item["prompt_hash"], item["gpt_mode"], item["latency_ms"])
                    # Persist post-TP1 state (halved qty, BE SL, tp1_hit) so a
                    # restart never re-books the partial.
                    await update_monitor_state(pos_ticket, trade_state=trade_state, expiry=expiry)
                    continue

                if action == PARTIAL_TP2:
                    # TP2 reached with the runner enabled: close a fraction of
                    # the remaining position, keep the rest trailed by the
                    # chandelier. Falls back to a full close when the position
                    # is at min-lot and cannot be split.
                    entry_price = trade_state.get("entry", 0)
                    direction = trade_state.get("direction", "long")
                    tp2_partial_done = False
                    if hasattr(self.broker, "partial_close"):
                        try:
                            closed = await self._partial_exit(item, TP2_PARTIAL_FRACTION, "tp2")
                            tp2_partial_done = isinstance(closed, (int, float)) and not isinstance(closed, bool) and 0 < float(closed) <= float(trade_state.get("quantity", 0))
                            # Filled, not requested — same reason as TP1 above.
                            tp2_filled_qty = float(closed) if tp2_partial_done else 0.0
                        except Exception as e:
                            logger.error(f"TP2 partial close failed for {symbol}: {e}")
                            continue  # An order error is not a minimum-lot refusal.

                    if not tp2_partial_done:
                        # Cannot split — preserve the pre-runner behavior:
                        # full close at TP2 (handled by the EXIT block below).
                        logger.info(
                            f"TP2 runner unavailable for {symbol} (cannot split) — full close at TP2"
                        )
                        action = EXIT_TP
                    else:
                        closed_qty = tp2_filled_qty
                        partial_pnl = _usd_pnl(
                            trade_state.get("asset", symbol), direction,
                            entry_price, float(close), closed_qty,
                        )
                        trade_state["quantity"] = max(
                            0.0, trade_state.get("quantity", 0) - closed_qty
                        )
                        trade_state["tp2_hit"] = True
                        # Seed the chandelier from this bar's extreme.
                        trade_state["runner_extreme"] = float(high) if direction == "long" else float(low)
                        # If TP1 never fired (gap straight through), lock
                        # BE+0.5R now — the runner must be risk-free.
                        if not trade_state.get("tp1_hit"):
                            initial_stop = trade_state.get("initial_stop") or trade_state.get("sl")
                            risk = abs(entry_price - initial_stop) if entry_price and initial_stop else 0
                            be_price = entry_price + 0.5 * risk if direction == "long" else entry_price - 0.5 * risk
                            try:
                                if hasattr(self.broker, "modify_sl"):
                                    applied = await self._modify_stop(item, be_price)
                                    if isinstance(applied, (int, float)) and not isinstance(applied, bool) and applied > 0:
                                        trade_state["sl"] = applied
                                        trade_state["tp1_hit"] = True
                            except Exception as e:
                                logger.error(f"Failed to lock BE after TP2 partial for {symbol}: {e}")
                        # Runner gets more room than the 4h default — extend
                        # monitoring (pre-close windows still force-close gaps).
                        expiry = max(expiry, min(
                            start_time + timedelta(hours=RUNNER_MAX_HOURS),
                            datetime.now(timezone.utc) + timedelta(hours=RUNNER_MAX_HOURS),
                        ))
                        if hard_expiry:
                            expiry = min(expiry, hard_expiry)
                        try:
                            from packages.db.repositories.comprehensive_logging import store_comprehensive_trade
                            await store_comprehensive_trade(
                                symbol=symbol,
                                asset=trade_state.get("asset", symbol),
                                quantity=closed_qty,
                                price=float(close),
                                entry_price=entry_price,
                                agent_id=item.get("agent_id") or item.get("prompt_hash", ""),
                                prompt_hash=item.get("prompt_hash", ""),
                                signal_received_at=start_time,
                                trade_executed_at=datetime.now(timezone.utc),
                                exit_price=float(close),
                                trade_type="live",
                                side=_norm_side(direction),
                                broker_type="MT5",
                                gpt_mode=item.get("gpt_mode", ""),
                                trade_closed_at=datetime.now(timezone.utc),
                                pnl=partial_pnl,
                                trade_status="closed",
                                exit_reason="TP2_PARTIAL",
                                stop_loss=trade_state.get("initial_stop") or trade_state.get("sl"),
                                take_profit=rules.get("tp"),
                                cycle_id=item.get("cycle_id") or None,
                                gpt_response_id=item.get("gpt_response_id") or None,
                                position_ticket=item.get("position_ticket") or None,
                                parent_trade_id=item.get("trade_id") or None,
                            )
                        except Exception as e:
                            logger.error(f"Failed to record TP2 partial for {symbol}: {e}")
                        logger.info(
                            f"TP2 partial booked for {symbol} — runner active",
                            extra={
                                "symbol": symbol,
                                "closed_qty": closed_qty,
                                "remaining_qty": trade_state.get("quantity"),
                                "pnl": partial_pnl,
                                "trail_dist": rules.get("runner_trail_dist"),
                                "new_expiry": expiry.isoformat(),
                            },
                        )
                        # Persist runner state (reduced qty, tp2_hit, extended
                        # expiry) so the trailed runner survives a restart.
                        await update_monitor_state(pos_ticket, trade_state=trade_state, expiry=expiry)
                        continue

                if action in {EXIT_TP, EXIT_SL}:
                    logger.info(f"Trade exit triggered for {symbol}: {action} at price {close}")

                    if action == EXIT_TP:
                        exit_reason = "TP2"
                    elif trade_state.get("tp2_hit"):
                        exit_reason = "RUNNER_TRAIL"  # runner ended on its ratcheted stop
                    else:
                        exit_reason = "SL"
                    entry_price = trade_state.get("entry", 0)
                    direction = trade_state.get("direction", "long")
                    quantity = trade_state.get("quantity", 0)

                    # The SL/TP level is only the *intent*. The SL is native at
                    # the broker (fills at the stop) and TP2 is a level too, but
                    # the app-driven close fills at MARKET — recording the planned
                    # level + a price-derived P&L overstated the exit whenever the
                    # bar ran past the level or the close slipped (and that wrong
                    # P&L also feeds the AI's RECENT_TRADES context). The level is
                    # kept only as the fallback for the real fill read back below.
                    if action == EXIT_SL:
                        _exit_lvl = trade_state.get("sl")
                    elif action == EXIT_TP:
                        _exit_lvl = rules.get("tp")
                    else:
                        _exit_lvl = None
                    planned_px = (
                        float(_exit_lvl)
                        if isinstance(_exit_lvl, (int, float)) and _exit_lvl > 0
                        else float(close)
                    )

                    # Close at market FIRST so the real closing deal exists, then
                    # record the ACTUAL fill price + realised money from MT5.
                    if hasattr(self.broker, "close"):
                        try:
                            await self._close_position(item)
                        except Exception as e:
                            logger.error(f"Failed to close trade for {symbol}: {e}")

                    exit_px, pnl, deal_time, _real = await self._resolve_real_exit(
                        symbol,
                        item.get("position_ticket") or None,
                        asset=trade_state.get("asset", symbol),
                        direction=direction,
                        entry=entry_price,
                        planned_exit=planned_px,
                        quantity=quantity,
                    )

                    # UPDATE the original trade document in MongoDB
                    try:
                        from packages.db.repositories.comprehensive_logging import update_comprehensive_trade_exit
                        updated = await self._record_trade_exit(
                            symbol=symbol,
                            exit_price=exit_px,
                            exit_reason=exit_reason,
                            pnl=pnl,
                            trade_closed_at=deal_time,
                            trade_id=item.get("trade_id") or None,
                            position_ticket=item.get("position_ticket") or None,
                        )
                        if updated:
                            logger.info(f"MongoDB trade updated with exit: {symbol} {exit_reason} pnl={pnl:.2f}")
                        else:
                            logger.warning(f"No open MongoDB trade found to update for {symbol}")
                    except Exception as e:
                        logger.error(f"Failed to update trade exit in MongoDB for {symbol}: {e}")

                    if self.journal and hasattr(self.journal, "log_action"):
                        self.journal.log_action({"action": action}, item["prompt_hash"], item["gpt_mode"], item["latency_ms"])

                    # Notify that trade is closed
                    if self.on_trade_closed:
                        try:
                            self.on_trade_closed(symbol, action, exit_px)
                        except Exception as e:
                            logger.error(f"Error in trade closed callback for {symbol}: {e}")

                    await clear_monitor_state(pos_ticket, exit_reason)
                    break

            except Exception as e:
                logger.error(f"Error in trade monitoring for {symbol}: {e}")
                # Continue monitoring despite errors
                await asyncio.sleep(1)
                
        logger.info(f"Trade monitoring completed for {symbol} - expired or exited")
        
        # If the loop exited because of expiry (not a break from EXIT_TP/EXIT_SL),
        # close the position on the broker and notify.
        if datetime.now(timezone.utc) >= expiry and not self._shutdown:
            # Timeout: close the remaining position at market
            logger.info(f"Trade EXPIRED for {symbol} — closing position at market")

            entry_price = trade_state.get("entry", 0)
            direction = trade_state.get("direction", "long")
            quantity = trade_state.get("quantity", 0)
            planned_px = float(close) if close is not None else entry_price

            # Close at market FIRST (the real fill is read back afterwards), so
            # the recorded TIMEOUT P&L is the broker's realised money, not a
            # price-derived estimate at the last candle close.
            if hasattr(self.broker, "close"):
                # Live broker: retry close if market is closed (retcode 10018)
                max_close_retries = 3
                for attempt in range(max_close_retries):
                    closed_now, wait_sec = _is_market_closed(symbol)
                    if closed_now:
                        logger.info(
                            f"Market closed for {symbol} — waiting {wait_sec + 30}s before retry "
                            f"(attempt {attempt + 1}/{max_close_retries})"
                        )
                        await asyncio.sleep(min(wait_sec + 30, 600))
                    try:
                        await self._close_position(item)
                        logger.info(f"Successfully closed expired trade for {symbol}")
                        break
                    except Exception as e:
                        if "market closed" in str(e).lower() or "10018" in str(e):
                            if attempt < max_close_retries - 1:
                                logger.warning(
                                    f"Market closed when closing {symbol}, will retry after reopen "
                                    f"(attempt {attempt + 1}/{max_close_retries})"
                                )
                                _, wait_sec = _is_market_closed(symbol)
                                await asyncio.sleep(max(wait_sec + 30, 60))
                            else:
                                logger.error(f"Failed to close expired trade for {symbol} after {max_close_retries} attempts: {e}")
                        else:
                            logger.error(f"Failed to close expired trade for {symbol}: {e}")
                            break

            # Record the close with the ACTUAL fill price + realised money.
            exit_px, pnl, deal_time, _real = await self._resolve_real_exit(
                symbol,
                item.get("position_ticket") or None,
                asset=trade_state.get("asset", symbol),
                direction=direction,
                entry=entry_price,
                planned_exit=planned_px,
                quantity=quantity,
            )
            try:
                from packages.db.repositories.comprehensive_logging import update_comprehensive_trade_exit
                await self._record_trade_exit(
                    symbol=symbol,
                    exit_price=exit_px,
                    exit_reason="TIMEOUT",
                    pnl=pnl,
                    trade_closed_at=deal_time,
                    trade_id=item.get("trade_id") or None,
                    position_ticket=item.get("position_ticket") or None,
                )
            except Exception as e:
                logger.error(f"Failed to update expired trade exit in MongoDB for {symbol}: {e}")

            if self.on_trade_closed:
                try:
                    self.on_trade_closed(symbol, "EXPIRED", exit_px)
                except Exception as e:
                    logger.error(f"Error in trade expired callback for {symbol}: {e}")

            await clear_monitor_state(pos_ticket, "TIMEOUT")

    async def shutdown(self) -> None:
        """Gracefully shutdown the trade monitor."""
        if self._shutdown:
            return
            
        self._shutdown = True
        
        # Cancel the main task
        if not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        
        # Wait for shutdown to complete
        await self._shutdown_event.wait()
    
    async def __aenter__(self):
        """Async context manager entry."""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit - ensures cleanup."""
        await self.shutdown()

__all__ = ["TradeMonitor"]
