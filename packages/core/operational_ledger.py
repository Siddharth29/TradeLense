"""Account-scoped operational state, independent of optional analytics/Mongo.

SQLite WAL supports processes on one host; use one shared local path per account
deployment. Replay creates its own database and must never use the live default.
Broker deal IDs are immutable keys; projections are rebuilt transactionally.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .asset_registry import profile_for


def utc(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def asset_key(symbol):
    profile = profile_for(symbol)
    return profile.symbol if profile else symbol.upper()


class OperationalLedger:
    def __init__(self, path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS deals (
              account TEXT NOT NULL, deal_id TEXT NOT NULL, position_id TEXT NOT NULL,
              payload TEXT NOT NULL, PRIMARY KEY(account, deal_id));
            CREATE INDEX IF NOT EXISTS deals_position ON deals(account,position_id);
            CREATE TABLE IF NOT EXISTS positions (
              account TEXT NOT NULL, position_id TEXT NOT NULL, asset TEXT NOT NULL,
              opened TEXT NOT NULL, closed TEXT, payload TEXT NOT NULL,
              PRIMARY KEY(account,position_id));
            CREATE TABLE IF NOT EXISTS observations (
              id INTEGER PRIMARY KEY, account TEXT NOT NULL, position_id TEXT,
              at TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operations (
              account TEXT NOT NULL, position_id TEXT NOT NULL, kind TEXT NOT NULL,
              payload TEXT NOT NULL, PRIMARY KEY(account,position_id,kind));
            CREATE TABLE IF NOT EXISTS position_aliases (
              account TEXT NOT NULL, ticket TEXT NOT NULL, position_id TEXT NOT NULL,
              PRIMARY KEY(account,ticket));
        """)

    def close(self):
        self.db.close()

    def ingest(self, account, deals, *, magic):
        if not account:
            raise ValueError("Operational ledger requires account identity")
        touched = set()
        with self.db:
            for raw in deals:
                d = dict(raw)
                pid = str(d.get("position_id") or "")
                if not pid or pid == "0":
                    continue  # Deposits and account-level charges are not position deals.
                deal_id = str(d.get("ticket") or "")
                if not deal_id:
                    raise ValueError("Broker deal has no identity")
                for field in ("volume", "price", "profit", "commission", "swap", "fee"):
                    d[field] = float(d.get(field) or 0.)
                    if not math.isfinite(d[field]):
                        raise ValueError(f"Nonfinite broker {field}")
                if d["volume"] < 0 or (d["volume"] > 0 and d["price"] <= 0):
                    raise ValueError("Invalid broker volume/price")
                d["time"] = utc(float(d["time_msc"])/1000 if d.get("time_msc") else d["time"]).isoformat()
                payload = json.dumps(d, sort_keys=True, default=str)
                prior = self.db.execute("SELECT payload FROM deals WHERE account=? AND deal_id=?", (account, deal_id)).fetchone()
                if prior and prior[0] != payload:
                    raise ValueError("Broker deal identity changed; explicit reconciliation required")
                self.db.execute("INSERT OR IGNORE INTO deals VALUES(?,?,?,?)", (account, deal_id, pid, payload))
                touched.add(pid)
            for pid in touched:
                rows = [json.loads(r[0]) for r in self.db.execute(
                    "SELECT payload FROM deals WHERE account=? AND position_id=?", (account, pid))]
                rows.sort(key=lambda d: (d["time"], int(d.get("ticket", 0))))
                entries = [d for d in rows if int(d.get("entry", -1)) == 0 and d["volume"] > 0]
                if not entries and any(int(d.get("magic", -1)) == magic for d in rows):
                    raise ValueError("Owned broker deal has no opening history")
                if not entries or int(entries[0].get("magic", -1)) != magic:
                    continue  # Ownership comes from entry, including manually closed app trades.
                if any(int(d.get("entry", -1)) == 2 for d in rows):
                    raise ValueError("Netting reversal requires separate position episodes")
                exits = [d for d in rows if int(d.get("entry", -1)) in (1, 3) and d["volume"] > 0]
                entered = sum(d["volume"] for d in entries)
                exited = sum(d["volume"] for d in exits)
                if entered <= 0 or exited > entered + 1e-8:
                    raise ValueError("Broker position volumes do not reconcile")
                closed = exits[-1]["time"] if exits and abs(entered - exited) <= 1e-8 else None
                net = sum(d["profit"] + d["commission"] + d["swap"] + d["fee"] for d in rows)
                p = {"account": account, "position_id": pid, "asset": asset_key(entries[0]["symbol"]),
                     "symbol": entries[0]["symbol"], "entry_price": sum(d["price"]*d["volume"] for d in entries)/entered,
                     "quantity": entered, "remaining_quantity": max(0., entered-exited),
                     "pnl": net, "fees": sum(d["commission"]+d["swap"]+d["fee"] for d in rows),
                     "commission": sum(d["commission"] for d in rows),
                     "swap": sum(d["swap"] for d in rows),
                     "fee": sum(d["fee"] for d in rows),
                     "opened": entries[0]["time"], "closed": closed,
                     "side": "buy" if int(entries[0].get("type", 0)) == 0 else "sell",
                     "exit_price": exits[-1]["price"] if exits else None,
                     "last_exit_volume": exits[-1]["volume"] if exits else None,
                     "exit_reason": str(exits[-1].get("reason", "")) if exits else None,
                     "deal_ids": [str(d["ticket"]) for d in rows]}
                self.db.execute("INSERT OR REPLACE INTO positions VALUES(?,?,?,?,?,?)",
                                (account, pid, p["asset"], p["opened"], closed, json.dumps(p)))

    def position(self, account, position_id):
        row = self.db.execute("SELECT payload FROM positions WHERE account=? AND position_id=?",
                              (account, self.resolve_position_id(account, position_id))).fetchone()
        return json.loads(row[0]) if row else None

    def register_aliases(self, account, aliases):
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO position_aliases VALUES(?,?,?)",
                                [(account, str(ticket), str(pid)) for ticket, pid in aliases.items()])

    def resolve_position_id(self, account, ticket):
        row = self.db.execute("SELECT position_id FROM position_aliases WHERE account=? AND ticket=?",
                              (account, str(ticket))).fetchone()
        return row[0] if row else str(ticket)

    def recent(self, account, asset, now, limit=10):
        return [json.loads(r[0]) for r in self.db.execute(
            "SELECT payload FROM positions WHERE account=? AND asset=? AND closed<=? ORDER BY closed DESC LIMIT ?",
            (account, asset_key(asset), utc(now).isoformat(), limit))]

    def throttle(self, account, asset, now, *, cap, loss_threshold, cooldown_minutes):
        now = utc(now)
        day = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        count = self.db.execute("SELECT COUNT(*) FROM positions WHERE account=? AND asset=? AND opened>=? AND opened<=?",
                                (account, asset_key(asset), day, now.isoformat())).fetchone()[0]
        if cap > 0 and count >= cap:
            return True, f"daily trade cap reached ({count}/{cap})"
        recent = self.recent(account, asset, now, max(1, loss_threshold))
        if loss_threshold > 0 and len(recent) >= loss_threshold and all(p["pnl"] < 0 for p in recent):
            age = (now-utc(recent[0]["closed"])).total_seconds()/60
            if age < cooldown_minutes:
                return True, f"whole-position loss cooldown ({cooldown_minutes-age:.1f} minutes remaining)"
        return False, ""

    def recent_views(self, account, asset, now, limit=10):
        views = []
        for p in self.recent(account, asset, now, limit):
            age = max(0, int((utc(now)-utc(p["closed"])).total_seconds()/60))
            entry, exit_px = p["entry_price"], p["exit_price"]
            views.append({"ago_min": age, "bars_elapsed": age//5, "side": p["side"],
                          "entry": entry, "exit": exit_px,
                          "move_pct": abs(exit_px-entry)/entry*100 if entry and exit_px else None,
                          "outcome": p["exit_reason"], "pnl": p["pnl"],
                          "result": "win" if p["pnl"] > 0 else "loss" if p["pnl"] < 0 else "scratch"})
        return views

    def record_simulation(self, account, position):
        """Import only already-booked replay legs; never inspect future bars."""
        from .config import MT5_MAGIC_NUMBER
        pid = position.prediction.setdefault("operational_position_id", str(int(utc(position.entry_time).timestamp()*1000)))
        sim = position.simulator_for_replay()
        base = {"position_id": pid, "symbol": position.asset, "magic": MT5_MAGIC_NUMBER,
                "type": 0 if position.trade_state["direction"] == "long" else 1,
                "swap": 0., "fee": 0., "commission": 0., "profit": 0.}
        commission = sum(leg.pnl for leg in sim.result.legs if leg.reason == "COMMISSION")
        deals = [{**base, "ticket": int(pid)*1000, "entry": 0, "time": position.entry_time,
                  "volume": sim.initial_qty, "price": position.trade_state["entry"], "commission": commission}]
        for i, leg in enumerate(sim.result.legs, 1):
            if leg.reason == "COMMISSION":
                continue
            deals.append({**base, "ticket": int(pid)*1000+i, "entry": 1,
                          "time": leg.time, "volume": leg.qty, "price": leg.price,
                          "profit": leg.pnl, "reason": leg.reason})
        self.ingest(account, deals, magic=MT5_MAGIC_NUMBER)

    def observe(self, account, position_id, kind, payload, at=None):
        with self.db:
            self.db.execute("INSERT INTO observations(account,position_id,at,kind,payload) VALUES(?,?,?,?,?)",
                            (account, self.resolve_position_id(account, position_id or ""), utc(at or datetime.now(timezone.utc)).isoformat(),
                             kind, json.dumps(payload, default=str, sort_keys=True)))

    def operations(self, account, position_id):
        return {r[0]: json.loads(r[1]) for r in self.db.execute(
            "SELECT kind,payload FROM operations WHERE account=? AND position_id=?", (account, self.resolve_position_id(account, position_id)))}

    def set_operation(self, account, position_id, kind, payload):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO operations VALUES(?,?,?,?)",
                            (account, self.resolve_position_id(account, position_id), kind, json.dumps(payload, default=str)))


def live_ledger():
    default = Path(__file__).resolve().parents[2] / "data" / "operational" / "ledger.sqlite3"
    return OperationalLedger(os.environ.get("OPERATIONAL_LEDGER_PATH", str(default)))


def sync_broker(broker, now=None):
    """Called in a worker thread; a failed sync must not authorize new exposure."""
    from .config import MT5_MAGIC_NUMBER
    account, deals = broker.operational_snapshot(now=now)
    ledger = live_ledger()
    try:
        aliases = getattr(broker, "_operational_position_aliases", {})
        if isinstance(aliases, dict):
            ledger.register_aliases(account, aliases)
        ledger.ingest(account, deals, magic=MT5_MAGIC_NUMBER)
    except Exception:
        ledger.close()
        raise
    return account, ledger


def position_cost(ledger, account, ticket, currency):
    """Whole-position posted charges; positive debit, negative credit.

    The opening deal and every partial/final exit are included exactly once.
    Open positions report accrued charges, never a projected round-trip fee.
    """
    position = ledger.position(account, ticket)
    if not position or not currency:
        raise ValueError("Confirmed position charges/account currency unavailable")
    return {
        "total": -position["fees"], "commission": -position["commission"],
        "swap": -position["swap"], "fee": -position["fee"],
        "currency": currency, "status": "final" if position["closed"] else "accrued",
        "source": "broker_deals", "account": account,
        "position_id": position["position_id"], "deal_ids": position["deal_ids"],
    }


def broker_position_cost(broker, ticket):
    if not ticket:
        raise ValueError("Exact broker position ticket required for charges")
    account, ledger = sync_broker(broker)
    try:
        return position_cost(ledger, account, ticket, getattr(broker, "_operational_account_currency", None))
    finally:
        ledger.close()
