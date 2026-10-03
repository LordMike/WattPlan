"""Tests for WattPlan forecast provider behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import custom_components.wattplan.forecast_provider as provider_module
import custom_components.wattplan.rolling_history_cache as cache_module
from custom_components.wattplan.forecast_provider import ForecastProvider
from custom_components.wattplan.source_types import SourceProviderError, SourceWindow
import pytest

from homeassistant.core import HomeAssistant

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

_VALID_LOAD_ATTRS = {
    "state_class": "measurement",
    "device_class": "energy",
    "unit_of_measurement": "kWh",
}


class _FakeRecorder:
    """Recorder stub with queued history/statistics responses."""

    def __init__(
        self,
        history_response: dict[str, list[Any]] | None = None,
        statistics_response: dict[str, list[dict[str, Any]]] | None = None,
        history_responses: list[dict[str, list[Any]]] | None = None,
    ) -> None:
        """Initialize fake recorder response."""
        self._history_responses = (
            history_responses[:] if history_responses is not None else [history_response or {}]
        )
        self._statistics_response = statistics_response or {}
        self.calls = 0
        self.statistics_calls = 0
        self.fetch_starts: list[datetime] = []

    async def async_add_executor_job(self, _job: Any) -> dict[str, list[Any]]:
        """Return queued recorder response."""
        self.calls += 1
        if _job.func is provider_module.statistics_during_period:
            self.statistics_calls += 1
            return self._statistics_response
        self.fetch_starts.append(_job.args[1])
        return self._history_responses.pop(0)


async def test_forecast_weekday_weighting_prefers_same_weekday(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same-weekday interval deltas should be weighted higher than other weekdays."""
    hass.config.components.add("recorder")
    entity_id = "sensor.house_load_kwh"
    hass.states.async_set(entity_id, "3.1", _VALID_LOAD_ATTRS)

    start_at = datetime(2026, 1, 12, 1, 0, tzinfo=UTC)  # Monday
    history_states = {
        entity_id: [
            # Monday one week earlier.
            SimpleNamespace(
                state="10.0", last_changed=datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
            ),
            SimpleNamespace(
                state="20.0", last_changed=datetime(2026, 1, 5, 1, 0, tzinfo=UTC)
            ),
            # Tuesday one week earlier.
            SimpleNamespace(
                state="2.0", last_changed=datetime(2026, 1, 6, 0, 0, tzinfo=UTC)
            ),
            SimpleNamespace(
                state="4.0", last_changed=datetime(2026, 1, 6, 1, 0, tzinfo=UTC)
            ),
            # Wednesday one week earlier.
            SimpleNamespace(
                state="2.0", last_changed=datetime(2026, 1, 7, 0, 0, tzinfo=UTC)
            ),
            SimpleNamespace(
                state="4.0", last_changed=datetime(2026, 1, 7, 1, 0, tzinfo=UTC)
            ),
            # Thursday one week earlier.
            SimpleNamespace(
                state="2.0", last_changed=datetime(2026, 1, 8, 0, 0, tzinfo=UTC)
            ),
            SimpleNamespace(
                state="4.0", last_changed=datetime(2026, 1, 8, 1, 0, tzinfo=UTC)
            ),
            # Friday one week earlier.
            SimpleNamespace(
                state="2.0", last_changed=datetime(2026, 1, 9, 0, 0, tzinfo=UTC)
            ),
            SimpleNamespace(
                state="4.0", last_changed=datetime(2026, 1, 9, 1, 0, tzinfo=UTC)
            ),
            # Saturday one week earlier.
            SimpleNamespace(
                state="2.0", last_changed=datetime(2026, 1, 10, 0, 0, tzinfo=UTC)
            ),
            SimpleNamespace(
                state="4.0", last_changed=datetime(2026, 1, 10, 1, 0, tzinfo=UTC)
            ),
            # Sunday one week earlier.
            SimpleNamespace(
                state="2.0", last_changed=datetime(2026, 1, 11, 0, 0, tzinfo=UTC)
            ),
            SimpleNamespace(
                state="4.0", last_changed=datetime(2026, 1, 11, 1, 0, tzinfo=UTC)
            ),
        ]
    }
    recorder = _FakeRecorder(history_states)
    monkeypatch.setattr(cache_module, "get_instance", lambda _hass: recorder)
    monkeypatch.setattr(provider_module, "get_instance", lambda _hass: recorder)

    provider = ForecastProvider(
        hass,
        entity_id=entity_id,
        lookback_days=7,
        same_weekday_weight=3.0,
        other_weekday_weight=1.0,
        recency_decay=0.0,
    )

    values = await provider.async_values(
        SourceWindow(start_at=start_at, slot_minutes=60, slots=1)
    )

    # Each delta is spread across the hour it covers. At 01:00, the observed
    # interval deltas are 2 kWh across all weekdays in this synthetic series.
    assert values == pytest.approx([2.0])


async def test_forecast_requires_recorder(hass: HomeAssistant) -> None:
    """Provider should fail clearly when recorder is not available."""
    hass.states.async_set("sensor.house_load_kwh", "3.1", _VALID_LOAD_ATTRS)
    provider = ForecastProvider(hass, entity_id="sensor.house_load_kwh")

    with pytest.raises(SourceProviderError, match="Recorder is required"):
        await provider.async_values(
            SourceWindow(
                start_at=datetime(2026, 1, 12, 0, 0, tzinfo=UTC),
                slot_minutes=60,
                slots=24,
            )
        )


async def test_forecast_requires_numeric_history(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provider should error when no numeric samples are present."""
    hass.config.components.add("recorder")
    entity_id = "sensor.house_load_kwh"
    hass.states.async_set(entity_id, "3.1", _VALID_LOAD_ATTRS)
    history_states = {
        entity_id: [
            SimpleNamespace(
                state="unknown", last_changed=datetime(2026, 1, 10, 0, 0, tzinfo=UTC)
            ),
            SimpleNamespace(
                state="n/a", last_changed=datetime(2026, 1, 11, 0, 0, tzinfo=UTC)
            ),
        ]
    }
    recorder = _FakeRecorder(history_states)
    monkeypatch.setattr(cache_module, "get_instance", lambda _hass: recorder)
    monkeypatch.setattr(provider_module, "get_instance", lambda _hass: recorder)
    provider = ForecastProvider(hass, entity_id=entity_id)

    with pytest.raises(SourceProviderError, match="No numeric history"):
        await provider.async_values(
            SourceWindow(
                start_at=datetime(2026, 1, 12, 0, 0, tzinfo=UTC),
                slot_minutes=60,
                slots=24,
            )
        )


async def test_forecast_uses_long_term_statistics_when_state_history_is_sparse(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Long-term cumulative statistics should be converted to interval deltas."""
    hass.config.components.add("recorder")
    entity_id = "sensor.house_load_kwh"
    hass.states.async_set(entity_id, "3.1", _VALID_LOAD_ATTRS)
    recorder = _FakeRecorder(
        history_response={entity_id: []},
        statistics_response={
            entity_id: [
                {"start": datetime(2026, 1, 10, 0, 0, tzinfo=UTC), "sum": 100.0},
                {"start": datetime(2026, 1, 11, 0, 0, tzinfo=UTC), "sum": 103.0},
                {"start": datetime(2026, 1, 12, 0, 0, tzinfo=UTC), "sum": 109.0},
            ]
        },
    )
    monkeypatch.setattr(cache_module, "get_instance", lambda _hass: recorder)
    monkeypatch.setattr(provider_module, "get_instance", lambda _hass: recorder)

    provider = ForecastProvider(
        hass,
        entity_id=entity_id,
        lookback_days=14,
        same_weekday_weight=1.0,
        other_weekday_weight=1.0,
        recency_decay=0.0,
    )
    values = await provider.async_values(
        SourceWindow(
            start_at=datetime(2026, 1, 12, 0, 0, tzinfo=UTC),
            slot_minutes=60,
            slots=1,
        )
    )

    # The 3 kWh and 6 kWh daily deltas are spread across 24 hourly intervals.
    assert values == pytest.approx([4.5 / 24.0])


async def test_forecast_accepts_non_kwh_when_history_is_numeric(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Numeric cumulative history should be accepted even when metadata is not ideal."""
    hass.config.components.add("recorder")
    entity_id = "sensor.house_load_kwh"
    hass.states.async_set(
        entity_id, "3.1", {"state_class": "measurement", "unit_of_measurement": "W"}
    )
    recorder = _FakeRecorder(
        history_response={
            entity_id: [
                SimpleNamespace(
                    state="1.0", last_changed=datetime(2026, 1, 11, 0, 0, tzinfo=UTC)
                ),
                SimpleNamespace(
                    state="2.0", last_changed=datetime(2026, 1, 11, 1, 0, tzinfo=UTC)
                ),
                SimpleNamespace(
                    state="4.0", last_changed=datetime(2026, 1, 11, 2, 0, tzinfo=UTC)
                ),
            ]
        },
        statistics_response={entity_id: []},
    )
    monkeypatch.setattr(cache_module, "get_instance", lambda _hass: recorder)
    monkeypatch.setattr(provider_module, "get_instance", lambda _hass: recorder)

    provider = ForecastProvider(hass, entity_id=entity_id)
    values = await provider.async_values(
        SourceWindow(
            start_at=datetime(2026, 1, 12, 1, 0, tzinfo=UTC),
            slot_minutes=60,
            slots=2,
        )
    )
    assert values == pytest.approx([2.0, 2.0])


async def test_forecast_uses_meter_deltas_not_raw_totals(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cumulative meter history should forecast interval usage, not raw totals."""
    hass.config.components.add("recorder")
    entity_id = "sensor.house_load_kwh"
    hass.states.async_set(entity_id, "140.0", _VALID_LOAD_ATTRS)
    recorder = _FakeRecorder(
        history_response={
            entity_id: [
                SimpleNamespace(
                    state="100.0", last_changed=datetime(2026, 1, 11, 0, 0, tzinfo=UTC)
                ),
                SimpleNamespace(
                    state="101.0", last_changed=datetime(2026, 1, 11, 1, 0, tzinfo=UTC)
                ),
                SimpleNamespace(
                    state="102.0", last_changed=datetime(2026, 1, 11, 2, 0, tzinfo=UTC)
                ),
                SimpleNamespace(
                    state="103.0", last_changed=datetime(2026, 1, 11, 3, 0, tzinfo=UTC)
                ),
            ]
        },
        statistics_response={entity_id: []},
    )
    monkeypatch.setattr(cache_module, "get_instance", lambda _hass: recorder)
    monkeypatch.setattr(provider_module, "get_instance", lambda _hass: recorder)

    provider = ForecastProvider(hass, entity_id=entity_id)
    values = await provider.async_values(
        SourceWindow(
            start_at=datetime(2026, 1, 12, 1, 0, tzinfo=UTC),
            slot_minutes=60,
            slots=2,
        )
    )

    assert values == pytest.approx([1.0, 1.0])


async def test_forecast_handles_meter_reset(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative deltas from a meter reset should be skipped cleanly."""
    hass.config.components.add("recorder")
    entity_id = "sensor.house_load_kwh"
    hass.states.async_set(entity_id, "5.0", _VALID_LOAD_ATTRS)
    recorder = _FakeRecorder(
        history_response={
            entity_id: [
                SimpleNamespace(
                    state="100.0", last_changed=datetime(2026, 1, 11, 0, 0, tzinfo=UTC)
                ),
                SimpleNamespace(
                    state="101.0", last_changed=datetime(2026, 1, 11, 1, 0, tzinfo=UTC)
                ),
                SimpleNamespace(
                    state="3.0", last_changed=datetime(2026, 1, 11, 2, 0, tzinfo=UTC)
                ),
                SimpleNamespace(
                    state="4.0", last_changed=datetime(2026, 1, 11, 3, 0, tzinfo=UTC)
                ),
            ]
        },
        statistics_response={entity_id: []},
    )
    monkeypatch.setattr(cache_module, "get_instance", lambda _hass: recorder)
    monkeypatch.setattr(provider_module, "get_instance", lambda _hass: recorder)

    provider = ForecastProvider(hass, entity_id=entity_id, recency_decay=0.0)
    values = await provider.async_values(
        SourceWindow(
            start_at=datetime(2026, 1, 12, 1, 0, tzinfo=UTC),
            slot_minutes=60,
            slots=2,
        )
    )

    assert values == pytest.approx([1.0, 1.0])


async def test_slot_observation_above_power_limit_is_dropped_not_zeroed(
    hass: HomeAssistant,
) -> None:
    """A slot total above the power limit must not drag the average down.

    Overlapping samples are not reachable through the public history path, so
    the slot builder is exercised directly with crafted samples.
    """
    provider = ForecastProvider(hass, entity_id="sensor.house_load_kwh")

    def hour(day: int) -> tuple[datetime, datetime]:
        return (
            datetime(2026, 1, day, 10, 0, tzinfo=UTC),
            datetime(2026, 1, day, 11, 0, tzinfo=UTC),
        )

    samples = [(*hour(10), 1.0), (*hour(11), 1.0)]
    # Two overlapping segments that each stay under the limit but sum above it.
    samples += [(*hour(12), 40.0), (*hour(12), 40.0)]
    debug_events: list[dict[str, Any]] = []

    by_slot = provider._build_slot_observations(
        samples=samples,
        history_start=datetime(2026, 1, 10, 0, 0, tzinfo=UTC),
        end_at=datetime(2026, 1, 13, 0, 0, tzinfo=UTC),
        slot_delta=timedelta(hours=1),
        slot_minutes=60,
        max_interval_kwh=50.0,
        debug_events=debug_events,
    )

    # Slot index depends on the local timezone, so look at the only populated one.
    assert len(by_slot) == 1
    observations = next(iter(by_slot.values()))
    assert [value for value, _, _ in observations] == pytest.approx([1.0, 1.0])
    assert [event["kind"] for event in debug_events] == ["slot_observation_clamped"]
    assert "replacement_value" not in debug_events[0]
    assert provider._weighted_average(
        observations=observations, target_weekday=0, fallback=0.0
    ) == pytest.approx(1.0)


async def test_forecast_spreads_sparse_meter_delta_over_elapsed_time(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A large daily delta should be spread across 15-minute intervals."""
    hass.config.components.add("recorder")
    entity_id = "sensor.house_load_kwh"
    hass.states.async_set(entity_id, "140.0", _VALID_LOAD_ATTRS)
    recorder = _FakeRecorder(
        history_response={
            entity_id: [
                SimpleNamespace(
                    state="100.0", last_changed=datetime(2026, 1, 10, 0, 0, tzinfo=UTC)
                ),
                SimpleNamespace(
                    state="140.0", last_changed=datetime(2026, 1, 11, 0, 0, tzinfo=UTC)
                ),
            ]
        },
        statistics_response={entity_id: []},
    )
    monkeypatch.setattr(cache_module, "get_instance", lambda _hass: recorder)
    monkeypatch.setattr(provider_module, "get_instance", lambda _hass: recorder)

    provider = ForecastProvider(
        hass,
        entity_id=entity_id,
        same_weekday_weight=1.0,
        other_weekday_weight=1.0,
        recency_decay=0.0,
    )
    values = await provider.async_values(
        SourceWindow(
            start_at=datetime(2026, 1, 12, 0, 0, tzinfo=UTC),
            slot_minutes=15,
            slots=4,
        )
    )

    assert values == pytest.approx([40.0 / 96.0] * 4)


async def test_forecast_counts_only_in_window_share_of_clipped_segment(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A segment straddling the lookback start only contributes its in-window share."""
    hass.config.components.add("recorder")
    entity_id = "sensor.house_load_kwh"
    hass.states.async_set(entity_id, "124.0", _VALID_LOAD_ATTRS)
    # History starts at 2026-01-11 00:00. The meter reads 12h before that and
    # 12h after it, so half of the 24 kWh delta falls inside the window.
    recorder = _FakeRecorder(
        history_response={
            entity_id: [
                SimpleNamespace(
                    state="100.0", last_changed=datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
                ),
                SimpleNamespace(
                    state="124.0", last_changed=datetime(2026, 1, 11, 12, 0, tzinfo=UTC)
                ),
            ]
        },
        statistics_response={entity_id: []},
    )
    monkeypatch.setattr(cache_module, "get_instance", lambda _hass: recorder)
    monkeypatch.setattr(provider_module, "get_instance", lambda _hass: recorder)

    provider = ForecastProvider(
        hass,
        entity_id=entity_id,
        lookback_days=1,
        same_weekday_weight=1.0,
        other_weekday_weight=1.0,
        recency_decay=0.0,
    )
    values = await provider.async_values(
        SourceWindow(
            start_at=datetime(2026, 1, 12, 0, 0, tzinfo=UTC),
            slot_minutes=15,
            slots=4,
        )
    )

    # 24 kWh over 24h is 0.25 kWh per 15 minutes; the clipped-duration divisor
    # would have produced 0.5.
    assert values == pytest.approx([0.25] * 4)


async def test_forecast_fetches_incrementally_from_cache_end(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second usage refresh should only fetch recorder history since cache end."""
    hass.config.components.add("recorder")
    entity_id = "sensor.house_load_kwh"
    hass.states.async_set(entity_id, "140.0", _VALID_LOAD_ATTRS)

    recorder = _FakeRecorder(
        history_responses=[
            {
                entity_id: [
                    SimpleNamespace(
                        state="100.0", last_changed=datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
                    ),
                    SimpleNamespace(
                        state="101.0", last_changed=datetime(2026, 1, 1, 1, 0, tzinfo=UTC)
                    ),
                ]
            },
            {
                entity_id: [
                    SimpleNamespace(
                        state="101.0", last_changed=datetime(2026, 1, 1, 1, 0, tzinfo=UTC)
                    ),
                    SimpleNamespace(
                        state="102.0", last_changed=datetime(2026, 1, 1, 2, 0, tzinfo=UTC)
                    ),
                ]
            },
        ],
        statistics_response={entity_id: []},
    )
    monkeypatch.setattr(cache_module, "get_instance", lambda _hass: recorder)
    monkeypatch.setattr(provider_module, "get_instance", lambda _hass: recorder)

    provider = ForecastProvider(
        hass,
        entity_id=entity_id,
        lookback_days=14,
        same_weekday_weight=1.0,
        other_weekday_weight=1.0,
        recency_decay=0.0,
    )

    await provider.async_values(
        SourceWindow(
            start_at=datetime(2026, 1, 1, 1, 0, tzinfo=UTC),
            slot_minutes=60,
            slots=1,
        )
    )
    await provider.async_values(
        SourceWindow(
            start_at=datetime(2026, 1, 1, 2, 0, tzinfo=UTC),
            slot_minutes=60,
            slots=1,
        )
    )

    assert recorder.fetch_starts == [
        datetime(2025, 12, 18, 1, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 1, 0, tzinfo=UTC),
    ]


class _BoundaryRowRecorder:
    """Recorder stub mimicking HA's include_start_time_state boundary row."""

    def __init__(self, entity_id: str, states: list[SimpleNamespace]) -> None:
        """Store the full real state history."""
        self._entity_id = entity_id
        self._states = states
        self.calls = 0

    async def async_add_executor_job(self, job: Any) -> dict[str, list[Any]]:
        """Answer history queries; statistics queries return nothing."""
        self.calls += 1
        if job.func.__name__ != "state_changes_during_period":
            return {}
        start, end = job.args[1], job.args[2]
        before = [s for s in self._states if s.last_changed <= start]
        rows = [s for s in self._states if start < s.last_changed <= end]
        if before:
            # Like HA: last state at/before start, last_changed rewritten to start.
            rows.insert(
                0, SimpleNamespace(state=before[-1].state, last_changed=start)
            )
        return {self._entity_id: rows}


async def test_forecast_warm_cache_matches_cold_cache_with_boundary_rows(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated start-time rows from incremental fetches must not skew intervals."""
    hass.config.components.add("recorder")
    entity_id = "sensor.house_load_kwh"
    hass.states.async_set(entity_id, "100.0", _VALID_LOAD_ATTRS)

    base = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    states = [
        SimpleNamespace(state=str(100.0 + hour), last_changed=base + timedelta(hours=hour))
        for hour in range(72)
    ]
    recorder = _BoundaryRowRecorder(entity_id, states)
    monkeypatch.setattr(cache_module, "get_instance", lambda _hass: recorder)
    monkeypatch.setattr(provider_module, "get_instance", lambda _hass: recorder)

    def _provider() -> ForecastProvider:
        return ForecastProvider(
            hass,
            entity_id=entity_id,
            lookback_days=2,
            same_weekday_weight=1.0,
            other_weekday_weight=1.0,
            recency_decay=0.0,
        )

    def _window(start_at: datetime) -> SourceWindow:
        return SourceWindow(start_at=start_at, slot_minutes=15, slots=96)

    warm = _provider()
    start_at = base + timedelta(days=2)
    for tick in range(17):
        warm_values = await warm.async_values(
            _window(start_at + timedelta(minutes=15 * tick))
        )
    final_start = start_at + timedelta(minutes=15 * 16)
    cold_values = await _provider().async_values(_window(final_start))

    # The warm period covers the first hours of the day, so a full-day forecast
    # exercises the time-of-day slots that the repeated boundary rows touched.
    assert cold_values == pytest.approx([0.25] * 96)
    assert warm_values == pytest.approx(cold_values)


async def test_forecast_restarts_full_fetch_when_window_moves_past_cache(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later window outside the cached lookback should reset the cache."""
    hass.config.components.add("recorder")
    entity_id = "sensor.house_load_kwh"
    hass.states.async_set(entity_id, "140.0", _VALID_LOAD_ATTRS)

    first_start = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)
    second_start = first_start + timedelta(days=20)
    recorder = _FakeRecorder(
        history_responses=[
            {entity_id: []},
            {entity_id: []},
        ],
        statistics_response={
            entity_id: [
                {"start": datetime(2026, 1, 14, 0, 0, tzinfo=UTC), "sum": 100.0},
                {"start": datetime(2026, 1, 15, 0, 0, tzinfo=UTC), "sum": 124.0},
                {"start": datetime(2026, 2, 3, 0, 0, tzinfo=UTC), "sum": 200.0},
                {"start": datetime(2026, 2, 4, 0, 0, tzinfo=UTC), "sum": 224.0},
            ]
        },
    )
    monkeypatch.setattr(cache_module, "get_instance", lambda _hass: recorder)
    monkeypatch.setattr(provider_module, "get_instance", lambda _hass: recorder)

    provider = ForecastProvider(
        hass,
        entity_id=entity_id,
        lookback_days=14,
        same_weekday_weight=1.0,
        other_weekday_weight=1.0,
        recency_decay=0.0,
    )

    await provider.async_values(SourceWindow(start_at=first_start, slot_minutes=60, slots=1))
    await provider.async_values(
        SourceWindow(start_at=second_start, slot_minutes=60, slots=1)
    )

    assert recorder.fetch_starts == [
        datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2026, 1, 21, 0, 0, tzinfo=UTC),
    ]


def _hourly_meter_states(
    start: datetime, hours: int, *, spike_hour_utc: int
) -> list[SimpleNamespace]:
    """Return hourly cumulative readings using 5 kWh in one UTC hour a day."""
    total = 0.0
    states: list[SimpleNamespace] = []
    for hour in range(hours + 1):
        at = start + timedelta(hours=hour)
        if hour:
            total += 5.0 if (at - timedelta(hours=1)).hour == spike_hour_utc else 1.0
        states.append(SimpleNamespace(state=str(total), last_changed=at))
    return states


async def test_forecast_load_pattern_follows_local_time_across_dst(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """History from before a DST change must land on the same local hour."""
    await hass.config.async_set_time_zone("Europe/Copenhagen")
    hass.config.components.add("recorder")
    entity_id = "sensor.house_load_kwh"
    hass.states.async_set(entity_id, "3.1", _VALID_LOAD_ATTRS)
    # Peak usage at 08:00 local. Before the change on 2026-10-25 that is
    # 06:00 UTC (CEST); afterwards it is 07:00 UTC (CET).
    recorder = _FakeRecorder(
        history_response={
            entity_id: _hourly_meter_states(
                datetime(2026, 10, 20, 0, 0, tzinfo=UTC), 48, spike_hour_utc=6
            )
        }
    )
    monkeypatch.setattr(cache_module, "get_instance", lambda _hass: recorder)
    monkeypatch.setattr(provider_module, "get_instance", lambda _hass: recorder)

    provider = ForecastProvider(hass, entity_id=entity_id, lookback_days=14)
    values = await provider.async_values(
        SourceWindow(
            start_at=datetime(2026, 10, 27, 7, 0, tzinfo=UTC),  # 08:00 CET
            slot_minutes=60,
            slots=2,
        )
    )

    assert values == pytest.approx([5.0, 1.0])


async def test_forecast_fetches_statistics_only_when_needed(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Long-term statistics are a fallback and are not queried needlessly."""
    hass.config.components.add("recorder")
    entity_id = "sensor.house_load_kwh"
    hass.states.async_set(entity_id, "3.1", _VALID_LOAD_ATTRS)
    recorder = _FakeRecorder(
        history_responses=[
            {
                entity_id: _hourly_meter_states(
                    datetime(2026, 1, 11, 0, 0, tzinfo=UTC), 6, spike_hour_utc=99
                )
            }
        ]
        * 2,
    )
    monkeypatch.setattr(cache_module, "get_instance", lambda _hass: recorder)
    monkeypatch.setattr(provider_module, "get_instance", lambda _hass: recorder)
    window = SourceWindow(
        start_at=datetime(2026, 1, 12, 1, 0, tzinfo=UTC), slot_minutes=60, slots=1
    )
    provider = ForecastProvider(hass, entity_id=entity_id)

    await provider.async_values(window)
    assert recorder.statistics_calls == 0

    payload = await provider.async_debug_payload(window)
    assert recorder.statistics_calls == 1
    assert payload["selected_sample_source"] == "recorder_history"
    assert payload["forecast_values"]
