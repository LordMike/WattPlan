"""Shared rolling recorder history cache for Home Assistant entities."""

from __future__ import annotations

from datetime import UTC, datetime
from functools import partial
from typing import Any

from homeassistant.components.recorder import get_instance, history
from homeassistant.core import HomeAssistant


class RollingHistoryCache:
    """Cache recorder state history and fetch only the delta after each read."""

    def __init__(self, hass: HomeAssistant, entity_id: str) -> None:
        """Initialize the cache for one entity."""
        self._hass = hass
        self._entity_id = entity_id
        self._entries: list[Any] = []
        self._cache_end: datetime | None = None

    async def async_fetch(self, *, window_start: datetime, now: datetime) -> list[Any]:
        """Return cached recorder states for one rolling window."""
        if self._cache_end is None or self._cache_end < window_start:
            fetch_start = window_start
            self._entries = []
        else:
            fetch_start = self._cache_end

        history_data = await get_instance(self._hass).async_add_executor_job(
            partial(
                history.state_changes_during_period,
                self._hass,
                fetch_start,
                now,
                entity_id=self._entity_id,
                include_start_time_state=True,
                no_attributes=True,
            )
        )
        entries = list(history_data.get(self._entity_id, []))
        if entries:
            self._entries.extend(entries)
            self._entries.sort(key=self._entry_changed_at)
            self._entries = self._deduplicate(self._entries)

        self._cache_end = now
        self._prune(window_start=window_start)
        return list(self._entries)

    def _prune(self, *, window_start: datetime) -> None:
        """Keep one boundary entry before the window and all newer entries."""
        if not self._entries:
            return

        keep_index = 0
        for index, entry in enumerate(self._entries):
            if self._entry_changed_at(entry) <= window_start:
                keep_index = index
            else:
                break
        self._entries = self._entries[keep_index:]

    def _deduplicate(self, entries: list[Any]) -> list[Any]:
        """Collapse duplicate timestamps, keeping the latest entry."""
        deduped: list[Any] = []
        for entry in entries:
            changed_at = self._entry_changed_at(entry)
            if deduped and self._entry_changed_at(deduped[-1]) == changed_at:
                deduped[-1] = entry
                continue
            deduped.append(entry)
        return deduped

    def _entry_changed_at(self, entry: Any) -> datetime:
        """Return normalized UTC timestamp for one recorder state entry."""
        changed = getattr(entry, "last_changed", None)
        if changed is None:
            raise ValueError(
                f"Recorder entry for `{self._entity_id}` is missing last_changed"
            )
        if changed.tzinfo is None:
            return changed.replace(tzinfo=UTC)
        return changed.astimezone(UTC)
