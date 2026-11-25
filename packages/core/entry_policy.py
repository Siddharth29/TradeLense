"""Causal session/event policy shared by live and replay. No network lookup."""
from __future__ import annotations

import json
import os
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .asset_registry import profile_for
from .operational_ledger import utc
from .risk.news_blackout import news_blackout_active
from .session import exness_closures

PRE_CLOSE_BUFFER_MINUTES = 5


def load_calendar():
    path = os.environ.get("TRADING_EVENT_CALENDAR")
    if not path:
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None  # Never replace the static policy with a broken feed.


def tradable_minutes(asset, now):
    """Time to the next maintenance/weekend close, zero inside a closure."""
    now = utc(now)
    closures = exness_closures(asset, now)
    if closures is not None:
        if any(start <= now < end for start, end, _ in closures):
            return 0.
        for day in range(6):
            upcoming = exness_closures(asset, now + timedelta(days=day)) or ()
            for start, _, _ in upcoming:
                if start >= now:
                    return max(0., (start - now).total_seconds() / 60 - PRE_CLOSE_BUFFER_MINUTES)
        return None

    profile = profile_for(asset)
    if not profile:
        return None
    windows = [w for w in (profile.maintenance_window, profile.daily_break) if w]
    if profile.has_weekend_gap and (now.weekday() == 5 or
        (now.weekday() == 6 and (now.hour, now.minute) < (22, 5))):
        return 0.
    limits = []
    for offset in (-1, 0, 1):
        day = (now + timedelta(days=offset)).replace(hour=0, minute=0, second=0, microsecond=0)
        for h, m, eh, em in windows:
            start = day + timedelta(hours=h, minutes=m)
            end = day + timedelta(hours=eh, minutes=em)
            if end <= start:
                end += timedelta(days=1)
            if start <= now < end:
                return 0.
            if start >= now:
                limits.append(max(0., (start-now).total_seconds()/60 - PRE_CLOSE_BUFFER_MINUTES))
    if profile.has_weekend_gap and now.weekday() == 4 and (now.hour, now.minute) >= (20, 55):
        return 0.
    return min(limits) if limits else None


def evaluate_entry_policy(asset, now, *, horizon_minutes=0., position="new", calendar=None, static_policy=None):
    now = utc(now)
    if position not in ("new", "exposed", "protected_runner"):
        raise ValueError("Unknown position risk state")
    remaining = tradable_minutes(asset, now)
    required = max(5., float(horizon_minutes or 0.))
    if not math.isfinite(float(horizon_minutes or 0.)) or float(horizon_minutes or 0.) < 0:
        raise ValueError("Invalid entry horizon")
    if position == "new" and remaining is not None and remaining <= required:
        return {"blocked": True, "reason": "insufficient_tradable_horizon", "remaining_minutes": remaining}
    data = calendar if calendar is not None else load_calendar()
    valid = False
    events = []
    try:
        # Explicit provenance + coverage, known by this checkpoint, required.
        valid = bool(data and data["verified"] is True and data["source"] and
                     utc(data["known_at"]) <= now and utc(data["valid_from"]) <= now < utc(data["valid_until"]))
        if valid:
            profile = profile_for(asset)
            currencies = {profile.base_currency, profile.quote_currency} if profile else set()
            for event in data["events"]:
                if utc(event["known_at"]) > now or event["impact"] != "high":
                    continue
                if not currencies.intersection(event["currencies"]):
                    continue
                at = utc(event["at"])
                before = float(event.get("before_minutes", 15))
                after = float(event.get("after_minutes", 15))
                if not all(math.isfinite(x) and x >= 0 for x in (before, after)):
                    raise ValueError("Invalid event buffers")
                if at-timedelta(minutes=max(before, required)) <= now < at+timedelta(minutes=after):
                    events.append(event["id"])
    except (ValueError, KeyError, TypeError, AttributeError):
        valid = False
        events = []
    if valid:
        blocked = bool(events) and position == "new"
        return {"blocked": blocked, "reason": "event_exposure" if events else "clear",
                "remaining_minutes": remaining,
                "events": events, "position": position, "calendar_source": data["source"],
                "management": "retain_protected_runner" if events and position == "protected_runner" else
                              "review_exposed_risk" if events else "normal"}
    blocked, reason = (static_policy or news_blackout_active)(asset, now)
    return {"blocked": blocked and position == "new", "reason": reason or "clear",
            "remaining_minutes": remaining,
            "calendar_source": "static_fallback", "position": position,
            "management": "review_exposed_risk" if blocked and position == "exposed" else "normal"}


def prompt_entry_policy(asset, as_of):
    if as_of is None:
        return ""
    return "\nEntry horizon at this snapshot (UTC): " + json.dumps({"as_of": str(as_of), **evaluate_entry_policy(asset, as_of)})
