"""XAUUSD vs XAGUSD correlation study — 3 years, LIVE Real8 data, read-only.

Answers, empirically:
  1. How correlated are gold and silver, at which horizons?
  2. How often do they move in OPPOSITE directions?
  3. In WHAT CASES do they diverge? (move size, session, volatility regime,
     trend vs chop, gold/silver-ratio state)

Motivation (2026-07-22): the app opened XAU sell + XAG buy simultaneously twice on
2026-07-21 and the user closed the XAG leg manually both times. Before adding a
correlation guard (CORRELATION_GROUPS) we validate the premise on real data rather
than assuming "metals move together".

NO orders are ever placed. Uses the LIVE creds in config/mt5_config.local.json
(a locally configured server); refuses non-live servers, using the same rule as
scripts/audit/pull_live_mt5.py.

Usage:
    python scripts/research/metals_correlation.py --years 3 --out data/research/metals_corr
"""
import argparse, json, os, sys
from datetime import datetime, timezone, timedelta

import MetaTrader5 as mt5
import numpy as np
import pandas as pd

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

# MT5 5m/15m history is capped ~100k bars on this account (see memory:
# smc-sweep-strategy-research). D1/H1 are safe for 3y; M15 is ~75k (ok); M5 is
# ~315k for 3y so we pull M5 for the most recent year only.
TIMEFRAMES = [
    ("D1", mt5.TIMEFRAME_D1, None),
    ("H4", mt5.TIMEFRAME_H4, None),
    ("H1", mt5.TIMEFRAME_H1, None),
    ("M15", mt5.TIMEFRAME_M15, None),
    ("M5", mt5.TIMEFRAME_M5, 1),   # last N years only
]


def load_live_creds():
    p = os.path.join(REPO, "config", "mt5_config.local.json")
    if not os.path.exists(p):
        sys.exit(f"LIVE creds not found at {p}. Refusing to use demo config.")
    cfg = json.load(open(p))["mt5"]
    if "real" not in cfg["server"].lower():
        sys.exit(f"server '{cfg['server']}' is not a real account — refusing")
    return cfg["server"], int(cfg["login"]), cfg["password"]


def connect():
    server, login, pw = load_live_creds()
    if not mt5.initialize(login=login, password=pw, server=server):
        if not mt5.initialize():
            sys.exit(f"mt5.initialize failed: {mt5.last_error()}")
        if not mt5.login(login=login, password=pw, server=server):
            sys.exit(f"mt5.login failed: {mt5.last_error()}")
    ai = mt5.account_info()
    if ai is None or ai.login != login or "real" not in str(ai.server).lower():
        sys.exit("ACCOUNT VERIFICATION FAILED")
    print(f"ACTIVE ACCOUNT: {ai.login} {ai.server} {ai.currency}")


def resolve(base):
    for cand in [base, base + "m", base + ".raw"]:
        if mt5.symbol_info(cand) is not None:
            mt5.symbol_select(cand, True)
            return cand
    sys.exit(f"symbol {base} not found")


def pull(sym, tf, frm, to):
    r = mt5.copy_rates_range(sym, tf, frm, to)
    if r is None or len(r) == 0:
        return None
    df = pd.DataFrame(r)
    df["dt"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df[["dt", "open", "high", "low", "close"]].set_index("dt")


def session_of(ts):
    h = ts.hour
    if h < 7:
        return "Asia"
    if h < 13:
        return "EU"
    if h < 21:
        return "NY"
    return "Late"


def analyse(name, au, ag, out_dir):
    """Join on timestamp, compute returns, report correlation + agreement."""
    j = pd.DataFrame({"au": au["close"], "ag": ag["close"]}).dropna()
    if len(j) < 50:
        print(f"  [{name}] too few overlapping bars ({len(j)}) — skipped")
        return None
    j["rau"] = j["au"].pct_change()
    j["rag"] = j["ag"].pct_change()
    j = j.dropna()
    # drop zero-move bars from the direction test (no information, and they would
    # otherwise be scored as "disagreement" by sign comparison)
    nz = j[(j["rau"] != 0) & (j["rag"] != 0)].copy()

    pear = j["rau"].corr(j["rag"])
    spear = j["rau"].corr(j["rag"], method="spearman")
    same = (np.sign(nz["rau"]) == np.sign(nz["rag"]))
    agree = same.mean()

    print(f"\n=== {name}  (n={len(j):,} bars, {j.index[0].date()} → {j.index[-1].date()}) ===")
    print(f"  Pearson r (returns)   : {pear:+.3f}")
    print(f"  Spearman r            : {spear:+.3f}")
    print(f"  SAME-direction bars   : {agree*100:.1f}%   (opposite {100-agree*100:.1f}%)")
    print(f"  zero-move bars dropped: {len(j)-len(nz):,}")

    # --- divergence conditioned on move size (is disagreement just noise?) ---
    nz["mag"] = nz["rau"].abs() * 100  # gold move size, %
    qs = nz["mag"].quantile([0.2, 0.4, 0.6, 0.8]).values
    def bucket(m):
        if m <= qs[0]: return "1 tiniest"
        if m <= qs[1]: return "2 small"
        if m <= qs[2]: return "3 medium"
        if m <= qs[3]: return "4 large"
        return "5 largest"
    nz["size_bucket"] = nz["mag"].apply(bucket)
    nz["same"] = same
    print("  --- agreement by GOLD move size (quintiles) ---")
    g = nz.groupby("size_bucket").agg(n=("same", "size"), agree=("same", "mean"),
                                      med_move_pct=("mag", "median"))
    for k, row in g.iterrows():
        print(f"    {k:10} n={int(row['n']):7,}  same-dir {row['agree']*100:5.1f}%   "
              f"median |gold move| {row['med_move_pct']:.3f}%")

    # --- by session (intraday timeframes only) ---
    if name in ("M5", "M15", "H1"):
        nz["session"] = [session_of(t) for t in nz.index]
        print("  --- agreement by SESSION ---")
        gs = nz.groupby("session").agg(n=("same", "size"), agree=("same", "mean"))
        for k, row in gs.iterrows():
            print(f"    {k:6} n={int(row['n']):7,}  same-dir {row['agree']*100:5.1f}%")

    # --- big-move test: when gold moves a lot, does silver follow? ---
    big = nz[nz["mag"] >= nz["mag"].quantile(0.95)]
    print(f"  --- top-5% gold moves: same-dir {big['same'].mean()*100:.1f}% (n={len(big):,}) ---")

    nz.to_csv(os.path.join(out_dir, f"joined_{name}.csv"))
    return dict(tf=name, n=len(j), pearson=pear, spearman=spear, agree=agree,
                agree_big=big["same"].mean())


def rolling_and_ratio(au_d1, ag_d1, out_dir):
    """Daily-horizon extras: rolling correlation stability + gold/silver ratio."""
    j = pd.DataFrame({"au": au_d1["close"], "ag": ag_d1["close"]}).dropna()
    j["rau"] = j["au"].pct_change()
    j["rag"] = j["ag"].pct_change()
    j = j.dropna()
    j["roll60"] = j["rau"].rolling(60).corr(j["rag"])
    j["ratio"] = j["au"] / j["ag"]

    print("\n=== Rolling 60-day correlation stability (daily returns) ===")
    r = j["roll60"].dropna()
    print(f"  mean {r.mean():+.3f} | min {r.min():+.3f} | max {r.max():+.3f} | "
          f"std {r.std():.3f}")
    print(f"  share of 60d windows with r < 0.50 : {(r < 0.50).mean()*100:.1f}%")
    print(f"  share of 60d windows with r < 0.30 : {(r < 0.30).mean()*100:.1f}%")
    print(f"  share of 60d windows NEGATIVE      : {(r < 0).mean()*100:.1f}%")
    worst = r.nsmallest(5)
    print("  weakest windows:")
    for d, v in worst.items():
        print(f"    {d.date()}  r={v:+.3f}")

    print("\n=== Gold/Silver ratio (3y) ===")
    print(f"  min {j['ratio'].min():.1f} | median {j['ratio'].median():.1f} | "
          f"max {j['ratio'].max():.1f} | last {j['ratio'].iloc[-1]:.1f}")

    # Does silver amplify? beta of silver on gold
    beta = np.polyfit(j["rau"], j["rag"], 1)[0]
    print(f"  beta (silver vs gold daily returns): {beta:.2f}x  "
          f"— silver vol {j['rag'].std()/j['rau'].std():.2f}x gold's")

    j.to_csv(os.path.join(out_dir, "daily_joined.csv"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=3)
    ap.add_argument("--out", default=os.path.join(REPO, "data", "research", "metals_corr"))
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    to = datetime.now(timezone.utc)
    frm = to - timedelta(days=365 * a.years + 5)

    connect()
    s_au, s_ag = resolve("XAUUSD"), resolve("XAGUSD")
    print(f"symbols: {s_au} / {s_ag}   window: {frm.date()} → {to.date()}\n")

    summary = []
    d1 = {}
    for name, tf, yrs in TIMEFRAMES:
        f = to - timedelta(days=365 * (yrs or a.years) + 5)
        au, ag = pull(s_au, tf, f, to), pull(s_ag, tf, f, to)
        if au is None or ag is None:
            print(f"  [{name}] no data")
            continue
        print(f"[{name}] gold {len(au):,} bars, silver {len(ag):,} bars")
        r = analyse(name, au, ag, a.out)
        if r:
            summary.append(r)
        if name == "D1":
            d1 = (au, ag)

    if d1:
        rolling_and_ratio(d1[0], d1[1], a.out)

    print("\n" + "=" * 68)
    print("SUMMARY — XAUUSD vs XAGUSD")
    print(f"{'TF':5}{'bars':>10}{'Pearson':>10}{'same-dir':>11}{'big-move same':>15}")
    for s in summary:
        print(f"{s['tf']:5}{s['n']:>10,}{s['pearson']:>+10.3f}"
              f"{s['agree']*100:>10.1f}%{s['agree_big']*100:>14.1f}%")
    pd.DataFrame(summary).to_csv(os.path.join(a.out, "summary.csv"), index=False)
    print(f"\nwrote -> {a.out}")
    mt5.shutdown()


if __name__ == "__main__":
    main()
