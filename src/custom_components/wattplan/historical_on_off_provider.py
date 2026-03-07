"""Historical on/off runtime provider for comfort entities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial

from homeassistant.components.recorder import get_instance, history
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant


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
        self._samples: list[OnOffSample] = []
        self._cache_end: datetime | None = None

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

        await self._async_fetch_since_cache(window_start=window_start, now=now)
        self._prune_cache(window_start=window_start)
        on_seconds = self._on_seconds_between(start=window_start, end=now)
        on_slots = int(round(on_seconds / slot_seconds))
        on_slots = max(0, min(rolling_window_slots, on_slots))
        return is_on_now, on_slots, off_streak_slots_now

    async def _async_fetch_since_cache(
        self, *, window_start: datetime, now: datetime
    ) -> None:
        """Fetch recorder history incrementally from cache boundary to now."""
        if self._cache_end is None or self._cache_end < window_start:
            fetch_start = window_start
            self._samples = []
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
        states = list(history_data.get(self._entity_id, []))
        if not states:
            self._cache_end = now
            return

        new_samples = [
            OnOffSample(at=item.last_changed.astimezone(UTC), is_on=item.state == STATE_ON)
            for item in states
        ]
        self._samples.extend(new_samples)
        self._samples.sort(key=lambda sample: sample.at)
        self._samples = self._deduplicate(self._samples)
        self._cache_end = now

    def _prune_cache(self, *, window_start: datetime) -> None:
        """Prune cached samples and keep only one boundary sample before window."""
        if not self._samples:
            return

        keep_index = 0
        for index, sample in enumerate(self._samples):
            if sample.at <= window_start:
                keep_index = index
            else:
                break
        self._samples = self._samples[keep_index:]

    def _on_seconds_between(self, *, start: datetime, end: datetime) -> float:
        """Calculate ON seconds in the closed-open interval `[start, end)`."""
        if not self._samples or start >= end:
            return 0.0

        on_seconds = 0.0
        prev_state = self._samples[0].is_on
        prev_time = start
        for sample in self._samples:
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
