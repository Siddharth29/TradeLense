"""Fetch 5m/15m/1h candles covering the FULL trade window (2026-05-27 -> now)
for all traded assets from the already-logged-in MT5 terminal. Read-only.
Saves data/processed/{asset}_{tf}_full.csv
"""
import MetaTrader5 as mt5
import csv, os, sys
from datetime import datetime

assert mt5.initialize(), mt5.last_error()

CANDIDATES = {
    "BTCUSD": ["BTCUSDm", "BTCUSD"],
    "XAUUSD": ["XAUUSDm", "XAUUSD"],
    "GBPUSD": ["GBPUSDm", "GBPUSD"],
    "USOIL":  ["USOILm", "USOIL", "XTIUSDm", "WTIm"],
}
TFS = {"5m": (mt5.TIMEFRAME_M5, 5500), "15m": (mt5.TIMEFRAME_M15, 2000), "1h": (mt5.TIMEFRAME_H1, 600)}
os.makedirs("data/processed", exist_ok=True)

for clean, syms in CANDIDATES.items():
    sym = None
    for s in syms:
        info = mt5.symbol_info(s)
        if info is not None:
            sym = s
            break
    if sym is None:
        print(f"NO SYMBOL for {clean}", file=sys.stderr)
        continue
    for tflab, (tf, n) in TFS.items():
        rates = mt5.copy_rates_from_pos(sym, tf, 0, n)
        if rates is None or len(rates) == 0:
            print(f"FAIL {sym} {tflab} {mt5.last_error()}", file=sys.stderr)
            continue
        path = f"data/processed/{clean.lower()}_{tflab}_full.csv"
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["time", "open", "high", "low", "close", "tick_volume"])
            for r in rates:
                w.writerow([datetime.utcfromtimestamp(r["time"]).isoformat(),
                            r["open"], r["high"], r["low"], r["close"], r["tick_volume"]])
        t0 = datetime.utcfromtimestamp(rates[0]["time"])
        t1 = datetime.utcfromtimestamp(rates[-1]["time"])
        print(f"saved {path:48s} {len(rates):5d} bars  {t0} -> {t1}")
mt5.shutdown()
