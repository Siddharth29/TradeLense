"""Scheduled-release entry blackout (LP-04).

Why this exists
---------------
LP-04 was promoted to CONFIRMED at the 2026-07-18 weekly review on three
day-occurrences — 07-07 (−$41.17), 07-10 (−$31.98) and 07-14 (−$38.20, of
which $22.66 was slippage straight *through* an XAUUSD stop on the 12:30 CPI
bar). Roughly $111 on a ~$450 account, from one repeating mechanism: the judge
has no economic calendar, so a release bar looks like any other bar to it.

Stops are not the answer — the 07-14 exhibit proves a release can gap through
one — so the defence is simply not opening a position into the print. This
module is the static-window version of that: no feed, no new dependency, and it
covers the slots where all three measured incidents occurred. A real calendar
feed keyed on actual high-impact events is the eventual replacement; the
interface here (``news_blackout_active``) is what it would swap in behind.

Deliberate scope: NEW entries only. An already-open position running into a
release is a management question the trail engine and pre-close windows already
own, and force-flattening on a clock would itself throw away with-trend runners.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Tuple

from ..config import (
    NEWS_BLACKOUT_ENABLED,
    NEWS_BLACKOUT_WINDOWS_UTC,
    NEWS_BLACKOUT_EXEMPT_ASSETS,
)


def _asset_key(asset: Optional[str]) -> str:
    """Normalise an asset name the same way every other per-asset gate does.

    Upper-cased, and a trailing broker demo ``m`` suffix dropped on names longer
    than four characters (``XAUUSDm`` → ``XAUUSD``). Kept byte-identical in
    behaviour to ``config.sl_anchor_mode_for`` so two gates can never disagree
    about what an asset is called.
    """
    key = (asset or "").strip().upper()
    if key.endswith("M") and len(key) > 4:
        key = key[:-1]
    return key


def news_blackout_active(
    asset: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Tuple[bool, str]:
    """Return ``(blocked, reason)`` for opening a new position right now.

    ``now`` defaults to the current UTC time; a naive datetime is read as UTC.
    Fails OPEN — an unparseable window is skipped rather than halting trading,
    matching every other guard on this path.
    """
    if not NEWS_BLACKOUT_ENABLED:
        return False, ""

    if _asset_key(asset) in {_asset_key(a) for a in NEWS_BLACKOUT_EXEMPT_ASSETS}:
        return False, ""

    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    minutes = now.hour * 60 + now.minute

    for window in NEWS_BLACKOUT_WINDOWS_UTC:
        try:
            start_h, start_m, end_h, end_m = window
            start = int(start_h) * 60 + int(start_m)
            end = int(end_h) * 60 + int(end_m)
        except (TypeError, ValueError):
            continue  # malformed window — ignore it rather than block trading
        # Inclusive of start, exclusive of end. Windows never wrap midnight;
        # a wrapped one would be a config error, so treat it as inert.
        if start <= minutes < end:
            return True, (
                f"scheduled-release window {start_h:02d}:{start_m:02d}–"
                f"{end_h:02d}:{end_m:02d} UTC (now {now:%H:%M} UTC)"
            )

    return False, ""


def minutes_until_blackout(
    asset: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Optional[int]:
    """Minutes until the next blackout window opens today, or ``None``.

    Used to give the judge prompt an explicit heads-up ("high-impact release
    window in N minutes") so the *reasoning* layer stops being calendar-blind
    too, rather than relying on the code gate alone as a backstop.
    """
    if not NEWS_BLACKOUT_ENABLED:
        return None
    if _asset_key(asset) in {_asset_key(a) for a in NEWS_BLACKOUT_EXEMPT_ASSETS}:
        return None

    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    minutes = now.hour * 60 + now.minute
    upcoming = []
    for window in NEWS_BLACKOUT_WINDOWS_UTC:
        try:
            start = int(window[0]) * 60 + int(window[1])
        except (TypeError, ValueError, IndexError):
            continue
        if start > minutes:
            upcoming.append(start - minutes)
    return min(upcoming) if upcoming else None
