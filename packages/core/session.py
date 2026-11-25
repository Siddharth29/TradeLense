"""Trading-session helpers (MT5-only).

Session gating is driven by the single asset registry: each symbol's
``AssetProfile.session`` decides the schedule —

- ``crypto_24_7``   : always open (BTCUSD, ETHUSD).  Daily broker maintenance is
                      handled separately by the candle fetch / monitor, not here.
- ``weekday_24_5``  : forex / metals / energy — open Mon–Fri, closed from
                      Friday 22:00 UTC through the weekend.
- ``index``         : index CFDs (e.g. USTEC) — weekday session plus a daily
                      break window and an earlier Friday close.

Unknown symbols default to ``weekday_24_5`` (never the old NSE 09:15–15:30 IST
gate, which silently blocked EURUSD and every un-listed MT5 symbol).

``IST`` / ``ensure_ist`` remain only as legacy timestamp utilities used by the
backtest CSV loaders and the supervisor scheduler; they no longer drive any
trading-session decision.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .asset_registry import profile_for

# Legacy timezone used by backtest CSV loaders (io_loader, resample_ohlc) and
# the supervisor scheduler.  Not used for session gating any more.
IST = ZoneInfo("Asia/Kolkata")
_NEW_YORK = ZoneInfo("America/New_York")

# Exness trading hours effective 2026-11-01. Times are New York local
# wall time so each side of a weekend uses the offset on its own date.
# https://get.exness.help/hc/en-us/articles/31342732737180
_FX = ((16, 59), (17, 5), None, None)
_EXNESS_HOURS = {
    "EURUSD": _FX,
    "GBPUSD": _FX,
    "XAUUSD": ((16, 58), (18, 5), (16, 58), (18, 2)),
    "XAGUSD": ((16, 58), (18, 5), (16, 58), (18, 1)),
    "USTEC": ((16, 55), (18, 5), (16, 59), (18, 1)),
    "BTCUSD": (None, None, None, None),
}
_EXNESS_HOURS_EFFECTIVE = date(2026, 11, 1)


def ensure_ist(dt: datetime) -> datetime:
    """Return *dt* converted to ``Asia/Kolkata`` timezone.

    Naive datetimes are interpreted as IST if the hour is 6 or later, otherwise
    they are assumed to be in UTC.  Retained for the historical CSV pipeline.
    """
    if dt.tzinfo is None:
        if dt.hour < 6:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def _to_utc(dt: datetime) -> datetime:
    """Normalize *dt* to UTC (naive datetimes are assumed to already be UTC)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def exness_closures(symbol: str, dt: datetime) -> tuple[tuple[datetime, datetime, str], ...] | None:
    """Return supported scheduled closures around *dt*, in UTC."""
    utc = _to_utc(dt)
    profile = profile_for(symbol)
    hours = _EXNESS_HOURS.get(profile.symbol if profile else symbol.upper().rstrip("M.RAW#"))
    if hours is None or utc.date() < _EXNESS_HOURS_EFFECTIVE:
        return None

    friday_close, sunday_open, weekday_close, weekday_open = hours
    if friday_close is None:
        return ()

    local_day = utc.astimezone(_NEW_YORK).date()
    weekday = local_day.weekday()

    def at(day, hour_minute):
        hour, minute = hour_minute
        return datetime(day.year, day.month, day.day, hour, minute, tzinfo=_NEW_YORK).astimezone(timezone.utc)

    if weekday >= 4:
        friday = local_day - timedelta(days=weekday - 4)
        sunday = friday + timedelta(days=2)
        return ((at(friday, friday_close), at(sunday, sunday_open), "weekend_close"),)
    if weekday_close is not None:
        return ((at(local_day, weekday_close), at(local_day, weekday_open), "daily_maintenance"),)
    return ()


def exness_expected_bar_gap(
    symbol: str, previous: datetime, current: datetime, interval_minutes: int
) -> bool | None:
    """Allow missing bars only where their time buckets overlap a scheduled break."""
    previous, current = _to_utc(previous), _to_utc(current)
    step = timedelta(minutes=interval_minutes)
    closures = exness_closures(symbol, previous + step)
    if closures is None:
        return None
    if current <= previous + step:
        return False
    return any(
        first < previous + 2 * step and current - step < last
        for first, last, _ in closures
    )


def _in_window(utc: datetime, window: tuple) -> bool:
    """True if *utc*'s time-of-day falls inside a (close_h, close_m, open_h, open_m) window."""
    close_h, close_m, open_h, open_m = window
    cur = utc.hour * 60 + utc.minute
    start = close_h * 60 + close_m
    end = open_h * 60 + open_m
    if start <= end:
        return start <= cur < end
    # window wraps past midnight
    return cur >= start or cur < end


def _is_weekday_24_5_open(utc: datetime) -> bool:
    """Forex / metal / energy: closed Fri 22:00 UTC → Sun 22:00 UTC reopen.

    The Sunday-evening reopen matters twice over: it is real tradable session
    time, and it is where the weekend-gap context bars form — treating all of
    Sunday as closed made the system systematically blind to both.
    """
    weekday = utc.weekday()  # 0=Mon … 6=Sun
    hour = utc.hour
    if weekday == 4 and hour >= 22:   # Friday after 22:00 UTC
        return False
    if weekday == 5:                  # Saturday
        return False
    if weekday == 6:                  # Sunday — broker reopens 22:00 UTC
        return hour >= 22
    return True


def _is_index_open(utc: datetime, prof) -> bool:
    """Index CFD: weekday session with an earlier Friday close + a daily break.

    Reopens Sunday 22:00 UTC like the 24/5 assets (verified against the live
    Exness USTEC schedule, 2026-06-09).
    """
    weekday = utc.weekday()
    if weekday == 5:                          # Saturday
        return False
    if weekday == 6:                          # Sunday — reopens 22:00 UTC
        return utc.hour >= 22
    if weekday == 4 and utc.hour >= 21:       # Friday close ~21:00 UTC
        return False
    daily_break = getattr(prof, "daily_break", None)
    if daily_break and _in_window(utc, daily_break):
        return False
    return True


def is_session_open(dt: datetime, asset: str = "MT5") -> bool:
    """Return ``True`` if *dt* is within trading hours for *asset* (registry-driven).

    All timing is computed in UTC.  The asset's schedule comes from its
    :class:`~packages.core.asset_registry.AssetProfile`; unknown symbols use the
    portable weekday-24/5 model.
    """
    utc = _to_utc(dt)
    closures = exness_closures(asset, utc)
    if closures is not None:
        return not any(start <= utc < end for start, end, _ in closures)
    prof = profile_for(asset)
    session = prof.session if prof else "weekday_24_5"

    if session == "crypto_24_7":
        # Crypto trades through the weekend; the daily broker maintenance window
        # is enforced by the candle fetch / monitor layer, not session gating
        # (preserves the historical behavior where BTC/ETH were always "open").
        return True
    if session == "index":
        return _is_index_open(utc, prof)
    return _is_weekday_24_5_open(utc)
