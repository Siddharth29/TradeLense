"""Classify structured trade records, including historical partial exits."""

import json
from collections import Counter

PARTIAL_REASONS = {"TP1_PARTIAL", "TP2_PARTIAL"}


def trade_stage(event, payload):
    if event not in ("trade_open", "trade_exit"):
        return None
    reason = str(payload.get("exit_reason") or "").strip().upper()
    if reason in PARTIAL_REASONS:
        return "partial"
    status = str(payload.get("trade_status") or "").strip().lower()
    if event == "trade_exit" or status == "closed":
        return "exit"
    if status in ("", "open"):
        return "fill"
    return None


def count_trade_stages(lines):
    counts = Counter()
    for line in lines:
        if '"trade_open"' not in line and '"trade_exit"' not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and isinstance(record.get("payload"), dict):
            stage = trade_stage(record.get("event"), record["payload"])
            if stage is not None:
                counts[stage] += 1
    return counts
