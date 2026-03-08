"""Source issue handling for WattPlan."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.issue_registry import IssueSeverity

from .const import (
    CONF_AGGREGATION_MODE,
    CONF_CLAMP_MODE,
    CONF_EDGE_FILL_MODE,
    CONF_FIXUP_PROFILE,
    CONF_RESAMPLE_MODE,
    CONF_SOURCE_PRICE,
    CONF_SOURCE_PV,
    CONF_SOURCE_USAGE,
    CONF_SOURCES,
    DOMAIN,
    FIXUP_PROFILE_EXTEND,
)


@dataclass(frozen=True, slots=True)
class SourceIssue:
    """Describe one active source issue for the repairs dashboard."""

    source_key: str
    kind: str
    is_fixable: bool
    placeholders: dict[str, str]
    data: dict[str, str | int | float | None]


def source_issue_id(entry_id: str, source_key: str, kind: str) -> str:
    """Return the stable repair issue id for one source and issue kind."""
    return f"{entry_id}_{source_key}_{kind}"


def source_display_name(source_key: str) -> str:
    """Return a user-facing source label."""
    return {
        CONF_SOURCE_PRICE: "price forecast",
        CONF_SOURCE_USAGE: "load forecast",
        CONF_SOURCE_PV: "solar forecast",
    }.get(source_key, source_key)


def source_display_title(source_key: str) -> str:
    """Return a title-cased source label for issue headings."""
    return {
        CONF_SOURCE_PRICE: "Price forecast",
        CONF_SOURCE_USAGE: "Load forecast",
        CONF_SOURCE_PV: "Solar forecast",
    }.get(source_key, source_key.title())


def source_issue_ids(entry_id: str, source_key: str) -> tuple[str, str]:
    """Return both issue ids for one source."""
    return (
        source_issue_id(entry_id, source_key, "source_unavailable"),
        source_issue_id(entry_id, source_key, "source_incomplete"),
    )


def apply_horizon_fill_defaults(source_config: dict[str, Any]) -> dict[str, Any]:
    """Return source config with the recommended fill defaults enabled.

    The repair action should be predictable. It always applies the same
    horizon-filling defaults that the source flows recommend for forecast data.
    """

    return {
        **source_config,
        CONF_FIXUP_PROFILE: FIXUP_PROFILE_EXTEND,
        CONF_AGGREGATION_MODE: "first",
        CONF_CLAMP_MODE: "nearest",
        CONF_RESAMPLE_MODE: "linear",
        CONF_EDGE_FILL_MODE: "hold",
    }


def source_fill_defaults_needed(source_config: dict[str, Any]) -> bool:
    """Return whether the source config differs from horizon-filling defaults."""
    repaired = apply_horizon_fill_defaults(source_config)
    return any(source_config.get(key) != repaired[key] for key in repaired if key in {
        CONF_FIXUP_PROFILE,
        CONF_AGGREGATION_MODE,
        CONF_CLAMP_MODE,
        CONF_RESAMPLE_MODE,
        CONF_EDGE_FILL_MODE,
    })


def update_entry_source_with_fill_defaults(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    source_key: str,
) -> bool:
    """Apply horizon-filling defaults to one configured source in place."""
    sources = entry.data.get(CONF_SOURCES, {})
    if not isinstance(sources, dict):
        return False
    source_config = sources.get(source_key)
    if not isinstance(source_config, dict):
        return False
    if not source_fill_defaults_needed(source_config):
        return False

    updated_sources = dict(sources)
    updated_sources[source_key] = apply_horizon_fill_defaults(source_config)
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, CONF_SOURCES: updated_sources},
    )
    return True


@callback
def sync_source_issues(
    hass: HomeAssistant,
    *,
    entry_id: str,
    issues: list[SourceIssue],
) -> None:
    """Replace the current set of source issues for one config entry."""
    keep = {source_issue_id(entry_id, issue.source_key, issue.kind) for issue in issues}
    for source_key in (CONF_SOURCE_PRICE, CONF_SOURCE_USAGE, CONF_SOURCE_PV):
        for issue_id in source_issue_ids(entry_id, source_key):
            if issue_id not in keep:
                ir.async_delete_issue(hass, DOMAIN, issue_id)

    for issue in issues:
        ir.async_create_issue(
            hass,
            DOMAIN,
            source_issue_id(entry_id, issue.source_key, issue.kind),
            data=issue.data,
            is_fixable=issue.is_fixable,
            is_persistent=False,
            severity=IssueSeverity.WARNING,
            translation_key=issue.kind,
            translation_placeholders=issue.placeholders,
        )


@callback
def clear_entry_source_issues(hass: HomeAssistant, entry_id: str) -> None:
    """Remove all source issues for one config entry."""
    sync_source_issues(hass, entry_id=entry_id, issues=[])


def build_source_issue(
    *,
    entry: ConfigEntry,
    source_key: str,
    kind: str,
    source_name: str,
    consequence: str,
    expires_at: datetime | None,
    available_count: int,
    required_count: int,
    is_fixable: bool,
) -> SourceIssue:
    """Build one repair issue with the translated placeholders."""
    slot_minutes = int(entry.data.get("slot_minutes", 60))
    covered_hours = _covered_hours(available_count, slot_minutes)
    required_hours = _covered_hours(required_count, slot_minutes)

    if expires_at is not None:
        first_failed_cycle = _first_failed_cycle(expires_at, slot_minutes)
        expires_local = first_failed_cycle.astimezone().strftime("%Y-%m-%d %H:%M %Z")
        relative_until_failure = _relative_until(first_failed_cycle)
        grace_note = (
            f"WattPlan is still using the last successful {source_name} data, "
            f"but the first planning cycle that will fail is in "
            f"{relative_until_failure} ({expires_local})."
        )
        expires_placeholder = first_failed_cycle.astimezone(UTC).isoformat()
    else:
        grace_note = (
            f"The last successful {source_name} data is no longer available, "
            "so this source already affects the current plan."
        )
        expires_placeholder = ""

    action = (
        "Review the configured source and submit the repair to enable horizon-filling "
        "defaults."
        if is_fixable
        else "Review whether the configured data source is online and still returning forecast data."
    )
    return SourceIssue(
        source_key=source_key,
        kind=kind,
        is_fixable=is_fixable,
        placeholders={
            "entry_title": entry.title,
            "source_name": source_name,
            "source_title": source_display_title(source_key),
            "consequence": consequence,
            "action": action,
            "grace_note": grace_note,
            "available_count": str(available_count),
            "required_count": str(required_count),
            "covered_hours": covered_hours,
            "required_hours": required_hours,
        },
        data={
            "entry_id": entry.entry_id,
            "source_key": source_key,
            "expires_at": expires_placeholder,
            "available_count": available_count,
            "required_count": required_count,
        },
    )


def _covered_hours(interval_count: int, slot_minutes: int) -> str:
    """Return one human-friendly duration string from slot counts."""
    hours = (interval_count * slot_minutes) / 60
    if float(hours).is_integer():
        return str(int(hours))
    return f"{hours:.1f}"


def _first_failed_cycle(expires_at: datetime, slot_minutes: int) -> datetime:
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


def _relative_until(target: datetime) -> str:
    """Return a short user-facing relative duration until one timestamp."""
    delta = max(target.astimezone(UTC) - datetime.now(tz=UTC), timedelta())
    total_minutes = int(delta.total_seconds() // 60)
    if total_minutes < 60:
        return f"{total_minutes} minutes"
    hours, minutes = divmod(total_minutes, 60)
    if minutes == 0:
        return f"{hours} hours"
    return f"{hours} hours {minutes} minutes"
