"""Unit tests for the shared source health presentation helpers."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.wattplan.const import (
    CONF_SOURCE_EXPORT_PRICE,
    CONF_SOURCE_IMPORT_PRICE,
    CONF_SOURCE_PV,
    CONF_SOURCE_USAGE,
)
from custom_components.wattplan.source_health_presenter import (
    covered_hours,
    first_failed_cycle,
    relative_until,
    source_consequence,
    source_display_name,
    source_display_title,
    stale_grace_note,
)

NOW = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("key", "name", "title"),
    [
        (CONF_SOURCE_IMPORT_PRICE, "price forecast", "Price forecast"),
        (CONF_SOURCE_EXPORT_PRICE, "export price forecast", "Export price forecast"),
        (CONF_SOURCE_USAGE, "load forecast", "Load forecast"),
        (CONF_SOURCE_PV, "solar forecast", "Solar forecast"),
    ],
)
def test_known_sources_have_names_and_titles(key, name, title) -> None:
    assert source_display_name(key) == name
    assert source_display_title(key) == title


def test_unknown_source_falls_back_to_the_key() -> None:
    assert source_display_name("custom_thing") == "custom_thing"
    assert source_display_title("custom_thing") == "Custom_Thing"


def test_consequence_differs_for_optional_and_required_sources() -> None:
    assert "without solar" in source_consequence(CONF_SOURCE_PV)
    assert "valued at zero" in source_consequence(CONF_SOURCE_EXPORT_PRICE)
    for key in (CONF_SOURCE_IMPORT_PRICE, CONF_SOURCE_USAGE):
        assert source_consequence(key) == "WattPlan will stop producing new plans."


@pytest.mark.parametrize(
    ("intervals", "slot_minutes", "expected"),
    [(24, 60, "24"), (4, 15, "1"), (3, 15, "0.8"), (10, 15, "2.5"), (0, 60, "0")],
)
def test_covered_hours(intervals, slot_minutes, expected) -> None:
    assert covered_hours(intervals, slot_minutes) == expected


def test_first_failed_cycle_aligns_up_to_the_next_slot() -> None:
    expires = datetime(2026, 1, 1, 10, 7, tzinfo=UTC)
    assert first_failed_cycle(expires, 15) == datetime(2026, 1, 1, 10, 15, tzinfo=UTC)


def test_first_failed_cycle_after_an_exact_boundary_is_the_next_slot() -> None:
    """Data that expires exactly on a boundary is still unusable at that cycle."""
    expires = datetime(2026, 1, 1, 10, 15, tzinfo=UTC)
    assert first_failed_cycle(expires, 15) == datetime(2026, 1, 1, 10, 30, tzinfo=UTC)


def test_first_failed_cycle_accepts_non_utc_input() -> None:
    expires = datetime(2026, 1, 1, 11, 7, tzinfo=ZoneInfo("Europe/Copenhagen"))
    assert first_failed_cycle(expires, 60) == datetime(2026, 1, 1, 11, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(minutes=-5), "0 minutes"),
        (timedelta(minutes=45, seconds=30), "45 minutes"),
        (timedelta(hours=2), "2 hours"),
        (timedelta(hours=1, minutes=30), "1 hours 30 minutes"),
    ],
)
def test_relative_until(freezer, delta, expected) -> None:
    freezer.move_to(NOW)
    assert relative_until(NOW + delta) == expected


def test_stale_grace_note_without_expiry_reports_data_gone() -> None:
    text, expiry = stale_grace_note(
        source_key=CONF_SOURCE_PV, expires_at=None, slot_minutes=15
    )
    assert "solar forecast data is no longer available" in text
    assert expiry == ""


def test_stale_grace_note_with_expiry_reports_first_failing_cycle(freezer) -> None:
    freezer.move_to(NOW)
    text, expiry = stale_grace_note(
        source_key=CONF_SOURCE_IMPORT_PRICE,
        expires_at=datetime(2026, 1, 1, 10, 40, tzinfo=UTC),
        slot_minutes=60,
    )
    assert "last successful price forecast data" in text
    assert "is in 1 hours (" in text
    assert expiry == "2026-01-01T11:00:00+00:00"
