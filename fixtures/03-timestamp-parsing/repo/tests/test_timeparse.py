from datetime import datetime, timedelta, timezone

import pytest

from events.timeparse import parse_timestamp

UTC = timezone.utc


def test_naive_input_is_utc():
    assert parse_timestamp("2026-03-01T12:30:00") == datetime(2026, 3, 1, 12, 30, tzinfo=UTC)


def test_z_suffix():
    assert parse_timestamp("2026-03-01T12:30:00Z") == datetime(2026, 3, 1, 12, 30, tzinfo=UTC)


def test_offset_is_converted_to_utc():
    dt = parse_timestamp("2026-03-01T14:30:00+02:00")
    assert dt == datetime(2026, 3, 1, 12, 30, tzinfo=UTC)
    assert dt.utcoffset() == timedelta(0)


def test_fractional_seconds():
    assert parse_timestamp("2026-03-01T12:30:00.250Z").microsecond == 250000


def test_invalid_raises_value_error():
    with pytest.raises(ValueError):
        parse_timestamp("yesterday")
