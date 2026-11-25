"""Read-only pull from a locally configured MT5 account.

NO orders are ever placed. Credentials must be supplied through the gitignored
``config/mt5_config.local.json`` file; the public placeholder is never used.

Usage:
    python scripts/audit/pull_live_mt5.py --from 2026-06-28 --to 2026-07-02 \
        --symbols USTEC USOIL XAUUSD XAGUSD BTCUSD --out data/audit/mktdata

Outputs (per symbol): <OUT>/<SYM>_5m.csv, <SYM>_15m.csv, plus <OUT>/deals.csv.
Candle timestamps (`dt_mt5`) are already true UTC for this account (verified offset = 0).
"""
import argparse, json, os, sys
from datetime import datetime, timezone
import MetaTrader5 as mt5
import pandas as pd

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def load_live_creds():
    """LIVE creds only. Fails loudly if the local (gitignored) config is missing —
    we must never silently fall back to the demo account for a loss audit."""
    p = os.path.join(REPO, "config", "mt5_config.local.json")
    if not os.path.exists(p):
        sys.exit(f"LIVE creds not found at {p}. Refusing to use demo config for an audit.")
    cfg = json.load(open(p))["mt5"]
    if "real" not in cfg["server"].lower():
        sys.exit(f"server '{cfg['server']}' is not a real account — refusing (2026-07-18: hard rule, was a warning)")
    return cfg["server"], int(cfg["login"]), cfg["password"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="frm", default="2026-06-28")
    ap.add_argument("--to", dest="to", default="2026-07-02")
    ap.add_argument("--symbols", nargs="+",
                    default=["USTEC", "USOIL", "XAUUSD", "XAGUSD", "BTCUSD", "EURUSD", "GBPUSD"])
    ap.add_argument("--out", default=os.path.join(REPO, "data", "audit", "mktdata"))
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    frm = datetime.fromisoformat(a.frm).replace(tzinfo=timezone.utc)
    to = datetime.fromisoformat(a.to).replace(tzinfo=timezone.utc)

    server, login, pw = load_live_creds()
    # Initialize WITH creds — never trust the terminal's ambient login
    # (2026-07-18: ambient state proved unstable mid-day; bare initialize()
    # attached to a session that later failed authorization).
    if not mt5.initialize(login=login, password=pw, server=server):
        if not mt5.initialize():
            sys.exit(f"mt5.initialize failed: {mt5.last_error()}")
        if not mt5.login(login=login, password=pw, server=server):
            sys.exit(f"mt5.login failed: {mt5.last_error()}")
    ai = mt5.account_info()
    if ai is None or ai.login != login or str(ai.server).casefold() != server.casefold():
        mt5.shutdown()
        sys.exit(f"ACCOUNT VERIFICATION FAILED: on {getattr(ai,'login',None)}@{getattr(ai,'server',None)}, expected {login}@{server}")
    print(f"ACTIVE ACCOUNT: {ai.login} {ai.server} balance={ai.balance} equity={ai.equity} {ai.currency}")

    # ---- authoritative realized P&L / commission / swap (reconciles to balance) ----
    deals = mt5.history_deals_get(frm, to)
    if deals is None:
        error = mt5.last_error()
        mt5.shutdown()
        sys.exit(f"Could not read broker deal history: {error}")
    recs = [dict(time=datetime.fromtimestamp(d.time, tz=timezone.utc).isoformat(),
                 symbol=d.symbol or "(balance)", type=d.type, entry=d.entry, volume=d.volume,
                 price=d.price, profit=d.profit, commission=d.commission, swap=d.swap,
                 magic=d.magic, reason=d.reason,
                 order=d.order, position_id=d.position_id, comment=d.comment,
                 ticket=d.ticket, fee=d.fee, time_msc=d.time_msc) for d in deals]
    deal_columns = ["time", "symbol", "type", "entry", "volume", "price", "profit",
                    "commission", "swap", "magic", "reason", "order", "position_id",
                    "comment", "ticket", "fee", "time_msc"]
    pd.DataFrame(recs, columns=deal_columns).to_csv(os.path.join(a.out, "deals.csv"), index=False)
    if deals:
        tp = sum(d.profit for d in deals); tc = sum(d.commission for d in deals); ts = sum(d.swap for d in deals)
        fees = sum(d.fee for d in deals)
        print(f"deals={len(deals)}  profit={tp:+.2f} commission={tc:+.2f} "
              f"swap={ts:+.2f} fee={fees:+.2f} NET={tp+tc+ts+fees:+.2f}")
    else:
        print("No broker deals returned for this window.")

    # ---- candles (try plain symbol, then 'm' suffix, then '.raw') ----
    def dl(sym, tf):
        r = mt5.copy_rates_range(sym, tf, frm, to)
        if r is None or len(r) == 0:
            return None
        df = pd.DataFrame(r)
        df["dt_mt5"] = pd.to_datetime(df["time"], unit="s", utc=True)   # true UTC for this account
        return df

    for base in a.symbols:
        for cand in [base, base + "m", base + ".raw"]:
            if mt5.symbol_info(cand) is None:
                continue
            mt5.symbol_select(cand, True)
            df5 = dl(cand, mt5.TIMEFRAME_M5)
            if df5 is None:
                continue
            df5.to_csv(os.path.join(a.out, f"{base}_5m.csv"), index=False)
            df15 = dl(cand, mt5.TIMEFRAME_M15)
            if df15 is not None:
                df15.to_csv(os.path.join(a.out, f"{base}_15m.csv"), index=False)
            print(f"{base:8} via '{cand}': 5m={len(df5)} [{df5['dt_mt5'].iloc[0]}..{df5['dt_mt5'].iloc[-1]}]")
            break
        else:
            print(f"{base:8}: NO symbol found (tried plain/m/.raw)")
    mt5.shutdown()
    print("DONE ->", a.out)


if __name__ == "__main__":
    main()
