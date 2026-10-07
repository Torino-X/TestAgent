from datetime import datetime, timedelta, timezone

from app.utils.task_timing import duration_ms, normalize_utc, to_rfc3339


def test_normalize_utc_treats_legacy_naive_values_as_utc() -> None:
    value = datetime(2026, 7, 24, 12, 0, 0)

    normalized = normalize_utc(value)

    assert normalized == value.replace(tzinfo=timezone.utc)
    assert to_rfc3339(value) == "2026-07-24T12:00:00+00:00"


def test_duration_ms_normalizes_mixed_timezone_values() -> None:
    started = datetime(2026, 7, 24, 12, 0, 0)
    finished = datetime(2026, 7, 24, 20, 0, 1, tzinfo=timezone(timedelta(hours=8)))

    assert duration_ms(started, finished) == 1000


def test_duration_ms_does_not_return_negative_values() -> None:
    started = datetime(2026, 7, 24, 12, 0, 1, tzinfo=timezone.utc)
    finished = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)

    assert duration_ms(started, finished) == 0
