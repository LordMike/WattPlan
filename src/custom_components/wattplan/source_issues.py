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
    CONF_SOURCE_EXPORT_PRICE,
    CONF_SOURCE_IMPORT_PRICE,
    CONF_SOURCE_PV,
    CONF_SOURCE_USAGE,
    CONF_SOURCES,
    DOMAIN,
)
from .source_config import apply_horizon_fill_defaults, source_fill_defaults_needed
from .source_health_presenter import (
    covered_hours as _covered_hours,
    source_display_name,
    source_display_title,
    stale_grace_note,
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


def source_issue_ids(entry_id: str, source_key: str) -> tuple[str, str]:
    """Return both issue ids for one source."""
    return (
        source_issue_id(entry_id, source_key, "source_unavailable"),
        source_issue_id(entry_id, source_key, "source_incomplete"),
    )


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
    for source_key in (
        CONF_SOURCE_IMPORT_PRICE,
        CONF_SOURCE_EXPORT_PRICE,
        CONF_SOURCE_USAGE,
        CONF_SOURCE_PV,
    ):
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
    grace_note, expires_placeholder = stale_grace_note(
        source_key=source_key,
        expires_at=expires_at,
        slot_minutes=slot_minutes,
    )

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
