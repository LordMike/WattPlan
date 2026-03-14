"""Repairs flows for WattPlan source issues."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .datetime_utils import parse_datetime_like
from .source_issues import (
    _covered_hours,
    source_display_name,
    source_display_title,
    source_issue_id,
    update_entry_source_with_fill_defaults,
)


class SourceIncompleteRepairFlow(RepairsFlow):
    """Apply horizon-filling defaults for an incomplete source."""

    def __init__(
        self,
        entry_id: str,
        entry_title: str,
        source_key: str,
        *,
        available_count: int,
        required_count: int,
        covered_hours: str,
        required_hours: str,
        grace_note: str,
        consequence: str,
    ) -> None:
        """Initialize the repair flow."""
        self._entry_id = entry_id
        self._entry_title = entry_title
        self._source_key = source_key
        self._available_count = available_count
        self._required_count = required_count
        self._covered_hours = covered_hours
        self._required_hours = required_hours
        self._grace_note = grace_note
        self._consequence = consequence

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Start the repair flow."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Apply the repair and reload the entry."""
        if user_input is not None:
            entry = self.hass.config_entries.async_get_entry(self._entry_id)
            if entry is None:
                return self.async_abort(reason="entry_removed")

            changed = update_entry_source_with_fill_defaults(
                self.hass,
                entry,
                source_key=self._source_key,
            )
            if changed:
                await self.hass.config_entries.async_reload(self._entry_id)
            ir.async_delete_issue(
                self.hass,
                DOMAIN,
                source_issue_id(
                    self._entry_id,
                    self._source_key,
                    "source_incomplete",
                ),
            )
            return self.async_create_entry(data={})

        return self.async_show_form(
            step_id="confirm",
            description_placeholders={
                "entry_title": self._entry_title,
                "source_name": source_display_name(self._source_key),
                "source_title": source_display_title(self._source_key),
                "available_count": str(self._available_count),
                "required_count": str(self._required_count),
                "covered_hours": self._covered_hours,
                "required_hours": self._required_hours,
                "grace_note": self._grace_note,
                "consequence": self._consequence,
            },
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, Any] | None,
) -> RepairsFlow:
    """Create the repair flow for an incomplete source."""
    if not data or "entry_id" not in data or "source_key" not in data:
        raise ValueError("Missing data for repair flow")
    entry_id = str(data["entry_id"])
    source_key = str(data["source_key"])
    entry = hass.config_entries.async_get_entry(entry_id)
    entry_title = entry.title if entry else entry_id
    available_count = int(data.get("available_count", 0))
    required_count = int(data.get("required_count", 0))
    slot_minutes = int(entry.data.get("slot_minutes", 60)) if entry else 60
    if expires_at := parse_datetime_like(data.get("expires_at")):
        expires_dt = expires_at.astimezone()
        expires_local = expires_dt.strftime("%Y-%m-%d %H:%M %Z")
        now_local = datetime.now(tz=expires_dt.tzinfo)
        total_minutes = max(int((expires_dt - now_local).total_seconds() // 60), 0)
        hours, minutes = divmod(total_minutes, 60)
        relative_until_failure = (
            f"{hours} hours {minutes} minutes"
            if hours and minutes
            else f"{hours} hours"
            if hours
            else f"{minutes} minutes"
        )
        grace_note = (
            f"WattPlan is still using the last successful {source_display_name(source_key)} "
            f"data, but the first planning cycle that will fail is in "
            f"{relative_until_failure} ({expires_local})."
        )
    else:
        grace_note = (
            f"The last successful {source_display_name(source_key)} data is no longer "
            "available, so this source already affects the current plan."
        )
    consequence = (
        "WattPlan will continue, but it will plan without solar contribution for the missing period."
        if source_key == "pv"
        else "WattPlan will stop producing new plans."
    )
    return SourceIncompleteRepairFlow(
        entry_id,
        entry_title,
        source_key,
        available_count=available_count,
        required_count=required_count,
        covered_hours=_covered_hours(available_count, slot_minutes),
        required_hours=_covered_hours(required_count, slot_minutes),
        grace_note=grace_note,
        consequence=consequence,
    )
