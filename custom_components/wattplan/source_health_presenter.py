"""Shared presentation helpers for source health, issues, and repairs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .const import (
    CONF_SOURCE_EXPORT_PRICE,
    CONF_SOURCE_IMPORT_PRICE,
    CONF_SOURCE_PV,
    CONF_SOURCE_USAGE,
)


def source_display_name(source_key: str) -> str:
    """Return a user-facing source label."""
    return {
        CONF_SOURCE_IMPORT_PRICE: "price forecast",
        CONF_SOURCE_EXPORT_PRICE: "export price forecast",
        CONF_SOURCE_USAGE: "load forecast",
        CONF_SOURCE_PV: "solar forecast",
    }.get(source_key, source_key)


def source_display_title(source_key: str) -> str:
    """Return a title-cased source label for issue headings."""
    return {
        CONF_SOURCE_IMPORT_PRICE: "Price forecast",
        CONF_SOURCE_EXPORT_PRICE: "Export price forecast",
        CONF_SOURCE_USAGE: "Load forecast",
        CONF_SOURCE_PV: "Solar forecast",
    }.get(source_key, source_key.title())


def source_consequence(source_key: str) -> str:
    """Return source-specific planning consequence text for issue and repair UX."""
    if source_key == CONF_SOURCE_PV:
        return (
            "WattPlan will continue, but it will plan without solar contribution "
            "for the missing period."
        )
    if source_key == CONF_SOURCE_EXPORT_PRICE:
        return (
            "WattPlan will continue, but exported power will be valued at zero "
            "for the missing period."
        )
    return "WattPlan will stop producing new plans."


def covered_hours(interval_count: int, slot_minutes: int) -> str:
    """Return one human-friendly duration string from slot counts."""
    hours = (interval_count * slot_minutes) / 60
    if float(hours).is_integer():
        return str(int(hours))
    return f"{hours:.1f}"


def first_failed_cycle(expires_at: datetime, slot_minutes: int) -> datetime:
    """Return the first aligned planner cycle that can no longer use stale data."""
    slot_delta = timedelta(minutes=slot_minutes)
    expires_utc = expires_at.astimezone(UTC)
    seconds = int(expires_utc.timestamp())
    slot_seconds = int(slot_delta.total_seconds())
    aligned_seconds = ((seconds + slot_seconds - 1) // slot_seconds) * slot_seconds
    candidate = datetime.fromtimestamp(aligned_seconds, tz=UTC)
    if candidate <= expires_utc:
        candidate += slot_delta
    return candidate


def relative_until(target: datetime) -> str:
    """Return a short user-facing relative duration until one timestamp."""
    delta = max(target.astimezone(UTC) - datetime.now(tz=UTC), timedelta())
    total_minutes = int(delta.total_seconds() // 60)
    if total_minutes < 60:
        return f"{total_minutes} minutes"
    hours, minutes = divmod(total_minutes, 60)
    if minutes == 0:
        return f"{hours} hours"
    return f"{hours} hours {minutes} minutes"


def stale_grace_note(
    *,
    source_key: str,
    expires_at: datetime | None,
    slot_minutes: int,
) -> tuple[str, str]:
    """Return user-facing stale fallback text and serialized expiry placeholder."""
    source_name = source_display_name(source_key)
    if expires_at is None:
        return (
            f"The last successful {source_name} data is no longer available, "
            "so this source already affects the current plan.",
            "",
        )

    first_failed = first_failed_cycle(expires_at, slot_minutes)
    expires_local = first_failed.astimezone().strftime("%Y-%m-%d %H:%M %Z")
    relative = relative_until(first_failed)
    return (
        f"WattPlan is still using the last successful {source_name} data, "
        f"but the first planning cycle that will fail is in {relative} ({expires_local}).",
        first_failed.astimezone(UTC).isoformat(),
    )
