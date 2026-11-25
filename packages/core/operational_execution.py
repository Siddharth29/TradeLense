"""Durable acknowledgement boundaries for position management."""
from __future__ import annotations

import math
from datetime import datetime, timezone

from .operational_ledger import sync_broker


class ExitNotConfirmed(RuntimeError):
    pass


def managed_partial(broker, position_id, symbol, fraction, kind, expected_quantity=None):
    """One owner per position, persisted intent BEFORE broker submission.

    A crash/ambiguous response is reconciled from volume before another action.
    The owner must call reconcile_management before evaluating targets again.
    """
    from packages.adapters.mt5_adapter import OrderReconciliationRequired
    account, ledger = sync_broker(broker)
    try:
        prior = ledger.operations(account, position_id).get(kind)
        if prior and prior["status"] == "refused":
            return False
        if prior and prior["status"] != "rejected":
            raise ExitNotConfirmed("Partial intent already exists; reconcile before acting")
        position = ledger.position(account, position_id)
        if not position or position["closed"]:
            raise ExitNotConfirmed("Owned open position unavailable for partial")
        if expected_quantity is not None and abs(position["remaining_quantity"]-expected_quantity) > 1e-8:
            raise ExitNotConfirmed("Position quantity and broker deal history have not reconciled")
        intent = {"status": "pending", "before": position["remaining_quantity"],
                  "requested_at": datetime.now(timezone.utc).isoformat(), "fraction": fraction}
        ledger.set_operation(account, position_id, kind, intent)
        try:
            filled = broker.partial_close(symbol, fraction, position_ticket=position_id)
            if filled is not False:
                try:
                    valid = not isinstance(filled, bool) and math.isfinite(float(filled)) and 0 < float(filled) <= intent["before"]
                except (TypeError, ValueError):
                    valid = False
                if not valid:
                    raise OrderReconciliationRequired("Partial receipt has no valid confirmed quantity")
        except OrderReconciliationRequired:
            raise  # Leave pending; never resubmit an uncertain order.
        except Exception:
            ledger.set_operation(account, position_id, kind, {**intent, "status": "rejected"})
            raise
        receipts = getattr(broker, "_partial_receipts", {})
        receipt = receipts.get(str(position_id), {}) if isinstance(receipts, dict) and filled is not False else {}
        ledger.set_operation(account, position_id, kind,
                             {**intent, "status": "refused" if filled is False else "acknowledged", "filled": filled,
                              "receipt": receipt})
        ledger.observe(account, position_id, "partial_receipt", {"kind": kind, "filled": filled, "receipt": receipt,
                       "requested_at": intent["requested_at"], "received_at": datetime.now(timezone.utc).isoformat()})
        try:
            from .config import MT5_MAGIC_NUMBER
            after_account, deals = broker.operational_snapshot()
            if after_account != account:
                raise ExitNotConfirmed("Account changed after partial receipt")
            ledger.ingest(account, deals, magic=MT5_MAGIC_NUMBER)
        except Exception as exc:
            # Receipt is already durable. Preserve its actual quantity even if
            # deal history is eventually consistent; subsequent controls reconcile.
            ledger.observe(account, position_id, "reconciliation_pending", {"error": str(exc)})
        return filled
    finally:
        ledger.close()


def reconcile_management(broker, position_id, state):
    """Return a patch for consumed partials; refuse to guess an ambiguous send."""
    account, ledger = sync_broker(broker)
    try:
        position = ledger.position(account, position_id)
        if not position:
            raise ExitNotConfirmed("Position history incomplete")
        patch = {}
        for kind, op in ledger.operations(account, position_id).items():
            if op["status"] == "rejected":
                continue
            if op["status"] == "pending":
                delta = op["before"]-position["remaining_quantity"]
                if delta <= 1e-8:
                    raise ExitNotConfirmed("Uncertain partial has no confirmed volume change; retaining state")
                op = {**op, "status": "acknowledged", "filled": delta}
                ledger.set_operation(account, position_id, kind, op)
            if op["status"] in ("acknowledged", "refused"):
                if op["status"] == "acknowledged" and position["remaining_quantity"] > op["before"]-op["filled"]+1e-8:
                    raise ExitNotConfirmed("Acknowledged partial is not yet visible in deal history")
                if kind == "tp1":
                    patch["tp1_hit"] = True
                elif kind == "tp2" and op["status"] == "acknowledged":
                    patch["tp2_hit"] = True
                    # Do not assert TP1 protection: the actual SL may still be original.
        if patch:
            patch["quantity"] = position["remaining_quantity"]
        return patch
    finally:
        ledger.close()
