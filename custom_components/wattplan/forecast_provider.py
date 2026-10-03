"""Forecast provider for WattPlan load estimates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging
import math
from math import isnan
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .rolling_history_cache import RollingHistoryCache
from .source_types import SourceProvider, SourceProviderError, SourceWindow

_LOGGER = logging.getLogger(__name__)
MAX_IMPLIED_POWER_KW = 50.0


@dataclass(slots=True)
class _ForecastRun:
    """Intermediate results of one forecast computation."""

    start_at: datetime
    state: Any
    states: list[Any]
    samples: list[tuple[datetime, datetime, float]]
    by_slot: dict[int, list[tuple[float, int, int]]]
    debug_events: list[dict[str, Any]]
    values: list[float]


class ForecastProvider(SourceProvider):
    """Provide quick weekday-weighted load forecasts from recorder history."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        entity_id: str,
        lookback_days: int = 28,
        same_weekday_weight: float = 3.0,
        other_weekday_weight: float = 1.0,
        recency_decay: float = 0.05,
    ) -> None:
        """Initialize one forecast provider instance."""
        if lookback_days < 1:
            raise SourceProviderError(
                "source_validation",
                "lookback_days must be at least 1",
                details={"lookback_days": lookback_days},
            )
        if same_weekday_weight <= 0 or other_weekday_weight <= 0:
            raise SourceProviderError(
                "source_validation",
                "weekday weights must be greater than zero",
                details={
                    "same_weekday_weight": same_weekday_weight,
                    "other_weekday_weight": other_weekday_weight,
                },
            )

        self._hass = hass
        self._entity_id = entity_id
        self._lookback_days = lookback_days
        self._same_weekday_weight = same_weekday_weight
        self._other_weekday_weight = other_weekday_weight
        self._recency_decay = max(0.0, recency_decay)
        self._history_cache = RollingHistoryCache(hass, entity_id)

    async def async_forecast(self, window: SourceWindow) -> list[float]:
        """Return exactly `window.slots` forecast values."""
        return await self.async_values(window)

    async def async_values(self, window: SourceWindow) -> list[float]:
        """Return exactly `window.slots` forecast values."""
        run = await self._async_run(window)
        return run.values

    async def async_debug_payload(self, window: SourceWindow) -> dict[str, Any]:
        """Return raw and normalized forecast data for debugging."""
        run = await self._async_run(window)
        return self._debug_payload(window, run)

    async def _async_run(self, window: SourceWindow) -> _ForecastRun:
        """Compute forecast values from recorder state history."""
        if window.slot_minutes <= 0:
            raise SourceProviderError(
                "source_validation",
                "slot_minutes must be greater than zero",
                details={"slot_minutes": window.slot_minutes},
            )
        if window.slots <= 0:
            raise SourceProviderError(
                "source_validation",
                "slots must be greater than zero",
                details={"slots": window.slots},
            )
        state = self._hass.states.get(self._entity_id)
        if state is None:
            raise SourceProviderError(
                "source_fetch",
                f"Built-in load source `{self._entity_id}` was not found",
                details={
                    "entity_id": self._entity_id,
                    "built_in_reason": "entity_not_found",
                },
            )

        if "recorder" not in self._hass.config.components:
            raise SourceProviderError(
                "source_fetch",
                "Recorder is required for load forecasting",
                details={
                    "entity_id": self._entity_id,
                    "built_in_reason": "recorder_missing",
                },
            )

        start_at = self._as_utc(window.start_at)
        slot_delta = timedelta(minutes=window.slot_minutes)
        history_start = start_at - timedelta(days=self._lookback_days)
        max_interval_kwh = MAX_IMPLIED_POWER_KW * (window.slot_minutes / 60.0)
        debug_events: list[dict[str, Any]] = []

        try:
            states = await self._history_cache.async_fetch(
                window_start=history_start,
                now=start_at,
            )
        except Exception as err:  # noqa: BLE001
            raise self._recorder_error(err) from err
        # Recorder states are treated as cumulative meter readings and converted
        # into usage segments: `(segment_start, segment_end, energy_delta_kwh)`.
        samples = self._delta_state_samples(states)
        if not samples:
            raise SourceProviderError(
                "source_parse",
                (
                    f"No numeric history found for `{self._entity_id}` in the "
                    f"last {self._lookback_days} days"
                ),
                details={
                    "entity_id": self._entity_id,
                    "built_in_reason": "no_numeric_history",
                },
            )

        samples.sort(key=lambda sample: sample[1])
        # Segment data can span long periods between two meter readings. We do
        # not want to drop the full delta into the segment end time. Instead,
        # spread each segment proportionally across the planner interval size so
        # a 24-hour meter jump becomes many small 15-minute observations.
        by_slot = self._build_slot_observations(
            samples=samples,
            history_start=history_start,
            end_at=start_at,
            slot_delta=slot_delta,
            slot_minutes=window.slot_minutes,
            max_interval_kwh=max_interval_kwh,
            debug_events=debug_events,
        )

        all_values = [value for values in by_slot.values() for value, _weekday, _age in values]
        if not all_values:
            raise SourceProviderError(
                "source_parse",
                (
                    f"No usable slot history found for `{self._entity_id}` in the "
                    f"last {self._lookback_days} days"
                ),
                details={"entity_id": self._entity_id},
            )

        fallback = sum(all_values) / len(all_values)
        local_tz = dt_util.get_default_time_zone()
        values: list[float] = []
        for slot_index in range(window.slots):
            at = start_at + (slot_delta * slot_index)
            local_at = at.astimezone(local_tz)
            minute_of_day = (local_at.hour * 60) + local_at.minute
            day_slot = minute_of_day // window.slot_minutes
            observations = by_slot.get(day_slot, [])
            # Forecast each future interval by comparing it with the same
            # time-of-day intervals in history, then weight same weekday higher
            # than other weekdays.
            forecast_value = self._weighted_average(
                observations=observations,
                target_weekday=local_at.weekday(),
                fallback=values[-1] if values else fallback,
            )
            if forecast_value > max_interval_kwh:
                replacement = values[-1] if values else min(fallback, max_interval_kwh)
                debug_events.append(
                    {
                        "kind": "forecast_value_clamped",
                        "slot_start": at.isoformat(),
                        "original_value": forecast_value,
                        "replacement_value": replacement,
                        "max_interval_kwh": max_interval_kwh,
                    }
                )
                _LOGGER.debug(
                    "Discarding forecast outlier for %s at %s: %.3f kWh > %.3f kWh, using %.3f kWh",
                    self._entity_id,
                    at.isoformat(),
                    forecast_value,
                    max_interval_kwh,
                    replacement,
                )
                forecast_value = replacement
            values.append(forecast_value)

        return _ForecastRun(
            start_at=start_at,
            state=state,
            states=states,
            samples=samples,
            by_slot=by_slot,
            debug_events=debug_events,
            values=values,
        )

    def _recorder_error(self, err: Exception) -> SourceProviderError:
        """Wrap a recorder failure as a source fetch error."""
        return SourceProviderError(
            "source_fetch",
            f"Recorder query failed for `{self._entity_id}`: {err}",
            details={
                "entity_id": self._entity_id,
                "built_in_reason": "recorder_error",
            },
        )

    def _debug_payload(
        self, window: SourceWindow, run: _ForecastRun
    ) -> dict[str, Any]:
        """Build the diagnostics payload from one forecast run."""
        start_at = run.start_at
        state = run.state
        states = run.states
        samples = run.samples
        by_slot = run.by_slot
        debug_events = run.debug_events
        values = run.values
        return {
            "entity_id": self._entity_id,
            "window": {
                "start_at": start_at.isoformat(),
                "slot_minutes": window.slot_minutes,
                "slots": window.slots,
                "lookback_days": self._lookback_days,
            },
            "entity_state": {
                "state": state.state,
                "attributes": dict(state.attributes),
            },
            "raw_history_states": [
                {
                    "state": getattr(item, "state", None),
                    "last_changed": (
                        self._as_utc(item.last_changed).isoformat()
                        if getattr(item, "last_changed", None) is not None
                        else None
                    ),
                }
                for item in states
            ],
            "normalized_segments": [
                {
                    "start": segment_start.isoformat(),
                    "end": segment_end.isoformat(),
                    "delta_kwh": delta,
                }
                for segment_start, segment_end, delta in samples
            ],
            "slot_observations": {
                str(slot_key): [
                    {
                        "value_kwh": value,
                        "weekday": weekday,
                        "age_days": age_days,
                    }
                    for value, weekday, age_days in observations
                ]
                for slot_key, observations in by_slot.items()
            },
            "guardrail_events": debug_events,
            "forecast_values": values,
        }

    def _delta_state_samples(
        self, states: list[Any]
    ) -> list[tuple[datetime, datetime, float]]:
        """Extract interval usage segments from cumulative recorder states."""
        samples: list[tuple[datetime, datetime, float]] = []
        previous_value: float | None = None
        previous_changed: datetime | None = None
        for state in states:
            raw = getattr(state, "state", None)
            changed = getattr(state, "last_changed", None)
            if changed is None:
                continue
            changed_at = self._as_utc(changed)
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if isnan(value):
                continue
            if previous_value is None or previous_changed is None:
                previous_value = value
                previous_changed = changed_at
                continue

            # Recorder history is also assumed cumulative. Each positive delta
            # becomes one usage segment spanning from the previous reading to
            # the current reading.
            delta = value - previous_value
            if delta == 0:
                # Recorder history only stores changes, so an unchanged value is
                # the synthetic start-time row Home Assistant adds at each
                # fetch boundary. Keep the segment open until the real change.
                continue
            previous_value = value
            segment_start = previous_changed
            previous_changed = changed_at
            # Treat negative deltas as meter resets. We skip the reset jump itself
            # and continue from the new baseline on the next sample.
            if not math.isfinite(delta) or delta < 0:
                continue
            if changed_at <= segment_start:
                continue
            duration_hours = (changed_at - segment_start).total_seconds() / 3600.0
            if duration_hours <= 0:
                continue
            if (delta / duration_hours) > MAX_IMPLIED_POWER_KW:
                _LOGGER.debug(
                    "Discarding recorder segment for %s from %s to %s: %.3f kWh over %.3f h implies %.3f kW",
                    self._entity_id,
                    segment_start.isoformat(),
                    changed_at.isoformat(),
                    delta,
                    duration_hours,
                    delta / duration_hours,
                )
                continue
            samples.append((segment_start, changed_at, delta))
        return samples

    def _build_slot_observations(
        self,
        *,
        samples: list[tuple[datetime, datetime, float]],
        history_start: datetime,
        end_at: datetime,
        slot_delta: timedelta,
        slot_minutes: int,
        max_interval_kwh: float,
        debug_events: list[dict[str, Any]],
    ) -> dict[int, list[tuple[float, int, int]]]:
        """Build historical observations keyed by time-of-day slot."""
        by_slot: dict[int, list[tuple[float, int, int]]] = {}
        slot_totals: dict[datetime, float] = {}
        for segment_start, segment_end, delta in samples:
            if segment_end <= history_start or segment_start >= end_at:
                continue
            bounded_start = max(segment_start, history_start)
            bounded_end = min(segment_end, end_at)
            if bounded_end <= bounded_start:
                continue
            duration_seconds = (segment_end - segment_start).total_seconds()
            if duration_seconds <= 0:
                continue

            # Spread a segment's energy across every planner interval it overlaps
            # in proportion to overlap duration. This is what prevents sparse
            # daily or hourly meter readings from becoming huge single-slot
            # values in the forecast. The divisor is the full segment duration,
            # so a segment clipped by the window only contributes its in-window
            # share of the energy.
            slot_start = history_start + (
                ((bounded_start - history_start) // slot_delta) * slot_delta
            )
            while slot_start < bounded_end:
                slot_end = min(slot_start + slot_delta, bounded_end)
                overlap_start = max(slot_start, bounded_start)
                overlap_end = min(slot_end, bounded_end)
                overlap_seconds = (overlap_end - overlap_start).total_seconds()
                if overlap_seconds > 0:
                    slot_totals[slot_start] = slot_totals.get(slot_start, 0.0) + (
                        delta * (overlap_seconds / duration_seconds)
                    )
                slot_start += slot_delta

        local_tz = dt_util.get_default_time_zone()
        local_end_date = end_at.astimezone(local_tz).date()
        for slot_start, slot_value in slot_totals.items():
            if slot_value > max_interval_kwh:
                debug_events.append(
                    {
                        "kind": "slot_observation_clamped",
                        "slot_start": slot_start.isoformat(),
                        "original_value": slot_value,
                        "max_interval_kwh": max_interval_kwh,
                    }
                )
                _LOGGER.debug(
                    "Dropping slot observation for %s at %s: %.3f kWh > %.3f kWh",
                    self._entity_id,
                    slot_start.isoformat(),
                    slot_value,
                    max_interval_kwh,
                )
                continue
            # Key by local time so the pattern stays aligned across DST changes.
            local_start = slot_start.astimezone(local_tz)
            minute_of_day = (local_start.hour * 60) + local_start.minute
            day_slot = minute_of_day // slot_minutes
            age_days = (local_end_date - local_start.date()).days
            by_slot.setdefault(day_slot, []).append(
                (slot_value, local_start.weekday(), age_days)
            )

        return by_slot

    def _weighted_average(
        self,
        *,
        observations: list[tuple[float, int, int]],
        target_weekday: int,
        fallback: float,
    ) -> float:
        """Return weighted average for one target slot."""
        if not observations:
            return fallback

        weighted_total = 0.0
        weight_total = 0.0
        for value, weekday, age_days in observations:
            weekday_weight = (
                self._same_weekday_weight
                if weekday == target_weekday
                else self._other_weekday_weight
            )
            recency_weight = 1.0 / (1.0 + (self._recency_decay * age_days))
            weight = weekday_weight * recency_weight
            weighted_total += value * weight
            weight_total += weight

        if weight_total <= 0:
            return fallback
        return weighted_total / weight_total

    def _as_utc(self, value: datetime) -> datetime:
        """Normalize datetimes to timezone-aware UTC."""
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
