"""Read-only quote/fill conversion report. Never connects to or trades a broker."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from packages.core.operational_ledger import utc
from packages.core.asset_registry import profile_for
from packages.core.backtest_account import usd_pnl


def report(path):
    path = Path(path).resolve(strict=True)
    db = sqlite3.connect(path.as_uri()+"?mode=ro", uri=True)
    results = []
    try:
        for account, pid, payload in db.execute("SELECT account,position_id,payload FROM positions"):
            p = json.loads(payload)
            obs = [(kind, json.loads(data)) for kind, data in db.execute(
                "SELECT kind,payload FROM observations WHERE account=? AND position_id=? ORDER BY at,id", (account, pid))]
            quotes = [q for kind, q in obs if kind == "quote"]
            deals = [json.loads(r[0]) for r in db.execute("SELECT payload FROM deals WHERE account=? AND position_id=?", (account, pid))]
            operations = {k: json.loads(v) for k, v in db.execute("SELECT kind,payload FROM operations WHERE account=? AND position_id=?", (account, pid))}
            profile = profile_for(p["asset"])
            risk = None
            if quotes and profile and profile.quote_currency == "USD" and quotes[0].get("account_currency") == "USD":
                stop = quotes[0].get("initial_stop")
                if stop:
                    risk = abs(usd_pnl(p["asset"], "long" if p["side"] == "buy" else "short", p["entry_price"], stop, p["quantity"])) or None
            marked = []
            for q in quotes:
                if not risk:
                    continue
                known = [d for d in deals if utc(d["time"]) <= utc(q["quote_at"])]
                booked = sum(d["profit"]+d["commission"]+d["swap"]+d["fee"] for d in known)
                remaining = sum(d["volume"] for d in known if int(d["entry"]) == 0)-sum(d["volume"] for d in known if int(d["entry"]) in (1, 3))
                marked.append((booked+usd_pnl(p["asset"], "long" if p["side"] == "buy" else "short", p["entry_price"], q["price"], max(0., remaining)))/risk)
            slippage = []
            tail = None
            for kind, op in operations.items():
                receipt = op.get("receipt", {})
                match = next((d for d in deals if str(d["ticket"]) == str(receipt.get("deal_id"))), None)
                eligible = [q for q in quotes if q.get("mode") == "live" and q.get("action") and utc(q["quote_at"]) <= utc(op["requested_at"])]
                if match and eligible:
                    q = eligible[-1]
                    if 0 <= (utc(op["requested_at"])-utc(q["quote_at"])).total_seconds() <= 10:
                        sign = 1 if p["side"] == "buy" else -1
                        slippage.append({"kind": kind, "deal_id": match["ticket"], "quote_at": q["quote_at"],
                                         "fill_at": match["time"], "adverse_price_slippage": sign*(q["price"]-match["price"])})
                if kind == "tp2" and match:
                    later = [d for d in deals if (utc(d["time"]), int(d["ticket"])) > (utc(match["time"]), int(match["ticket"]))]
                    tail = sum(d["profit"]+d["commission"]+d["swap"]+d["fee"] for d in later)
            final_r = p["pnl"]/risk if p["closed"] and risk else None
            results.append({"account": account, "position_id": pid, "asset": p["asset"],
                            "closed": p["closed"], "net": p["pnl"], "signed_fees": p["fees"],
                            "sampled_quotes": len(quotes), "net_r": final_r,
                            "sampled_peak_net_r": max(marked) if marked else None,
                            "net_giveback_r": max(0., max(marked)-final_r) if marked and final_r is not None else None,
                            "stop_modification_failures": sum(k == "stop_receipt" and bool(v.get("error")) for k, v in obs),
                            "matched_partial_slippage": slippage, "runner_net_after_tp2_deal": tail,
                            "pending_operations": [k for k, v in operations.items() if v["status"] == "pending"]})
    finally:
        db.close()
    return {"positions": results, "scope": "Sampled executable quotes and broker-identified deals. No OHLC fill inference.",
            "r_basis": "Actual entry volume and original stop, using contract specs; only USD accounts / USD quote assets. Other R fields are null.",
            "limits": "Sampled peak is not a guaranteed attainable fill. Slippage requires an exact receipt deal ID and a live action quote within 10 seconds; unmatched exits are omitted."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ledger")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    Path(args.output).write_text(json.dumps(report(args.ledger), indent=2), encoding="utf-8")
