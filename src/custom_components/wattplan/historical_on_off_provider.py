"""Historical on/off runtime provider for comfort entities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant

from .rolling_history_cache import RollingHistoryCache


@dataclass(slots=True)
class OnOffSample:
    """One sampled on/off state at a specific timestamp."""

    at: datetime
    is_on: bool


class HistoricalOnOffProvider:
    """Provide comfort runtime context from recorder history with caching."""

    def __init__(self, hass: HomeAssistant, entity_id: str) -> None:
        """Initialize the historical provider for one entity."""
        self._hass = hass
        self._entity_id = entity_id
        self._history_cache = RollingHistoryCache(hass, entity_id)

    async def async_runtime_state(
        self, *, rolling_window_slots: int, slot_minutes: int
    ) -> tuple[bool, int, int]:
        """Return `(is_on_now, on_slots_last_window, off_streak_slots_now)`."""
        state = self._hass.states.get(self._entity_id)
        if state is None:
            raise ValueError(f"On/off source `{self._entity_id}` was not found")

        now = datetime.now(tz=UTC)
        slot_seconds = slot_minutes * 60
        window_start = now - timedelta(minutes=rolling_window_slots * slot_minutes)
        is_on_now = state.state == STATE_ON
        off_streak_slots_now = 0
        if not is_on_now:
            off_seconds = max(0.0, (now - state.last_changed).total_seconds())
            off_streak_slots_now = int(off_seconds // slot_seconds)

        if "recorder" not in self._hass.config.components:
            return is_on_now, 0, off_streak_slots_now

        states = await self._history_cache.async_fetch(window_start=window_start, now=now)
        samples = self._samples_from_states(states)
        on_seconds = self._on_seconds_between(samples=samples, start=window_start, end=now)
        on_slots = int(round(on_seconds / slot_seconds))
        on_slots = max(0, min(rolling_window_slots, on_slots))
        return is_on_now, on_slots, off_streak_slots_now

    def _samples_from_states(self, states: list[object]) -> list[OnOffSample]:
        """Convert recorder states into deduplicated on/off samples."""
        samples = [
            OnOffSample(
                at=self._as_utc(item.last_changed),
                is_on=item.state == STATE_ON,
            )
            for item in states
            if getattr(item, "last_changed", None) is not None
        ]
        return self._deduplicate(samples)

    def _on_seconds_between(
        self, *, samples: list[OnOffSample], start: datetime, end: datetime
    ) -> float:
        """Calculate ON seconds in the closed-open interval `[start, end)`."""
        if not samples or start >= end:
            return 0.0

        on_seconds = 0.0
        prev_state = samples[0].is_on
        prev_time = start
        for sample in samples:
            if sample.at <= start:
                prev_state = sample.is_on
                prev_time = start
                continue
            if sample.at >= end:
                break
            if prev_state:
                on_seconds += max(0.0, (sample.at - prev_time).total_seconds())
            prev_state = sample.is_on
            prev_time = sample.at

        if prev_state and prev_time < end:
            on_seconds += max(0.0, (end - prev_time).total_seconds())

        return on_seconds

    def _deduplicate(self, samples: list[OnOffSample]) -> list[OnOffSample]:
        """Collapse duplicate points and repeated same-state points."""
        deduped: list[OnOffSample] = []
        for sample in samples:
            if deduped and sample.at == deduped[-1].at:
                deduped[-1] = sample
                continue
            if deduped and sample.is_on == deduped[-1].is_on:
                continue
            deduped.append(sample)
        return deduped

    def _as_utc(self, value: datetime) -> datetime:
        """Normalize datetimes to timezone-aware UTC."""
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
