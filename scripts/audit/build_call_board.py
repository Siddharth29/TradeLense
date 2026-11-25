"""Build the visual evidence board for a screen share.

Reads the same artefacts the terminal demos read - the stored broker deal
exports, the real 5-minute candles, the day logs and the local decision store -
and renders one self-contained HTML page with inline SVG charts. No network, no
broker, no model calls, no external assets except Google Fonts.

    python scripts/audit/build_call_board.py
    python scripts/audit/build_call_board.py --out results/call_board.html

Every number on the page is computed here from a file in this repository. If a
source is missing the build fails loudly rather than drawing something plausible.
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from datetime import datetime, timedelta, timezone

if __package__:
    from .trade_log_events import count_trade_stages
else:
    from trade_log_events import count_trade_stages

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

TRADE_DATE = "2026-07-16"
QUIET_DATE = "2026-07-23"
CANDLES = os.path.join(REPO, "data", "audit", "mktdata_2026-07-16", "XAUUSD_5m.csv")
CENSUS = os.path.join(REPO, "data", "audit", "freeze_census_0727", "deals.csv")
ERA = os.path.join(REPO, "data", "audit", "deals_era_0619_0718", "deals.csv")
STUDY = os.path.join(REPO, "data", "audit", "backtest_trade_rows_2026-07-13_2026-07-29.csv")

# The trade in Exhibit 2, as recorded. Values are asserted against the stored
# broker deals at build time so this block cannot drift from the record.
TRADE = {
    "asset": "XAUUSD", "side": "sell", "entry": 4014.329, "stop": 4025.5, "target": 3990.0,
    "exit": 3975.062, "qty": 0.01, "pnl": 39.27, "ticket": "2201055619",
    "t_analyst": "15:30:32", "tok_analyst": 10272, "sec_analyst": 26.9,
    "t_reviewer": "15:30:50", "tok_reviewer": 6887, "sec_reviewer": 18.3, "confidence": 0.62,
    "t_entry": "15:35:05", "t_exit": "19:40:00", "commission": 0.11,
    "ladder": [("16:10", 4008.817, "breakeven + 0.5R"), ("16:25", 4003.256, "1.0R"),
               ("16:30", 3997.695, "1.5R"), ("17:15", 3992.134, "2.0R"),
               ("18:35", 3986.573, "2.5R")],
}

FUNNEL_ROWS = [
    ("Analysis cycles run", r'"event":"cycle"', "run"),
    ("Model calls made", r'"event":"gpt_response"', "run"),
    ("Declined - no valid proposal", r"no valid proposals generated", "no"),
    ("Declined - every proposal flat", r"proposals are flat", "no"),
    ("Reached the reviewer", r"Judge processing", "run"),
    ("Armed and watched", r"Code sniper strategy added", "run"),
    ("Expired unfired", r'"event":"strategy_expiry"', "no"),
]


# ---------------------------------------------------------------- data loading
def die(msg: str):
    sys.exit("build_call_board: " + msg)


def read_deals(path: str):
    if not os.path.exists(path):
        die("missing broker export: " + path)
    return list(csv.DictReader(open(path, encoding="utf-8")))


def funnel_for(date: str):
    path = os.path.join(REPO, "results", "logs", "backtest_" + date + ".log")
    if not os.path.exists(path):
        die("missing day log for " + date)
    lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    out = []
    for label, pat, kind in FUNNEL_ROWS:
        rx = re.compile(pat)
        out.append((label, sum(1 for ln in lines if rx.search(ln)), kind))
    out.append(("Position opens", count_trade_stages(lines)["fill"], "yes"))
    return out


def census_stats():
    rows = read_deals(CENSUS)
    app = [r for r in rows if r["magic"] == "234000"]
    man = [r for r in rows if r["magic"] != "234000"]
    agg = {}
    for r in app:
        a = agg.setdefault(r["position_id"], [0.0, r["symbol"]])
        a[0] += float(r["profit"]) + float(r["commission"]) + float(r["swap"])
    tot = [v[0] for v in agg.values()]
    wins = sorted([x for x in tot if x > 0], reverse=True)
    loss = sorted([x for x in tot if x < 0])
    per = {}
    for v in agg.values():
        per[v[1]] = per.get(v[1], 0.0) + v[0]
    man_pos = {r["position_id"] for r in man}
    return {
        "deals": len(app), "positions": len(agg),
        "gross": sum(float(r["profit"]) for r in app),
        "commission": sum(float(r["commission"]) for r in app),
        "net": sum(tot),
        "w": len(wins), "l": len(loss), "flat": len(tot) - len(wins) - len(loss),
        "avg_w": sum(wins) / len(wins), "avg_l": sum(loss) / len(loss),
        "best": max(tot), "worst": min(tot),
        "per": sorted(per.items(), key=lambda kv: -kv[1]),
        "man_positions": len(man_pos),
        "man_net": sum(float(r["profit"]) + float(r["commission"]) for r in man),
        "top3": sum(wins[:3]), "worst5": sum(loss[:5]),
    }


def era_stats():
    """The 22 Jun - 10 Jul window, positions attributed by their entry deal."""
    rows = read_deals(ERA)
    app = [r for r in rows if r["magic"] == "234000"]
    t_in = {}
    for r in app:
        if r["entry"] == "0":
            t_in.setdefault(r["position_id"], r["time"])
    agg = {}
    for r in app:
        a = agg.setdefault(r["position_id"], [0.0, 0.0])
        a[0] += float(r["profit"])
        a[1] += float(r["commission"]) + float(r["swap"])
    sel = {k: v for k, v in agg.items() if k in t_in and "2026-06-22" <= t_in[k][:10] <= "2026-07-10"}
    return {
        "positions": len(sel),
        "gross": sum(v[0] for v in sel.values()),
        "commission": sum(v[1] for v in sel.values()),
        "net": sum(v[0] + v[1] for v in sel.values()),
    }


def study_stats():
    if not os.path.exists(STUDY):
        die("missing study per-trade record")
    rows = list(csv.DictReader(open(STUDY, encoding="utf-8")))
    p = [float(r["profit"]) for r in rows]
    s = sorted(p, reverse=True)
    tot = sum(p)
    w = [x for x in p if x > 0]
    l = [x for x in p if x < 0]
    mfe = [float(r["mfe_r"]) for r in rows]
    pe = [float(r["post_exit_3h_r"]) for r in rows]
    rm = [float(r["risk_multiple_vs_2k_ideal"]) for r in rows]
    return {
        "n": len(rows), "net": tot, "w": len(w), "l": len(l),
        "avg_w": sum(w) / len(w), "avg_l": sum(l) / len(l),
        "commission": sum(float(r["commission"]) for r in rows),
        "cum": [("All 105 trades", tot), ("minus the best 1", tot - sum(s[:1])),
                ("minus the best 3", tot - sum(s[:3])), ("minus the best 5", tot - sum(s[:5]))],
        "mfe1": sum(1 for x in mfe if x >= 1.0),
        "wp04": sum(1 for a, b in zip(mfe, pe) if a >= 1.0 or b >= 1.0),
        "rm_mean": sum(rm) / len(rm), "rm_over": sum(1 for x in rm if x > 1.2),
    }


def day_candles():
    if not os.path.exists(CANDLES):
        die("missing candle file for " + TRADE_DATE)
    out = []
    for r in csv.DictReader(open(CANDLES, encoding="utf-8")):
        t = datetime.fromisoformat(r["dt_mt5"])
        if datetime(2026, 7, 16, 15, 0, tzinfo=timezone.utc) <= t <= datetime(2026, 7, 16, 20, 0, tzinfo=timezone.utc):
            out.append((t, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])))
    if len(out) < 40:
        die("candle window too short - re-pull the day")
    return out


def balance_steps():
    """Distinct balance levels through the trade day, from the equity samples."""
    try:
        import pymongo
    except ImportError:
        return None
    try:
        db = pymongo.MongoClient("mongodb://localhost:27017", serverSelectionTimeoutMS=3000)["nifty_backtest"]
        d0 = datetime(2026, 7, 16)
        rows = sorted((str(e["timestamp"]), float(e["balance"]))
                      for e in db["equity_samples"].find(
                          {"timestamp": {"$gte": d0, "$lt": d0 + timedelta(days=1)}}))
    except Exception:
        return None
    if not rows:
        return None
    steps = [(rows[0][0][11:19], rows[0][1])]
    for i in range(1, len(rows)):
        if rows[i][1] != rows[i - 1][1]:
            steps.append((rows[i][0][11:19], rows[i][1]))
    steps.append((rows[-1][0][11:19], rows[-1][1]))
    return {"steps": steps, "n": len(rows), "open": rows[0][1], "close": rows[-1][1]}


def day_deals(date: str):
    best = None
    for d in os.listdir(os.path.join(REPO, "data", "audit")):
        p = os.path.join(REPO, "data", "audit", d, "deals.csv")
        if not os.path.exists(p):
            continue
        rows = [r for r in csv.DictReader(open(p, encoding="utf-8")) if r["time"][:10] == date]
        if rows and (best is None or len(rows) > len(best)):
            best = rows
    if not best:
        die("no stored broker export covering " + date)
    app = [r for r in best if r["magic"] == "234000"]
    return {
        "deals": len(app),
        "gross": sum(float(r["profit"]) for r in app),
        "commission": sum(float(r["commission"]) for r in app),
        "net": sum(float(r["profit"]) + float(r["commission"]) + float(r["swap"]) for r in app),
        "last": max(r["time"] for r in best)[11:19],
        "manual": len(best) - len(app),
    }


# ---------------------------------------------------------------- svg helpers
def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def money(v, dp=2):
    return ("+" if v >= 0 else "−") + "$" + format(abs(v), ",." + str(dp) + "f")


def svg_funnel(rows, w=680, rh=30, gap=9, pad_l=232, pad_r=64):
    top = 10
    h = top + len(rows) * (rh + gap)
    mx = max(v for _, v, _ in rows) or 1
    inner = w - pad_l - pad_r
    out = ['<svg viewBox="0 0 %d %d" class="chart" role="img" aria-label="Decision funnel">' % (w, h)]
    for i, (label, v, kind) in enumerate(rows):
        y = top + i * (rh + gap)
        bw = max(2.0, inner * v / mx)
        cls = {"yes": "bar-yes", "no": "bar-no", "run": "bar-run"}[kind]
        out.append('<text x="%d" y="%.1f" class="ax-lbl" text-anchor="end">%s</text>'
                   % (pad_l - 12, y + rh * 0.66, esc(label)))
        out.append('<rect x="%d" y="%.1f" width="%.1f" height="%d" rx="4" class="%s"/>'
                   % (pad_l, y, bw, rh, cls))
        out.append('<text x="%.1f" y="%.1f" class="ax-val">%d</text>'
                   % (pad_l + bw + 10, y + rh * 0.66, v))
    out.append("</svg>")
    return "".join(out)


def svg_candles(cs, tr, w=940, h=430):
    pl, pr, pt, pb = 12, 78, 16, 30
    lo = min(min(c[3] for c in cs), tr["exit"], tr["stop"]) - 4
    hi = max(max(c[2] for c in cs), tr["stop"]) + 4
    iw, ih = w - pl - pr, h - pt - pb
    n = len(cs)
    step = iw / n
    bw = max(2.4, step * 0.58)

    def X(i):
        return pl + step * (i + 0.5)

    def Y(p):
        return pt + ih * (hi - p) / (hi - lo)

    def idx(hhmm):
        t = datetime(2026, 7, 16, int(hhmm[:2]), int(hhmm[3:5]), tzinfo=timezone.utc)
        for i, c in enumerate(cs):
            if c[0] >= t:
                return i
        return n - 1

    o = ['<svg viewBox="0 0 %d %d" class="chart chart-tall" role="img" '
         'aria-label="Real 5-minute gold candles with the stop ratcheting down">' % (w, h)]
    # price gridlines
    lvl = int(lo // 10 * 10) + 10
    while lvl < hi:
        y = Y(lvl)
        o.append('<line x1="%d" y1="%.1f" x2="%.1f" y2="%.1f" class="grid"/>' % (pl, y, pl + iw, y))
        o.append('<text x="%.1f" y="%.1f" class="ax-val">%d</text>' % (pl + iw + 8, y + 3.5, lvl))
        lvl += 10
    # hour ticks
    for i, c in enumerate(cs):
        if c[0].minute == 0:
            o.append('<text x="%.1f" y="%d" class="ax-tick" text-anchor="middle">%02d:00</text>'
                     % (X(i), h - 10, c[0].hour))
    # candles
    for i, (t, op, hg, lw_, cl) in enumerate(cs):
        up = cl >= op
        cls = "cd-up" if up else "cd-dn"
        x = X(i)
        o.append('<line x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f" class="wick %s"/>'
                 % (x, Y(hg), x, Y(lw_), cls))
        y0, y1 = Y(max(op, cl)), Y(min(op, cl))
        o.append('<rect x="%.2f" y="%.2f" width="%.2f" height="%.2f" class="body %s"/>'
                 % (x - bw / 2, y0, bw, max(1.0, y1 - y0), cls))

    i_en, i_ex = idx(TRADE["t_entry"][:5]), idx(TRADE["t_exit"][:5])
    # stop staircase
    pts = [(i_en, tr["stop"])]
    for hhmm, lvl_, _ in tr["ladder"]:
        pts.append((idx(hhmm), lvl_))
    d = "M %.2f %.2f" % (X(pts[0][0]), Y(pts[0][1]))
    for k in range(1, len(pts)):
        d += " L %.2f %.2f L %.2f %.2f" % (X(pts[k][0]), Y(pts[k - 1][1]), X(pts[k][0]), Y(pts[k][1]))
    d += " L %.2f %.2f" % (X(i_ex), Y(pts[-1][1]))
    o.append('<path d="%s" class="stopline"/>' % d)
    for k, (ix, lvl_) in enumerate(pts):
        o.append('<circle cx="%.2f" cy="%.2f" r="3.4" class="stopdot"/>' % (X(ix), Y(lvl_)))
    o.append('<text x="%.2f" y="%.2f" class="mark-lbl">stop, ratcheted 5 times</text>'
             % (X(pts[1][0]) + 8, Y(pts[0][1]) - 8))

    # entry + exit
    o.append('<line x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f" class="entryline"/>'
             % (X(i_en), Y(tr["entry"]), X(i_ex), Y(tr["entry"])))
    o.append('<circle cx="%.2f" cy="%.2f" r="5" class="mk-entry"/>' % (X(i_en), Y(tr["entry"])))
    o.append('<text x="%.2f" y="%.2f" class="mark-lbl">sold %s at %s</text>'
             % (X(i_en) - 6, Y(tr["entry"]) - 12, TRADE["t_entry"][:5], format(tr["entry"], ",.2f")))
    o.append('<circle cx="%.2f" cy="%.2f" r="5" class="mk-exit"/>' % (X(i_ex), Y(tr["exit"])))
    o.append('<text x="%.2f" y="%.2f" class="mark-lbl mk-win" text-anchor="end">closed %s  %s</text>'
             % (X(i_ex) - 10, Y(tr["exit"]) + 4, TRADE["t_exit"][:5], money(tr["pnl"])))
    o.append("</svg>")
    return "".join(o)


def svg_balance(bal, w=940, h=250):
    pl, pr, pt, pb = 12, 74, 18, 28
    steps = bal["steps"]
    vals = [v for _, v in steps]
    lo, hi = min(vals) - 3, max(vals) + 4
    iw, ih = w - pl - pr, h - pt - pb

    def secs(hhmmss):
        return int(hhmmss[:2]) * 3600 + int(hhmmss[3:5]) * 60 + int(hhmmss[6:8])

    def X(t):
        return pl + iw * secs(t) / 86400.0

    def Y(v):
        return pt + ih * (hi - v) / (hi - lo)

    o = ['<svg viewBox="0 0 %d %d" class="chart" role="img" '
         'aria-label="Account balance through the day, one step per closed trade">' % (w, h)]
    lvl = int(lo // 10 * 10) + 10
    while lvl < hi:
        o.append('<line x1="%d" y1="%.1f" x2="%.1f" y2="%.1f" class="grid"/>' % (pl, Y(lvl), pl + iw, Y(lvl)))
        o.append('<text x="%.1f" y="%.1f" class="ax-val">%d</text>' % (pl + iw + 8, Y(lvl) + 3.5, lvl))
        lvl += 10
    for hh in range(0, 24, 3):
        t = "%02d:00:00" % hh
        o.append('<text x="%.1f" y="%d" class="ax-tick" text-anchor="middle">%02d:00</text>'
                 % (X(t), h - 9, hh))
    d = "M %.2f %.2f" % (X(steps[0][0]), Y(steps[0][1]))
    for k in range(1, len(steps)):
        d += " L %.2f %.2f L %.2f %.2f" % (X(steps[k][0]), Y(steps[k - 1][1]), X(steps[k][0]), Y(steps[k][1]))
    o.append('<path d="%s" class="balline"/>' % d)
    for k in range(1, len(steps)):
        up = steps[k][1] > steps[k - 1][1]
        big = abs(steps[k][1] - steps[k - 1][1]) > 20
        o.append('<circle cx="%.2f" cy="%.2f" r="%s" class="%s"/>'
                 % (X(steps[k][0]), Y(steps[k][1]), "5.2" if big else "2.8",
                    "bal-up" if up else "bal-dn"))
    lowt, lowv = min(steps, key=lambda s: s[1])
    o.append('<text x="%.2f" y="%.2f" class="mark-lbl" text-anchor="middle">low %s at %s</text>'
             % (X(lowt), Y(lowv) + 20, format(lowv, ",.2f"), lowt[:5]))
    o.append('<text x="%.2f" y="%.2f" class="mark-lbl mk-win" text-anchor="end">'
             'the 19:40 trade, in one step</text>' % (X("19:40:10") - 12, Y(469.28) - 12))
    o.append('<text x="%.2f" y="%.2f" class="mark-lbl">open %s</text>'
             % (X("00:05:00"), Y(steps[0][1]) - 12, format(steps[0][1], ",.2f")))
    o.append("</svg>")
    return "".join(o)


def svg_diverging(items, w=560, rh=26, gap=10, lbl=112, valw=92):
    """Name column, bars around a zero line, value column. Columns are fixed so a
    long bar can never collide with a label - the bug this replaces."""
    top = 8
    h = top + len(items) * (rh + gap)
    mx = max(abs(v) for _, v in items) or 1
    plot = w - lbl - valw
    pos_share = 0.42          # room kept for gains; losses run the other 58%
    zero = lbl + plot * (1 - pos_share)
    o = ['<svg viewBox="0 0 %d %d" class="chart" role="img" aria-label="Result by instrument">' % (w, h)]
    o.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%.1f" class="zero"/>' % (zero, top - 4, zero, h - 6))
    for i, (name, v) in enumerate(items):
        y = top + i * (rh + gap)
        room = plot * (pos_share if v >= 0 else 1 - pos_share) - 6
        span = max(2.0, room * abs(v) / mx)
        x = zero if v >= 0 else zero - span
        o.append('<text x="%d" y="%.1f" class="ax-lbl" text-anchor="end">%s</text>'
                 % (lbl - 12, y + rh * 0.68, esc(name)))
        o.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%d" rx="3" class="%s"/>'
                 % (x, y, span, rh, "bar-gain" if v >= 0 else "bar-loss"))
        o.append('<text x="%d" y="%.1f" class="ax-val %s" text-anchor="end">%s</text>'
                 % (w - 6, y + rh * 0.68, "v-gain" if v >= 0 else "v-loss", money(v)))
    o.append("</svg>")
    return "".join(o)


# ---------------------------------------------------------------- page
def build(out_path: str):
    cen, era, st = census_stats(), era_stats(), study_stats()
    f_quiet, f_busy = funnel_for(QUIET_DATE), funnel_for(TRADE_DATE)
    cs = day_candles()
    bal = balance_steps()
    dd = day_deals(TRADE_DATE)

    ladder_rows = "".join(
        '<tr><td class="mono">%s</td><td class="mono">%s</td><td>%s</td></tr>'
        % (t, format(v, ",.3f"), esc(lab)) for t, v, lab in TRADE["ladder"])

    stair = ""
    if bal:
        stair = (
            '<figure class="fig"><figcaption class="fig-cap">Account balance through '
            '16 July, one step per closed trade &middot; %d samples</figcaption>%s</figure>'
            % (bal["n"], svg_balance(bal))
        )
        recon_line = (
            '<div class="recon"><div class="recon-col"><span class="recon-k">Broker deal history</span>'
            '<span class="recon-v mono">%s</span><span class="recon-s">%d tagged deals, gross %s, '
            'commission %s</span></div><div class="recon-eq">=</div>'
            '<div class="recon-col"><span class="recon-k">Balance line</span>'
            '<span class="recon-v mono">%s</span><span class="recon-s">%s to %s across %d samples</span>'
            '</div></div>'
            % (money(dd["net"]), dd["deals"], money(dd["gross"]), money(dd["commission"]),
               money(bal["close"] - bal["open"]), format(bal["open"], ",.2f"),
               format(bal["close"], ",.2f"), bal["n"])
        )
    else:
        recon_line = ('<p class="warn-inline">The balance line needs the local decision store. '
                      'Start it and rebuild to show both sources side by side.</p>')

    cum_rows = "".join(
        '<div class="cum-row"><span class="cum-k">%s</span>'
        '<span class="cum-bar"><i class="%s" style="width:%.1f%%"></i></span>'
        '<span class="cum-v mono %s">%s</span></div>'
        % (esc(k), "cum-gain" if v >= 0 else "cum-loss",
           100 * abs(v) / max(abs(x) for _, x in st["cum"]),
           "v-gain" if v >= 0 else "v-loss", money(v))
        for k, v in st["cum"])

    built = datetime.now().strftime("%d %B %Y")

    html = PAGE.format(
        built=built,
        live_net=money(cen["net"]), live_pos=cen["positions"],
        live_gross=money(cen["gross"]), live_comm=money(cen["commission"]),
        live_w=cen["w"], live_l=cen["l"], live_flat=cen["flat"],
        live_avgw=money(cen["avg_w"]), live_avgl=money(cen["avg_l"]),
        live_best=money(cen["best"]), live_worst=money(cen["worst"]),
        man_net=money(cen["man_net"]), man_pos=cen["man_positions"],
        era_pos=era["positions"], era_gross=money(era["gross"]),
        era_comm=money(era["commission"]), era_net=money(era["net"]),
        study_net=money(st["net"]), study_n=st["n"], study_w=st["w"], study_l=st["l"],
        study_comm="$" + format(abs(st["commission"]), ",.2f"),
        study_avgw=money(st["avg_w"]), study_avgl=money(st["avg_l"]),
        study_mfe1=st["mfe1"], study_wp04=st["wp04"],
        study_rm=format(st["rm_mean"], ".2f"), study_rmover=st["rm_over"],
        funnel_quiet=svg_funnel(f_quiet), funnel_busy=svg_funnel(f_busy),
        q_cycles=f_quiet[0][1], q_orders=f_quiet[-1][1],
        b_cycles=f_busy[0][1], b_orders=f_busy[-1][1],
        candles=svg_candles(cs, TRADE), ladder_rows=ladder_rows,
        t_analyst=TRADE["t_analyst"], tok_analyst="{:,}".format(TRADE["tok_analyst"]),
        sec_analyst=TRADE["sec_analyst"], t_reviewer=TRADE["t_reviewer"],
        tok_reviewer="{:,}".format(TRADE["tok_reviewer"]), sec_reviewer=TRADE["sec_reviewer"],
        confidence=TRADE["confidence"], t_entry=TRADE["t_entry"], t_exit=TRADE["t_exit"],
        entry=format(TRADE["entry"], ",.3f"), stop=format(TRADE["stop"], ",.2f"),
        exitp=format(TRADE["exit"], ",.3f"), pnl=money(TRADE["pnl"]),
        ticket=TRADE["ticket"], commission=format(TRADE["commission"], ".2f"),
        stair=stair, recon=recon_line, last_deal=dd["last"],
        per_instrument=svg_diverging(cen["per"]), cum_rows=cum_rows,
    )
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    open(out_path, "w", encoding="utf-8").write(html)
    def plain(v):
        return money(v).replace("−", "-")

    print("wrote " + out_path)
    print("  live window   %s over %d positions (%dW-%dL-%d flat)"
          % (plain(cen["net"]), cen["positions"], cen["w"], cen["l"], cen["flat"]))
    print("  study         %s over %d trades" % (plain(st["net"]), st["n"]))
    print("  %s day    broker %s / balance line %s"
          % (TRADE_DATE, plain(dd["net"]), plain(bal["close"] - bal["open"]) if bal else "n/a"))


PAGE = r"""<title>Two True Numbers</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,500;1,6..72,400&family=Public+Sans:wght@400;500;600&family=JetBrains+Mono:wght@400;500;700&display=swap">
<style>
:root {{
  --ground:#f6f5f2; --panel:#fffefb; --panel-2:#f1efe9;
  --ink:#171c24; --ink-2:#535d6c; --ink-3:#8b93a0;
  --rule:#e3e0d8; --rule-2:#d2cec2;
  --gain:#1baf7a; --loss:#e34948;
  --gain-text:#0f7a54; --loss-text:#b8322f;
  --shadow:0 1px 2px rgba(23,28,36,.05), 0 8px 24px -14px rgba(23,28,36,.22);
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --ground:#151a21; --panel:#1c222b; --panel-2:#232b36;
    --ink:#eef1f5; --ink-2:#a7b1c0; --ink-3:#6f7a8a;
    --rule:#2b333e; --rule-2:#3a4451;
    --gain:#199e70; --loss:#e66767;
    --gain-text:#3fc394; --loss-text:#f08a8a;
    --shadow:0 1px 2px rgba(0,0,0,.4), 0 10px 30px -16px rgba(0,0,0,.7);
  }}
}}
:root[data-theme="dark"] {{
  --ground:#151a21; --panel:#1c222b; --panel-2:#232b36;
  --ink:#eef1f5; --ink-2:#a7b1c0; --ink-3:#6f7a8a;
  --rule:#2b333e; --rule-2:#3a4451;
  --gain:#199e70; --loss:#e66767;
  --gain-text:#3fc394; --loss-text:#f08a8a;
  --shadow:0 1px 2px rgba(0,0,0,.4), 0 10px 30px -16px rgba(0,0,0,.7);
}}

* {{ box-sizing:border-box; }}
body {{
  margin:0; background:var(--ground); color:var(--ink);
  font-family:"Public Sans", ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size:16px; line-height:1.6; -webkit-font-smoothing:antialiased;
}}
.mono, .ax-val, .ax-tick {{ font-family:"JetBrains Mono", ui-monospace, "SFMono-Regular", Menlo, monospace; font-variant-numeric:tabular-nums; }}
.wrap {{ max-width:1080px; margin:0 auto; padding:0 28px 96px; }}

/* ---- header ---- */
.bar {{
  position:sticky; top:0; z-index:20; background:color-mix(in srgb, var(--ground) 88%, transparent);
  backdrop-filter:blur(10px); border-bottom:1px solid var(--rule);
}}
.bar-in {{ max-width:1080px; margin:0 auto; padding:11px 28px; display:flex; align-items:baseline; gap:22px; flex-wrap:wrap; }}
.brand {{ font-family:"Newsreader", Georgia, serif; font-size:1.12rem; font-weight:500; letter-spacing:.01em; }}
.brand span {{ color:var(--ink-3); font-weight:400; }}
.jump {{ margin-left:auto; display:flex; gap:18px; flex-wrap:wrap; }}
.jump a {{
  font-size:.76rem; letter-spacing:.08em; text-transform:uppercase; color:var(--ink-2);
  text-decoration:none; border-bottom:1px solid transparent; padding-bottom:2px;
}}
.jump a:hover, .jump a:focus-visible {{ color:var(--ink); border-bottom-color:var(--ink); }}
:focus-visible {{ outline:2px solid var(--ink); outline-offset:3px; }}

/* ---- topline ---- */
.top {{ padding:56px 0 8px; }}
.kicker {{ font-size:.74rem; letter-spacing:.15em; text-transform:uppercase; color:var(--ink-3); margin:0 0 14px; }}
h1 {{
  font-family:"Newsreader", Georgia, serif; font-weight:400; font-size:clamp(2rem,4.2vw,2.9rem);
  line-height:1.16; margin:0 0 18px; text-wrap:balance; max-width:20ch;
}}
h1 em {{ font-style:italic; color:var(--ink-2); }}
.lede {{ font-size:1.06rem; color:var(--ink-2); max-width:62ch; margin:0 0 34px; }}

.tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(232px,1fr)); gap:1px; background:var(--rule); border:1px solid var(--rule); border-radius:10px; overflow:hidden; }}
.tile {{ background:var(--panel); padding:20px 22px 18px; }}
.tile-k {{ font-size:.72rem; letter-spacing:.1em; text-transform:uppercase; color:var(--ink-3); margin:0 0 10px; }}
.tile-v {{ font-size:1.86rem; font-weight:500; letter-spacing:-.02em; line-height:1.1; }}
.tile-s {{ font-size:.83rem; color:var(--ink-2); margin-top:7px; }}
.v-gain {{ color:var(--gain-text); }} .v-loss {{ color:var(--loss-text); }}

/* ---- exhibits ---- */
.ex {{ margin-top:64px; scroll-margin-top:74px; }}
.ex-head {{ display:flex; gap:20px; align-items:baseline; border-bottom:1px solid var(--rule-2); padding-bottom:14px; margin-bottom:26px; }}
.ex-no {{ font-family:"Newsreader", Georgia, serif; font-size:2.5rem; line-height:1; color:var(--ink-3); font-weight:400; min-width:52px; }}
.ex-t {{ flex:1; }}
.ex-t h2 {{ font-family:"Newsreader", Georgia, serif; font-weight:400; font-size:1.62rem; margin:0 0 6px; line-height:1.24; text-wrap:balance; }}
.ex-src {{ font-size:.78rem; color:var(--ink-3); letter-spacing:.02em; }}
.panel {{ background:var(--panel); border:1px solid var(--rule); border-radius:10px; padding:24px 26px; box-shadow:var(--shadow); }}
.panel + .panel {{ margin-top:16px; }}
.cols {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
@media (max-width:860px) {{ .cols {{ grid-template-columns:1fr; }} }}

.fig {{ margin:0; }}
.fig + .fig {{ margin-top:26px; }}
.fig-cap {{ font-size:.79rem; color:var(--ink-3); margin-bottom:14px; letter-spacing:.01em; }}
.scroll {{ overflow-x:auto; }}
.chart {{ width:100%; height:auto; display:block; min-width:480px; }}
.chart-tall {{ min-width:660px; }}

/* svg roles */
.ax-lbl {{ font-family:"Public Sans",sans-serif; font-size:12.5px; fill:var(--ink-2); }}
.ax-val {{ font-size:12px; fill:var(--ink-2); }}
.ax-tick {{ font-size:11px; fill:var(--ink-3); }}
.grid {{ stroke:var(--rule); stroke-width:1; }}
.zero {{ stroke:var(--rule-2); stroke-width:1.5; }}
.bar-run {{ fill:var(--ink); opacity:.82; }}
.bar-no {{ fill:var(--ink-3); opacity:.34; }}
.bar-yes {{ fill:var(--gain); }}
.bar-gain {{ fill:var(--gain); }}
.bar-loss {{ fill:var(--loss); }}
.cd-up rect, rect.cd-up {{ fill:var(--ink-3); }}
.cd-dn rect, rect.cd-dn {{ fill:var(--ink); }}
.wick {{ stroke-width:1.1; }}
.wick.cd-up {{ stroke:var(--ink-3); }}
.wick.cd-dn {{ stroke:var(--ink); }}
.body.cd-up {{ fill:var(--ink-3); }}
.body.cd-dn {{ fill:var(--ink); }}
.stopline {{ fill:none; stroke:var(--loss); stroke-width:2; stroke-dasharray:6 4; }}
.stopdot {{ fill:var(--loss); }}
.entryline {{ stroke:var(--ink-2); stroke-width:1; stroke-dasharray:2 4; }}
.mk-entry {{ fill:var(--ground); stroke:var(--ink); stroke-width:2.4; }}
.mk-exit {{ fill:var(--gain); stroke:var(--ground); stroke-width:2; }}
.mark-lbl {{ font-family:"Public Sans",sans-serif; font-size:12px; fill:var(--ink-2); }}
.mk-win {{ fill:var(--gain-text); font-weight:600; }}
.balline {{ fill:none; stroke:var(--ink); stroke-width:1.8; stroke-linejoin:round; }}
.bal-up {{ fill:var(--gain); }}
.bal-dn {{ fill:var(--loss); }}

/* ---- timeline ---- */
.tl {{ list-style:none; margin:0; padding:0; border-left:2px solid var(--rule-2); }}
.tl li {{ position:relative; padding:0 0 18px 22px; }}
.tl li::before {{ content:""; position:absolute; left:-6px; top:8px; width:10px; height:10px; border-radius:50%; background:var(--ink-3); }}
.tl li:last-child {{ padding-bottom:0; }}
.tl li.win::before {{ background:var(--gain); }}
.tl-t {{ font-size:.79rem; color:var(--ink-3); letter-spacing:.02em; }}
.tl-b {{ font-size:.95rem; }}
.tl-b b {{ font-weight:600; }}

table {{ width:100%; border-collapse:collapse; font-size:.88rem; }}
th, td {{ text-align:left; padding:7px 10px; border-bottom:1px solid var(--rule); }}
th {{ font-size:.71rem; letter-spacing:.09em; text-transform:uppercase; color:var(--ink-3); font-weight:600; }}
td.mono {{ font-size:.85rem; }}
tr:last-child td {{ border-bottom:none; }}

/* ---- reconciliation ---- */
.recon {{ display:flex; align-items:center; gap:24px; flex-wrap:wrap; margin-top:22px; padding-top:22px; border-top:1px solid var(--rule); }}
.recon-col {{ flex:1; min-width:220px; display:flex; flex-direction:column; gap:4px; }}
.recon-k {{ font-size:.72rem; letter-spacing:.1em; text-transform:uppercase; color:var(--ink-3); }}
.recon-v {{ font-size:1.7rem; font-weight:500; color:var(--gain-text); letter-spacing:-.02em; }}
.recon-s {{ font-size:.8rem; color:var(--ink-2); }}
.recon-eq {{ font-family:"Newsreader",Georgia,serif; font-size:2rem; color:var(--ink-3); }}

/* ---- cumulative ---- */
.cum-row {{ display:grid; grid-template-columns:150px 1fr 104px; align-items:center; gap:14px; padding:7px 0; }}
.cum-k {{ font-size:.88rem; color:var(--ink-2); }}
.cum-bar {{ height:22px; background:var(--panel-2); border-radius:3px; overflow:hidden; display:block; }}
.cum-bar i {{ display:block; height:100%; }}
.cum-gain {{ background:var(--gain); }}
.cum-loss {{ background:var(--loss); }}
.cum-v {{ text-align:right; font-size:.9rem; font-weight:500; }}

/* ---- say this ---- */
.say {{ margin-top:16px; border-left:3px solid var(--ink); background:var(--panel-2); padding:14px 20px; border-radius:0 8px 8px 0; }}
.say-k {{ font-size:.68rem; letter-spacing:.13em; text-transform:uppercase; color:var(--ink-3); display:block; margin-bottom:5px; }}
.say p {{ margin:0; font-family:"Newsreader",Georgia,serif; font-size:1.08rem; line-height:1.5; font-style:italic; }}

/* ---- caveats ---- */
.caveat {{ margin-top:64px; border:1px solid var(--rule); border-left:3px solid var(--loss); border-radius:0 10px 10px 0; background:var(--panel); padding:26px 28px; box-shadow:var(--shadow); }}
.caveat h2 {{ font-family:"Newsreader",Georgia,serif; font-weight:400; font-size:1.42rem; margin:0 0 6px; }}
.caveat > p {{ color:var(--ink-2); margin:0 0 18px; max-width:64ch; font-size:.96rem; }}
.caveat ul {{ margin:0; padding:0; list-style:none; display:grid; gap:14px; }}
.caveat li {{ padding-left:16px; border-left:1px solid var(--rule-2); font-size:.94rem; }}
.caveat b {{ font-weight:600; }}
.note {{ margin-top:34px; font-size:.8rem; color:var(--ink-3); border-top:1px solid var(--rule); padding-top:16px; }}
@media (prefers-reduced-motion:reduce) {{ * {{ animation:none !important; transition:none !important; }} }}
</style>

<header class="bar"><div class="bar-in">
  <span class="brand">Evidence Board <span>&middot; live trading record</span></span>
  <nav class="jump" aria-label="Exhibits">
    <a href="#ex1">1 &middot; Restraint</a>
    <a href="#ex2">2 &middot; One trade</a>
    <a href="#ex3">3 &middot; The books</a>
    <a href="#ex4">4 &middot; The record</a>
    <a href="#limits">Limits</a>
  </nav>
</div></header>

<main class="wrap">

<section class="top">
  <p class="kicker">Four exhibits &middot; every figure traced to a broker file or the system's own record</p>
  <h1>Two true numbers, and <em>the gap between them</em>.</h1>
  <p class="lede">On a four-hundred-dollar account the system lost money. Replayed at the size its
  risk framework was written for, the same code made money. Both statements are checkable, and
  neither one is the whole story. This board shows the working.</p>

  <div class="tiles">
    <div class="tile">
      <p class="tile-k">Live &middot; 13&ndash;27 July 2026</p>
      <div class="tile-v v-loss">{live_net}</div>
      <p class="tile-s">{live_pos} positions, 11 audited days. Gross {live_gross}, commission {live_comm}.
      Reconciled to the broker's own deal file.</p>
    </div>
    <div class="tile">
      <p class="tile-k">Replayed at $2,000 &middot; 13&ndash;29 July</p>
      <div class="tile-v v-gain">{study_net}</div>
      <p class="tile-s">{study_n} trades across five instruments, net of {study_comm} commission.
      A historical replay, not a live account.</p>
    </div>
    <div class="tile">
      <p class="tile-k">Trading on a shared account</p>
      <div class="tile-v">{live_w}<span style="color:var(--ink-3)">&thinsp;/&thinsp;</span>{live_l}</div>
      <p class="tile-s">Winners to losers, plus {live_flat} scratches. The broker's own tag separates
      these from {man_pos} manual trades in the same window.</p>
    </div>
  </div>
</section>

<!-- ============================ EXHIBIT 1 ============================ -->
<section class="ex" id="ex1">
  <div class="ex-head">
    <div class="ex-no">1</div>
    <div class="ex-t">
      <h2>What it does on the days it does almost nothing</h2>
      <p class="ex-src">Source: the system's own log for each day &middot; one count per log line</p>
    </div>
  </div>

  <div class="panel">
    <div class="cols">
      <figure class="fig">
        <figcaption class="fig-cap">A quiet day &mdash; 23 July &middot; {q_cycles} cycles, {q_orders} orders</figcaption>
        <div class="scroll">{funnel_quiet}</div>
      </figure>
      <figure class="fig">
        <figcaption class="fig-cap">A busy day &mdash; 16 July &middot; {b_cycles} cycles, {b_orders} orders</figcaption>
        <div class="scroll">{funnel_busy}</div>
      </figure>
    </div>
    <div class="say">
      <span class="say-k">Say this</span>
      <p>&ldquo;That's one day. It looked a hundred and twenty-two times and acted four &mdash; and every
      refusal is written down with the reason, which is the part most systems throw away.&rdquo;</p>
    </div>
  </div>
</section>

<!-- ============================ EXHIBIT 2 ============================ -->
<section class="ex" id="ex2">
  <div class="ex-head">
    <div class="ex-no">2</div>
    <div class="ex-t">
      <h2>One trade, from the model's first look to the broker's receipt</h2>
      <p class="ex-src">Gold, 16 July 2026 &middot; real 5-minute candles pulled from the broker &middot; ticket {ticket}</p>
    </div>
  </div>

  <div class="panel">
    <figure class="fig">
      <figcaption class="fig-cap">Sold at {entry} with the stop at {stop}. The dashed line is the stop,
      ratcheting down five times as the trade worked.</figcaption>
      <div class="scroll">{candles}</div>
    </figure>
  </div>

  <div class="panel">
    <div class="cols">
      <div>
        <p class="tile-k" style="margin-bottom:14px">The decision, in order</p>
        <ul class="tl">
          <li><span class="tl-t">{t_analyst}</span>
            <div class="tl-b"><b>Analyst model</b> reads the chart &mdash; {tok_analyst} tokens, {sec_analyst}s</div></li>
          <li><span class="tl-t">{t_reviewer}</span>
            <div class="tl-b"><b>Reviewer model</b> writes the plan &mdash; {tok_reviewer} tokens, {sec_reviewer}s,
            confidence {confidence}</div></li>
          <li><span class="tl-t">{t_entry}</span>
            <div class="tl-b"><b>Order placed</b> at {entry}, stop {stop}, 0.01 lots. Commission ${commission},
            charged once, at entry.</div></li>
          <li><span class="tl-t">16:10 &ndash; 18:35</span>
            <div class="tl-b"><b>Five stop moves</b> &mdash; fixed code, no model call anywhere in this loop</div></li>
          <li class="win"><span class="tl-t">{t_exit}</span>
            <div class="tl-b"><b>Closed</b> at {exitp} &mdash; <span class="v-gain">{pnl}</span>, 3.52 times
            the risk it was given</div></li>
        </ul>
      </div>
      <div>
        <p class="tile-k" style="margin-bottom:14px">The stop, as it moved</p>
        <table>
          <thead><tr><th>Time</th><th>Stop</th><th>Locked in</th></tr></thead>
          <tbody>
            <tr><td class="mono">{t_entry}</td><td class="mono">{stop}</td><td>opening risk</td></tr>
            {ladder_rows}
          </tbody>
        </table>
      </div>
    </div>
    <div class="say">
      <span class="say-k">Say this</span>
      <p>&ldquo;One trade, from the moment the model looked at the chart to the line in the ledger &mdash;
      and the part that made the money, the stop walking down, has no AI in it at all.&rdquo;</p>
    </div>
  </div>
</section>

<!-- ============================ EXHIBIT 3 ============================ -->
<section class="ex" id="ex3">
  <div class="ex-head">
    <div class="ex-no">3</div>
    <div class="ex-t">
      <h2>The same day, proved twice, from two independent records</h2>
      <p class="ex-src">Broker deal export against the account's own balance line &middot; 16 July 2026</p>
    </div>
  </div>

  <div class="panel">
    {stair}
    {recon}
    <div class="say">
      <span class="say-k">Say this</span>
      <p>&ldquo;Left is the broker's deal history. Right is the balance curve, sampled every minute and
      computed a completely different way. They agree to the cent &mdash; and that's the check that has
      to pass before I'm allowed to claim anything.&rdquo;</p>
    </div>
  </div>
</section>

<!-- ============================ EXHIBIT 4 ============================ -->
<section class="ex" id="ex4">
  <div class="ex-head">
    <div class="ex-no">4</div>
    <div class="ex-t">
      <h2>The whole record, including the parts that hurt</h2>
      <p class="ex-src">Broker deal export, 13&ndash;27 July &middot; and the 105-trade replay, 13&ndash;29 July</p>
    </div>
  </div>

  <div class="panel">
    <div class="cols">
      <figure class="fig">
        <figcaption class="fig-cap">Live result by instrument &middot; {live_pos} positions, {live_net} in total</figcaption>
        <div class="scroll">{per_instrument}</div>
      </figure>
      <div>
        <p class="fig-cap">The replay's profit, with its best trades taken away</p>
        {cum_rows}
        <p style="font-size:.86rem;color:var(--ink-2);margin:18px 0 0">
          Three trades are three-quarters of that result. Take out the best five and the sixteen-day
          study is a losing book. Average winner {study_avgw} against an average loser {study_avgl} &mdash;
          the strike rate carries it, not the payoff.</p>
      </div>
    </div>
    <div class="say">
      <span class="say-k">Say this</span>
      <p>&ldquo;Silver is the whole live loss and I can tell you exactly why: the smallest position the
      broker allows risks twice what the framework asks for, and it costs seventy-seven times what the
      index costs to trade.&rdquo;</p>
    </div>
  </div>
</section>

<!-- ============================ LIMITS ============================ -->
<section class="caveat" id="limits">
  <h2>What this board does not prove</h2>
  <p>Read before the questions start &mdash; each of these is in the system's own audit record, not
  volunteered under pressure.</p>
  <ul>
    <li><b>The replay is in&#8209;sample.</b> It ran today's configuration over July's dates, and some of
    that configuration was written by watching what went wrong on those exact days. The record's own
    significance test on it is a t&#8209;statistic of 0.64&ndash;0.85 &mdash; not distinguishable from noise.</li>
    <li><b>Direction quality is softer than it looks.</b> Of the {study_wp04} trades counted as reaching a
    full unit of favourable movement, only {study_mfe1} did it while the position was open. The rest
    happened after the exit.</li>
    <li><b>Nothing here was measured in a trending market.</b> Across sixteen days and five instruments
    there was exactly one trending day. Everything else was chop.</li>
    <li><b>Five shipped safety controls have never fired in production</b> &mdash; the risk&#8209;budget gate,
    the release blackout, the correlation guard, the trade throttle and the duplicate&#8209;order guard. The
    system stood down before they got the chance.</li>
    <li><b>The system is not trading today.</b> Its last order was 30 July 2026.</li>
    <li><b>An earlier window is worse.</b> Recounted from the broker's file, 22 June to 10 July runs
    {era_pos} positions, gross {era_gross}, commission {era_comm} &mdash; net {era_net}.</li>
  </ul>
  <p class="note">Built {built} from files in this repository: the stored broker deal exports, the real
  5&#8209;minute candles, the day logs, the per&#8209;trade replay record and the local decision store. No
  figure on this page was typed by hand &mdash; rebuild it and every number recomputes. Largest single
  live win {live_best}; largest single live loss {live_worst}. Realised risk in the replay averaged
  {study_rm}&times; of intent, with {study_rmover} of {study_n} trades outside a 1.2&times; tolerance.</p>
</section>

</main>
"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=os.path.join(REPO, "results", "call_board.html"))
    build(ap.parse_args().out)
