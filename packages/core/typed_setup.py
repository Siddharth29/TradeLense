"""Versioned setup compiler. The model chooses intent; fixed code owns triggers.

The same closed-bar state machine is replayed from the immutable arming snapshot
in live and historical execution. No generated Python, hidden counters or future
extrema participate in typed execution.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .operational_ledger import utc

VERSION = "setup-v1"
FAMILIES = ("continuation_retest", "range_reclaim", "sweep_reclaim")


class SetupSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)
    family: Literal["continuation_retest", "range_reclaim", "sweep_reclaim"]
    side: Literal["long", "short"]
    level_id: str
    target_level_id: str
    entry: float = Field(gt=0)
    stop: float = Field(gt=0)
    tp1: float = Field(gt=0)
    tp2: float = Field(gt=0)
    tolerance: float = Field(gt=0)
    expected_horizon_minutes: int = Field(ge=5, le=240)
    max_position_age_minutes: int = Field(ge=5, le=480)

    @model_validator(mode="after")
    def geometry(self):
        from decimal import Decimal
        sign = 1 if self.side == "long" else -1
        entry, stop, tp1, tp2, tolerance = [Decimal(str(x)) for x in (self.entry, self.stop, self.tp1, self.tp2, self.tolerance)]
        risk = sign * (entry-stop)
        if risk <= 0 or sign*(tp1-entry) < Decimal("1.2")*risk or sign*(tp2-tp1) < 0:
            raise ValueError("Invalid structural geometry / TP1 RR")
        if tolerance > risk * Decimal("0.25"):
            raise ValueError("Entry tolerance exceeds 0.25R")
        if self.expected_horizon_minutes > self.max_position_age_minutes:
            raise ValueError("Expected horizon exceeds maximum holding period")
        return self


def snapshot_id(c5, c15, as_of):
    payload = json.dumps({"c5": c5, "c15": c15, "as_of": str(as_of)}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def level_catalog(c15, as_of):
    """Only completed, confirmed two-sided M15 pivots, with causal availability."""
    now = utc(as_of)
    bars = [c for c in c15 if utc(c["time"])+timedelta(minutes=15) <= now]
    levels = []
    for i in range(2, len(bars)-2):
        window = bars[i-2:i+3]
        for field, kind, reducer in (("high", "resistance", max), ("low", "support", min)):
            price = float(bars[i][field])
            if not math.isfinite(price) or price <= 0:
                continue
            if price == reducer(float(c[field]) for c in window):
                levels.append({"id": f"m15:{bars[i]['time']}:{field}", "price": price,
                               "kind": kind, "available_at": str(utc(bars[i+2]["time"])+timedelta(minutes=15))})
    return levels[-24:]


def compile_setup(proposal, c5, c15, cfg):
    from .schemas_trade import CodeSniperSignal
    spec = SetupSpec.model_validate(proposal.setup)
    now = utc(cfg["as_of"])
    levels = {x["id"]: x for x in level_catalog(c15, now)}
    level, target = levels[spec.level_id], levels[spec.target_level_id]
    if abs(spec.entry-level["price"]) > spec.tolerance or abs(spec.tp1-target["price"]) > 1e-9:
        raise ValueError("Setup is not anchored to its observed structural levels")
    if (spec.side == "long") != (proposal.signal == "buy"):
        raise ValueError("Typed side differs from proposal")
    if any(abs(a-b) > 1e-9 for a, b in ((spec.entry, proposal.entry), (spec.stop, proposal.stop), (spec.tp1, proposal.target))):
        raise ValueError("Typed geometry differs from proposal")
    expected_kind = "resistance" if spec.side == "long" else "support"
    if spec.family != "continuation_retest":
        expected_kind = "support" if spec.side == "long" else "resistance"
    if level["kind"] != expected_kind or target["kind"] != ("resistance" if spec.side == "long" else "support"):
        raise ValueError("Structural level roles do not match setup family")
    metadata = {"rationale": proposal.rationale, "typed_setup": spec.model_dump(), "compiler_version": VERSION,
                "armed_at": now.isoformat(), "expires_at": (now+timedelta(minutes=proposal.ttl_minutes)).isoformat(),
                "level_price": level["price"], "snapshot_id": snapshot_id(c5, c15, now),
                "model_expert": cfg.get("_resolved_model_expert") or cfg.get("model_expert") or cfg.get("model"),
                "prompt_version": cfg.get("_typed_prompt_hash") or cfg.get("prompt_hash"), "adjudication": "deterministic_single_candidate"}
    # Compatibility carrier only: all execution/dry-run paths dispatch metadata.
    return CodeSniperSignal(code="def run(candles_5, now):\n    return {'direction': 'hold'}",
                            metadata=metadata, ttl_minutes=proposal.ttl_minutes, confidence=proposal.confidence)


def conditional_compile(proposals, c5, c15, cfg):
    cfg.pop("typed_compilation", None)
    mode = cfg.get("typed_setup_mode", os.environ.get("TYPED_SETUP_MODE", "shadow"))
    if mode not in ("off", "shadow", "prefer"):
        raise ValueError("TYPED_SETUP_MODE must be off, shadow or prefer")
    if mode == "off" or len(proposals) != 1 or not getattr(proposals[0], "setup", None):
        return None
    if proposals[0].confidence < .75 or not cfg.get("as_of"):
        return None
    try:
        compiled = compile_setup(proposals[0], c5, c15, cfg)
    except (ValueError, KeyError, TypeError):
        return None  # Ambiguity/unsupported intent still requires model adjudication.
    cfg["typed_compilation"] = compiled.model_dump()  # Paired shadow artifact, same snapshot.
    return compiled if mode == "prefer" else None


def evaluate_setup(metadata, history, current):
    if metadata.get("compiler_version") != VERSION:
        raise ValueError("Unsupported setup compiler version")
    spec = SetupSpec.model_validate(metadata["typed_setup"])
    armed, expiry = utc(metadata["armed_at"]), utc(metadata["expires_at"])
    now = utc(current["time"])+timedelta(minutes=5)
    hold = {"direction": "hold", "typed_status": "waiting"}
    if now >= expiry:
        return {**hold, "typed_status": "expired"}
    sign = 1 if spec.side == "long" else -1
    level = float(metadata["level_price"])
    phase_at = None
    # Dedupe closed bars, ordered by actual availability (bar open + 5m).
    bars = {utc(c["time"]): c for c in [*history, current]}
    active = [stamp for stamp in sorted(bars) if armed < stamp+timedelta(minutes=5) <= now]
    if active and (active[0] > armed or any(b-a != timedelta(minutes=5) for a, b in zip(active, active[1:]))):
        return {**hold, "typed_status": "history_unavailable"}
    for stamp, c in sorted(bars.items()):
        at = stamp+timedelta(minutes=5)
        if not armed < at <= now:
            continue
        low, high, close = (float(c[k]) for k in ("low", "high", "close"))
        if not all(math.isfinite(x) and x > 0 for x in (low, high, close)) or not low <= close <= high:
            raise ValueError("Invalid typed execution candle")
        if sign*(close-spec.stop) <= 0:
            return {**hold, "typed_status": "invalidated"}
        adverse = low if sign == 1 else high
        if phase_at is None:
            first = sign*(close-level) > spec.tolerance if spec.family == "continuation_retest" else sign*(adverse-level) < -spec.tolerance
            if first:
                phase_at = at
            continue  # A break/sweep and its later retest are different closed bars.
        reclaimed = sign*(close-level) >= 0 and sign*(adverse-level) <= spec.tolerance
        if reclaimed and abs(close-spec.entry) <= spec.tolerance:
            # Never backfill an old trigger at a newer quote after downtime.
            if at != now:
                return {**hold, "typed_status": "missed_trigger", "first_trigger_at": at.isoformat()}
            return {"direction": spec.side, "entry": spec.entry, "stop_loss": spec.stop,
                    "take_profit_1": spec.tp1, "take_profit_2": spec.tp2,
                    "typed_status": "triggered", "first_trigger_at": at.isoformat(),
                    "rationale": f"{VERSION}:{spec.family}"}
    return hold


def evaluate_observed_setup(metadata, history, current, state):
    """Retain the observed arming window across a rolling 40-bar input feed.

    At most TTL/5 bars are retained. A cold restart with an unrecoverable gap
    fails closed in evaluate_setup instead of inventing the missing sequence.
    """
    armed = utc(metadata["armed_at"])
    now = utc(current["time"])
    merged = {utc(c["time"]): c for c in [*state.get("typed_observed_bars", []), *history, current]
              if armed <= utc(c["time"]) <= now}
    observed = [c for _, c in sorted(merged.items())]
    state["typed_observed_bars"] = observed
    return evaluate_setup(metadata, observed, current)


def typed_prompt(c15, cfg):
    if not cfg.get("as_of") or cfg.get("typed_setup_mode", os.environ.get("TYPED_SETUP_MODE", "shadow")) == "off":
        return ""
    return ("\nOptional typed setup: use setup only when one of continuation_retest, range_reclaim, "
            "sweep_reclaim exactly describes your intent. Otherwise omit setup and retain normal adjudication. "
            "All families require a break/sweep on one completed bar and a later retest/reclaim bar; "
            "same-bar sweep/reclaim is unsupported. Use ONLY these level IDs, with tp1 exactly at the "
            "target level and entry within tolerance of the entry level. Copy proposal entry/stop/target "
            "to setup entry/stop/tp1. Tolerance <= 0.25R. Select a realistic expected horizon and hard "
            "maximum holding period. Range family exits fully at TP1; other families retain the runner ladder. "
            "Do not invent IDs or change a thesis just to qualify. Schema: "
            + json.dumps(SetupSpec.model_json_schema()) + "\nObserved levels: "
            + json.dumps(level_catalog(c15, cfg["as_of"])))
