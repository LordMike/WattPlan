"""Minimal Home Assistant test helpers used by the WattPlan test suite."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import time
from typing import Any
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import event
from homeassistant.util import dt as dt_util, ulid as ulid_util
from homeassistant.util.async_ import get_scheduled_timer_handles

SubentryData = getattr(config_entries, "ConfigSubentryData", dict[str, Any])


def make_subentry_data(
    *,
    subentry_id: str,
    subentry_type: str,
    title: str,
    unique_id: str,
    data: dict[str, Any],
) -> Any:
    """Build subentry payloads across Home Assistant API versions."""
    subentry_cls = getattr(config_entries, "ConfigSubentryData", None)
    if subentry_cls is not None:
        return subentry_cls(
            subentry_id=subentry_id,
            subentry_type=subentry_type,
            title=title,
            unique_id=unique_id,
            data=data,
        )

    return {
        "subentry_id": subentry_id,
        "subentry_type": subentry_type,
        "title": title,
        "unique_id": unique_id,
        "data": data,
    }


@callback
def async_fire_time_changed(
    hass: HomeAssistant, datetime_: datetime | None = None, fire_all: bool = False
) -> None:
    """Fire a time-changed event for scheduled callbacks in tests."""
    if datetime_ is None:
        utc_datetime = datetime.now(UTC)
    else:
        utc_datetime = dt_util.as_utc(datetime_)

    utc_datetime += timedelta(microseconds=event.RANDOM_MICROSECOND_MAX)
    _async_fire_time_changed(hass, utc_datetime, fire_all)


_MONOTONIC_RESOLUTION = time.get_clock_info("monotonic").resolution


@callback
def _async_fire_time_changed(
    hass: HomeAssistant, utc_datetime: datetime | None, fire_all: bool
) -> None:
    timestamp = utc_datetime.timestamp()
    for task in list(get_scheduled_timer_handles(hass.loop)):
        if not isinstance(task, asyncio.TimerHandle):
            continue
        if task.cancelled():
            continue

        mock_seconds_into_future = timestamp - time.time()
        future_seconds = task.when() - (hass.loop.time() + _MONOTONIC_RESOLUTION)

        if fire_all or mock_seconds_into_future >= future_seconds:
            with (
                patch(
                    "homeassistant.helpers.event.time_tracker_utcnow",
                    return_value=utc_datetime,
                ),
                patch(
                    "homeassistant.helpers.event.time_tracker_timestamp",
                    return_value=timestamp,
                ),
            ):
                task._run()
                task.cancel()


class MockConfigEntry(config_entries.ConfigEntry):
    """Helper for creating config entries with practical test defaults."""

    def __init__(
        self,
        *,
        data: dict[str, Any] | None = None,
        disabled_by: str | None = None,
        discovery_keys: dict[str, Any] | None = None,
        domain: str = "test",
        entry_id: str | None = None,
        minor_version: int = 1,
        options: dict[str, Any] | None = None,
        pref_disable_new_entities: bool | None = None,
        pref_disable_polling: bool | None = None,
        reason: str | None = None,
        source: str | None = config_entries.SOURCE_USER,
        state: config_entries.ConfigEntryState | None = None,
        subentries_data: list[Any] | None = None,
        title: str = "Mock Title",
        unique_id: str | None = None,
        version: int = 1,
    ) -> None:
        kwargs = {
            "data": data or {},
            "disabled_by": disabled_by,
            "discovery_keys": discovery_keys or {},
            "domain": domain,
            "entry_id": entry_id or ulid_util.ulid_now(),
            "minor_version": minor_version,
            "options": options or {},
            "pref_disable_new_entities": pref_disable_new_entities,
            "pref_disable_polling": pref_disable_polling,
            "subentries_data": subentries_data or (),
            "title": title,
            "unique_id": unique_id,
            "version": version,
        }
        if source is not None:
            kwargs["source"] = source
        if state is not None:
            kwargs["state"] = state
        super().__init__(**kwargs)
        if reason is not None:
            object.__setattr__(self, "reason", reason)

    def add_to_hass(self, hass: HomeAssistant) -> None:
        """Add the entry to Home Assistant's config entry manager."""
        hass.config_entries._entries[self.entry_id] = self

    async def start_reconfigure_flow(
        self,
        hass: HomeAssistant,
        *,
        show_advanced_options: bool = False,
    ) -> ConfigFlowResult:
        """Start a reconfigure flow for this entry."""
        return await hass.config_entries.flow.async_init(
            self.domain,
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": self.entry_id,
                "show_advanced_options": show_advanced_options,
            },
        )
