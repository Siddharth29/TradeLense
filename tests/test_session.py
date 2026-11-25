from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from core.session import ensure_ist, is_session_open
from packages.core.session import exness_expected_bar_gap
from packages.core.services.code_sniper_monitor import _is_market_closed, _is_market_closing_soon
from packages.core.entry_policy import tradable_minutes


def _utc(y, m, d, h, mn=0):
    return datetime(y, m, d, h, mn, tzinfo=timezone.utc)


# Reference calendar: 2024-01-24 = Wed, 26 = Fri, 27 = Sat, 28 = Sun.

def test_ensure_ist_naive_utc():
    dt = datetime(2024, 1, 1, 3, 45)
    out = ensure_ist(dt)
    assert out.tzinfo == ZoneInfo("Asia/Kolkata")
    assert out.hour == 9 and out.minute == 15


def test_ensure_ist_naive_ist():
    dt = datetime(2024, 1, 1, 9, 15)
    out = ensure_ist(dt)
    assert out.tzinfo == ZoneInfo("Asia/Kolkata")
    assert out.hour == 9 and out.minute == 15


def test_weekday_24_5_open_and_weekend_closed():
    # Forex / metals: open on a weekday, closed over the weekend.
    assert is_session_open(_utc(2024, 1, 26, 2), "XAUUSD")        # Friday 02:00 UTC
    assert not is_session_open(_utc(2024, 1, 27, 10), "XAUUSD")   # Saturday
    assert not is_session_open(_utc(2024, 1, 26, 22), "GBPUSD")   # Friday after 22:00 UTC


def test_eurusd_open_midweek_not_nse_gated():
    # Regression: EURUSD used to fall through to the NSE 09:15-15:30 IST gate and
    # was "closed" most of the day. It must now follow the weekday-24/5 schedule.
    assert is_session_open(_utc(2024, 1, 24, 3), "EURUSD")        # Wed 03:00 UTC (08:30 IST)
    assert is_session_open(_utc(2024, 1, 24, 18), "EURUSD")       # Wed 18:00 UTC
    assert not is_session_open(_utc(2024, 1, 27, 12), "EURUSD")   # Saturday


def test_crypto_open_through_weekend():
    assert is_session_open(_utc(2024, 1, 27, 12), "BTCUSD")       # Saturday — crypto open
    assert is_session_open(_utc(2024, 1, 28, 3), "ETHUSD")        # Sunday — crypto open
    assert is_session_open(_utc(2024, 1, 27, 12), "BTCUSDm")      # demo 'm' suffix tolerated


def test_unknown_symbol_defaults_to_weekday_not_nse():
    assert is_session_open(_utc(2024, 1, 24, 12), "SOMETHINGNEW")     # Wednesday open
    assert not is_session_open(_utc(2024, 1, 27, 12), "SOMETHINGNEW")  # Saturday closed


def test_ustec_index_session():
    # Open during the regular weekday session...
    assert is_session_open(_utc(2024, 1, 24, 15), "USTEC")       # Wed 15:00 UTC
    # ...closed during the daily index break (~21:00-22:00 UTC)...
    assert not is_session_open(_utc(2024, 1, 24, 21, 30), "USTEC")
    # ...and closed over the weekend (incl. early Friday close).
    assert not is_session_open(_utc(2024, 1, 27, 15), "USTEC")   # Saturday
    assert not is_session_open(_utc(2024, 1, 26, 21, 30), "USTEC")  # Friday after close


def test_november_exness_hours_keep_scheduler_and_monitor_in_sync():
    wed = (2026, 11, 18)
    assert is_session_open(_utc(*wed, 21, 30), "USTEC")
    assert _is_market_closed("USTEC", _utc(*wed, 21, 30)) == (False, 0)
    assert not is_session_open(_utc(*wed, 22), "USTEC")
    assert _is_market_closed("USTEC", _utc(*wed, 22)) == (True, 3660)
    assert _is_market_closing_soon("USTEC", now=_utc(*wed, 21, 58)) == (True, "daily_maintenance")
    assert _is_market_closed("XAUUSD", _utc(*wed, 23, 1)) == (True, 60)
    assert _is_market_closed("EURUSD", _utc(*wed, 22)) == (False, 0)
    assert _is_market_closed("BTCUSD", _utc(*wed, 22)) == (False, 0)
    assert tradable_minutes("USTEC", _utc(*wed, 21, 30)) == 24
    assert tradable_minutes("EURUSD", _utc(2026, 11, 20, 21, 49)) == 5
    assert tradable_minutes("BTCUSD", _utc(*wed, 22)) is None


def test_exness_gap_allows_partial_session_bars_and_rejects_missing_open_bars():
    wed = (2026, 11, 18)
    assert exness_expected_bar_gap("XAUUSD", _utc(*wed, 21, 55), _utc(*wed, 23), 5)
    assert not exness_expected_bar_gap("XAUUSD", _utc(*wed, 21, 45), _utc(*wed, 23), 5)
    assert not exness_expected_bar_gap("XAUUSD", _utc(*wed, 21, 55), _utc(*wed, 23, 10), 5)
    assert exness_expected_bar_gap("GBPUSD", _utc(2026, 11, 20, 21, 55), _utc(2026, 11, 22, 22, 5), 5)
