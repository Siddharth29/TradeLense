"""Read-only audit of preserved prompts and session-tape research inputs."""
import csv
import json
import re
from collections import Counter
from pathlib import Path

import pandas as pd

from packages.core.market_structure import compute_session_tape
from packages.core.session_stats import session_bounds

ROOT = Path(__file__).resolve().parents[2]
counts = Counter()
prompt_rows = []
latencies = []
for path in sorted((ROOT / "results/logs").glob("backtest_2026-07-*.log")):
    if not "2026-07-13" <= path.stem.removeprefix("backtest_") <= "2026-07-29":
        continue
    for line in path.open(encoding="utf-8"):
        if "EXPERT PROMPT >>>" not in line and '"event":"gpt_response"' not in line:
            continue
        try:
            j = json.loads(line)
        except ValueError:
            continue
        if '"event":"gpt_response"' in line:
            p = j.get("payload") or {}
            mode = p.get("gpt_mode") or (j.get("ctx") or {}).get("gpt_mode")
            latencies.append({"mode": mode, **{k: v for k, v in p.items() if "ms" in k or "latenc" in k or "duration" in k}})
        msg = j.get("msg", "")
        if not msg.startswith("EXPERT PROMPT >>>"):
            continue
        match = re.search(r"5MIN \((\d+)\): (\[.*?\])\n", msg)
        asset_match = re.search(r"asset=(\w+)", msg)
        if not match or not asset_match:
            continue
        cs = json.loads(match[2])
        last = pd.Timestamp(cs[-1][0], tz="UTC") if pd.Timestamp(cs[-1][0]).tzinfo is None else pd.Timestamp(cs[-1][0])
        first = pd.Timestamp(cs[0][0], tz="UTC") if pd.Timestamp(cs[0][0]).tzinfo is None else pd.Timestamp(cs[0][0])
        _, start, _ = session_bounds(last.to_pydatetime())
        truncated = first > start
        counts[(asset_match[1], int(match[1]), truncated)] += 1
        prompt_rows.append({"asset": asset_match[1], "timestamp": j["ts"], "count": int(match[1]), "session_start_missing": truncated})

stored = pd.read_csv(ROOT / "data/research/label_tape_replay_2026-07-25.csv")
comparisons = []
cache = {}
for r in stored.to_dict("records"):
    asset = r["symbol"]
    if asset not in cache:
        file = ROOT / "data/processed" / f"{asset.lower()}_5m_2years.csv"
        if not file.exists():
            continue
        candles = pd.read_csv(file)
        candles["time"] = pd.to_datetime(candles["time"], utc=True)
        cache[asset] = candles
    candles = cache[asset]
    at = pd.Timestamp(r["entry_time"])
    old = candles[candles.time <= at].tail(150)
    closed = candles[candles.time + pd.Timedelta(minutes=5) <= at].tail(150)
    if len(old) < 100 or at > candles.time.max():
        continue
    old_tape = compute_session_tape(old.to_dict("records"), r["label"], asset)
    new_tape = compute_session_tape(closed.to_dict("records"), r["label"], asset)
    if not old_tape or not new_tape:
        continue
    matches_archive = abs(old_tape["move_pct"] - r["tape_move_pct"]) < 0.0002
    comparisons.append({
        "asset": asset, "entry_time": r["entry_time"], "net": r["net"],
        "matches_archive": matches_archive,
        "old_conflict": old_tape["conflict"], "closed_conflict": new_tape["conflict"],
        "old_er": old_tape["er_session"], "closed_er": new_tape["er_session"],
        "old_move": old_tape["move_pct"], "closed_move": new_tape["move_pct"],
    })

output = ROOT / "docs/reviews/2026-09-09-decision-evidence.json"
output.parent.mkdir(parents=True, exist_ok=True)
summary = {
    "expert_prompts": len(prompt_rows),
    "prompt_count_distribution": [{"asset": k[0], "bars": k[1], "session_start_missing": k[2], "n": v} for k, v in sorted(counts.items())],
    "session_start_missing_total": sum(r["session_start_missing"] for r in prompt_rows),
    "research_candle_reconstruction": comparisons,
    "gpt_latency_payload_samples": latencies[:5],
    "gpt_latency_summary_seconds": [
        {"mode": mode, "n": len(group), "p50": round(group.latency_ms.median() / 1000, 2),
         "p90": round(group.latency_ms.quantile(0.90) / 1000, 2),
         "max": round(group.latency_ms.max() / 1000, 2)}
        for mode, group in pd.DataFrame(latencies).groupby("mode")
    ],
}
output.write_text(json.dumps(summary, indent=2), encoding="utf-8")
print(json.dumps({"expert_prompts": len(prompt_rows), "session_start_missing": summary["session_start_missing_total"], "bar_counts": dict(Counter(r["count"] for r in prompt_rows)), "reconstruction_rows": len(comparisons), "archive_matches": sum(r["matches_archive"] for r in comparisons), "changed_conflicts": sum(r["matches_archive"] and r["old_conflict"] != r["closed_conflict"] for r in comparisons), "latency": summary["gpt_latency_summary_seconds"]}, indent=2))
