"""Download recent BTCUSD candles (5m/15m/1h/4h/1d) from MT5 -> data/processed CSVs.

Reuses the project's MT5Adapter. Trial6 creds passed via env or defaults below.
Usage: python scripts/dl_btc_recent.py --days 22
"""
import argparse, asyncio, os, sys
from datetime import datetime, timezone, timedelta
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import pandas as pd
from packages.adapters.mt5_adapter import MT5Adapter
from packages.core.config import DATA_DIR

CREDS = dict(
    server=os.getenv("MT5_SERVER", "Example-Demo"),
    login=os.getenv("MT5_LOGIN", "00000000"),
    password=os.getenv("MT5_PASSWORD", "CHANGE_ME"),
)


async def fetch(adapter, symbol, tf_const, start, end):
    mt5 = adapter._mt5
    sym = adapter._normalize_symbol(symbol)
    raw = await asyncio.wait_for(
        asyncio.to_thread(mt5.copy_rates_range, sym, tf_const, start, end),
        timeout=180.0,
    )
    if raw is None or len(raw) == 0:
        return pd.DataFrame()
    rows = [{
        "time": pd.to_datetime(int(r["time"]), unit="s", utc=True),
        "open": float(r["open"]), "high": float(r["high"]),
        "low": float(r["low"]), "close": float(r["close"]),
        "volume": float(r["tick_volume"]) if "tick_volume" in r.dtype.names else 0.0,
    } for r in raw]
    df = pd.DataFrame(rows).drop_duplicates("time").sort_values("time").set_index("time")
    return df


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=22)
    ap.add_argument("--asset", default="BTCUSD")
    args = ap.parse_args()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    a = MT5Adapter()
    print(f"Connecting {CREDS['server']} / {CREDS['login']} ...", flush=True)
    await a.connect(**CREDS)
    print("Connected. Downloading", args.asset, start.date(), "->", end.date(), flush=True)
    mt5 = a._mt5
    tfs = {
        "5m": mt5.TIMEFRAME_M5, "15m": mt5.TIMEFRAME_M15,
        "1h": mt5.TIMEFRAME_H1, "4h": mt5.TIMEFRAME_H4, "1d": mt5.TIMEFRAME_D1,
    }
    for name, const in tfs.items():
        df = await fetch(a, args.asset, const, start, end)
        if df.empty:
            print(f"  {name}: NO DATA", flush=True)
            continue
        fp = DATA_DIR / f"{args.asset.lower()}_{name}_recent.csv"
        df.to_csv(fp)
        print(f"  {name}: {len(df)} bars  {df.index[0]} -> {df.index[-1]}  -> {fp}", flush=True)
    try:
        a.disconnect()
    except Exception:
        pass
    print("DONE", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
