"""Unit tests for the shared datetime helpers."""

from datetime import UTC, datetime, timedelta

from custom_components.wattplan.datetime_utils import parse_datetime_like, typical_step

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def test_parse_datetime_like() -> None:
    assert parse_datetime_like(T0) is T0
    assert parse_datetime_like("2026-01-01T00:00:00+00:00") == T0
    assert parse_datetime_like("not a date") is None
    assert parse_datetime_like(1767225600) is None
    assert parse_datetime_like(None) is None


def test_typical_step_ignores_holes_and_short_tails() -> None:
    hour = timedelta(hours=1)
    stamps = [T0, T0 + hour, T0 + 2 * hour, T0 + 5 * hour, T0 + 6 * hour]
    assert typical_step(stamps) == hour


def test_typical_step_tie_goes_to_the_shorter_step() -> None:
    stamps = [T0, T0 + timedelta(minutes=15), T0 + timedelta(minutes=75)]
    assert typical_step(stamps) == timedelta(minutes=15)


def test_typical_step_needs_a_positive_step() -> None:
    assert typical_step([]) is None
    assert typical_step([T0]) is None
    assert typical_step([T0, T0]) is None
