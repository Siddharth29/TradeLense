"""Multiplex executable quotes and closed bars into ONE position owner."""
from __future__ import annotations

import asyncio
import math
import logging
import time
from datetime import datetime, timezone

from .entry_policy import evaluate_entry_policy
from .exit_policy import quote_action
from .operational_ledger import live_ledger, utc
from .trade_monitor import is_risk_free, unrealised_r


class ManagementAccountUnavailable(RuntimeError):
    """Account identity must be established before interpreting a live quote."""


class ManagementEventFeed:
    def __init__(self, broker, symbol, bar_source, *, mode="shadow", interval=2., record=None):
        if mode not in ("shadow", "live") or not math.isfinite(interval) or not .05 <= interval <= 30:
            raise ValueError("Invalid fast management mode / interval")
        self.broker, self.symbol, self.bar_source = broker, symbol, bar_source
        self.mode, self.interval, self.record = mode, interval, record
        self.bar_task = None
        self.last_quote_at = None

    async def _record(self, kind, payload):
        if self.record:
            try:
                await asyncio.to_thread(self.record, kind, payload)
            except ManagementAccountUnavailable:
                raise
            except Exception:
                # Quote telemetry is not order authorization. A full disk or
                # trace-write error must not suppress exits on an existing trade.
                logging.getLogger(__name__).exception("Management telemetry write failed")

    async def next(self, state, rules):
        if self.bar_task is None:
            self.bar_task = asyncio.create_task(self.bar_source())
        done, _ = await asyncio.wait({self.bar_task}, timeout=self.interval)
        if done:
            task, self.bar_task = self.bar_task, None
            return task.result()  # Exceptions are handled by the existing owner.
        started = time.monotonic()
        try:
            quote = await asyncio.to_thread(self.broker.executable_quote, self.symbol, state["direction"])
            price, stamp = float(quote["price"]), utc(quote["time"])
            now = datetime.now(timezone.utc)
            age = (now-stamp).total_seconds()
            if not math.isfinite(price) or price <= 0 or not -2 <= age <= 10:
                raise ValueError("Stale or invalid executable quote")
            if self.last_quote_at is not None and stamp <= self.last_quote_at:
                return {"source": "heartbeat"}
            self.last_quote_at = stamp
            action = quote_action(state, rules, price)
            risk_state = "protected_runner" if state.get("tp2_hit") and is_risk_free(state) else "exposed"
            trace = {"quote_at": stamp.isoformat(), "observed_at": now.isoformat(), "price": price,
                     "quote_age_ms": age*1000, "fetch_ms": (time.monotonic()-started)*1000,
                     "mode": self.mode, "action": action, "family": rules.get("family", "legacy"),
                     "entry": state.get("entry"), "initial_stop": state.get("initial_stop"),
                     "unrealised_r": unrealised_r(state, price), "quantity": state.get("quantity"),
                     "sl": state.get("sl"), "event_policy": evaluate_entry_policy(self.symbol, now, position=risk_state)}
            await self._record("quote", trace)
            return {"source": "quote", "time": stamp, "close": price, "high": price, "low": price,
                    "quote_action": action if self.mode == "live" else None}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                await self._record("quote_failure", {"error": str(exc)})
            except ManagementAccountUnavailable:
                logging.getLogger(__name__).error("Management account identity unavailable")
            # Bar task survives a failed quote; no stale price is returned.
            return {"source": "heartbeat"}

    async def close(self):
        if self.bar_task is not None:
            self.bar_task.cancel()
            await asyncio.gather(self.bar_task, return_exceptions=True)
            self.bar_task = None


def position_recorder(broker, position_id):
    """Bind traces to the account observed at startup; recheck before recording."""
    from .operational_ledger import sync_broker
    account, ledger = sync_broker(broker)
    ledger.close()
    def record(kind, payload):
        try:
            info = broker._mt5.account_info()
            matches = info is not None and f"mt5:{info.server}:{info.login}" == account
        except Exception as exc:
            raise ManagementAccountUnavailable("Account identity unavailable during management") from exc
        if not matches:
            raise ManagementAccountUnavailable("Account changed during position management")
        db = live_ledger()
        try:
            db.observe(account, position_id, kind, {**payload, "account_currency": getattr(info, "currency", None)})
        finally:
            db.close()
    return record
