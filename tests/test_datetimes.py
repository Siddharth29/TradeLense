from datetime import datetime, timezone, timedelta

from apps.backend.utils.datetimes import to_aware_utc


def test_naive_datetime_to_utc():
    naive = datetime(2023, 1, 1, 0, 0, 0)
    res = to_aware_utc(naive)
    assert res.tzinfo == timezone.utc
    assert res == datetime(2023, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def test_aware_offset_datetime():
    offset = timezone(timedelta(hours=5, minutes=30))
    aware = datetime(2023, 1, 1, 12, 0, tzinfo=offset)
    res = to_aware_utc(aware)
    assert res.tzinfo == timezone.utc
    assert res == datetime(2023, 1, 1, 6, 30, tzinfo=timezone.utc)


def test_iso_z_and_offset_and_naive():
    assert to_aware_utc("2023-01-01T00:00:00Z") == datetime(
        2023, 1, 1, 0, 0, tzinfo=timezone.utc
    )
    assert to_aware_utc("2023-01-01T00:00:00+05:30") == datetime(
        2022, 12, 31, 18, 30, tzinfo=timezone.utc
    )
    assert to_aware_utc("2023-01-01T00:00:00") == datetime(
        2023, 1, 1, 0, 0, tzinfo=timezone.utc
    )


def test_epoch_seconds():
    assert to_aware_utc(0) == datetime(1970, 1, 1, 0, 0, tzinfo=timezone.utc)
    assert to_aware_utc("0") == datetime(1970, 1, 1, 0, 0, tzinfo=timezone.utc)
