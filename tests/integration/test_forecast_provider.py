"""Tests for WattPlan forecast provider behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import custom_components.wattplan.rolling_history_cache as cache_module
from custom_components.wattplan.forecast_provider import ForecastProvider
from custom_components.wattplan.source_types import SourceProviderError, SourceWindow
import pytest

from homeassistant.core import HomeAssistant

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

_ENTITY_ID = "sensor.house_load_kwh"
_VALID_LOAD_ATTRS = {
    "state_class": "measurement",
    "device_class": "energy",
    "unit_of_measurement": "kWh",
}


class _FakeRecorder:
    """Recorder stub with queued history responses."""

    def __init__(
        self,
        history_response: dict[str, list[Any]] | None = None,
        history_responses: list[dict[str, list[Any]]] | None = None,
    ) -> None:
        """Initialize fake recorder response."""
        self._history_responses = (
            history_responses[:] if history_responses is not None else [history_response or {}]
        )
        self.calls = 0
        self.fetch_starts: list[datetime] = []

    async def async_add_executor_job(self, _job: Any) -> dict[str, list[Any]]:
        """Return queued recorder response."""
        self.calls += 1
        self.fetch_starts.append(_job.args[1])
        return self._history_responses.pop(0)


def _row(at: datetime, state: float | str) -> SimpleNamespace:
    """Return one recorder history row."""
    return SimpleNamespace(state=str(state), last_changed=at)


def _window(start_at: datetime, *, slot_minutes: int = 60, slots: int = 1) -> SourceWindow:
    """Return a forecast window."""
    return SourceWindow(start_at=start_at, slot_minutes=slot_minutes, slots=slots)


def _provider_with_history(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    *responses: list[SimpleNamespace],
    attrs: dict[str, str] | None = None,
    **provider_kwargs: Any,
) -> tuple[ForecastProvider, _FakeRecorder]:
    """Return a provider whose recorder answers each fetch with the next rows."""
    hass.config.components.add("recorder")
    hass.states.async_set(_ENTITY_ID, "3.1", attrs or _VALID_LOAD_ATTRS)
    recorder = _FakeRecorder(
        history_responses=[{_ENTITY_ID: rows} for rows in responses]
    )
    monkeypatch.setattr(cache_module, "get_instance", lambda _hass: recorder)
    return ForecastProvider(hass, entity_id=_ENTITY_ID, **provider_kwargs), recorder


async def test_forecast_weekday_weighting_prefers_same_weekday(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same-weekday observations should outweigh other weekdays at the 01:00 slot.

    The meter resets to zero at 01:00 each day, which adds no consumption of its
    own, and then reads the 01:00-02:00 consumption an hour later.
    """
    daily_kwh = {5: 10.0, 6: 2.0, 7: 2.0, 8: 2.0, 9: 2.0, 10: 2.0, 11: 2.0}
    rows: list[SimpleNamespace] = []
    for day, kwh in daily_kwh.items():
        rows.append(_row(datetime(2026, 1, day, 1, 0, tzinfo=UTC), 0.0))
        rows.append(_row(datetime(2026, 1, day, 2, 0, tzinfo=UTC), kwh))
    provider, _recorder = _provider_with_history(
        hass,
        monkeypatch,
        rows,
        lookback_days=14,
        same_weekday_weight=3.0,
        other_weekday_weight=1.0,
        recency_decay=0.0,
    )

    # Monday 2026-01-12 01:00. The only Monday observation is 10 kWh (2026-01-05).
    values = await provider.async_values(_window(datetime(2026, 1, 12, 1, 0, tzinfo=UTC)))

    # Weighted 3:1 this is (3 * 10 + 6 * 2) / 9; equal weights would give 22 / 7.
    assert values == pytest.approx([42.0 / 9.0])


async def test_forecast_requires_recorder(hass: HomeAssistant) -> None:
    """Provider should fail clearly when recorder is not available."""
    hass.states.async_set(_ENTITY_ID, "3.1", _VALID_LOAD_ATTRS)
    provider = ForecastProvider(hass, entity_id=_ENTITY_ID)

    with pytest.raises(SourceProviderError, match="Recorder is required"):
        await provider.async_values(
            _window(datetime(2026, 1, 12, 0, 0, tzinfo=UTC), slots=24)
        )


async def test_forecast_requires_numeric_history(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provider should error when no numeric samples are present."""
    provider, _recorder = _provider_with_history(
        hass,
        monkeypatch,
        [
            _row(datetime(2026, 1, 10, 0, 0, tzinfo=UTC), "unknown"),
            _row(datetime(2026, 1, 11, 0, 0, tzinfo=UTC), "n/a"),
        ],
    )

    with pytest.raises(SourceProviderError, match="No numeric history"):
        await provider.async_values(
            _window(datetime(2026, 1, 12, 0, 0, tzinfo=UTC), slots=24)
        )


async def test_forecast_accepts_non_kwh_when_history_is_numeric(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Numeric cumulative history should be accepted even when metadata is not ideal."""
    provider, _recorder = _provider_with_history(
        hass,
        monkeypatch,
        [
            _row(datetime(2026, 1, 11, 0, 0, tzinfo=UTC), 1.0),
            _row(datetime(2026, 1, 11, 1, 0, tzinfo=UTC), 2.0),
            _row(datetime(2026, 1, 11, 2, 0, tzinfo=UTC), 4.0),
        ],
        attrs={"state_class": "measurement", "unit_of_measurement": "W"},
    )

    values = await provider.async_values(
        _window(datetime(2026, 1, 12, 1, 0, tzinfo=UTC), slots=2)
    )

    assert values == pytest.approx([2.0, 2.0])


async def test_forecast_uses_meter_deltas_not_raw_totals(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cumulative meter history should forecast interval usage, not raw totals."""
    provider, _recorder = _provider_with_history(
        hass,
        monkeypatch,
        [_row(datetime(2026, 1, 11, hour, 0, tzinfo=UTC), 100.0 + hour) for hour in range(4)],
    )

    values = await provider.async_values(
        _window(datetime(2026, 1, 12, 1, 0, tzinfo=UTC), slots=2)
    )

    assert values == pytest.approx([1.0, 1.0])


async def test_forecast_handles_meter_reset(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first reading after a meter reset counts as consumption since the reset."""
    provider, _recorder = _provider_with_history(
        hass,
        monkeypatch,
        [
            _row(datetime(2026, 1, 11, 0, 0, tzinfo=UTC), 100.0),
            _row(datetime(2026, 1, 11, 1, 0, tzinfo=UTC), 101.0),
            _row(datetime(2026, 1, 11, 2, 0, tzinfo=UTC), 3.0),
            _row(datetime(2026, 1, 11, 3, 0, tzinfo=UTC), 4.0),
        ],
        recency_decay=0.0,
    )

    values = await provider.async_values(
        _window(datetime(2026, 1, 12, 1, 0, tzinfo=UTC), slots=2)
    )

    # Hour 1->2 is the reset (101 -> 3): the 3.0 reading is the consumption since
    # the reset, so the slots at 02:00 and 03:00 see 3.0 and 1.0.
    assert values == pytest.approx([3.0, 1.0])


async def test_forecast_ignores_small_meter_decrease(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A decrease of less than 10% is meter noise and adds no consumption."""
    provider, _recorder = _provider_with_history(
        hass,
        monkeypatch,
        [
            _row(datetime(2026, 1, 11, 0, 0, tzinfo=UTC), 100.0),
            _row(datetime(2026, 1, 11, 1, 0, tzinfo=UTC), 99.5),
            _row(datetime(2026, 1, 11, 2, 0, tzinfo=UTC), 100.5),
        ],
        recency_decay=0.0,
    )

    values = await provider.async_values(
        _window(datetime(2026, 1, 12, 1, 0, tzinfo=UTC), slots=2)
    )

    # Only the 99.5 -> 100.5 hour counts; the decrease itself adds nothing.
    assert values[0] == pytest.approx(1.0)


async def test_forecast_spreads_sparse_meter_delta_over_elapsed_time(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A large daily delta should be spread across 15-minute intervals."""
    provider, _recorder = _provider_with_history(
        hass,
        monkeypatch,
        [
            _row(datetime(2026, 1, 10, 0, 0, tzinfo=UTC), 100.0),
            _row(datetime(2026, 1, 11, 0, 0, tzinfo=UTC), 140.0),
        ],
        same_weekday_weight=1.0,
        other_weekday_weight=1.0,
        recency_decay=0.0,
    )

    values = await provider.async_values(
        _window(datetime(2026, 1, 12, 0, 0, tzinfo=UTC), slot_minutes=15, slots=4)
    )

    assert values == pytest.approx([40.0 / 96.0] * 4)


async def test_forecast_counts_only_in_window_share_of_clipped_segment(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A segment straddling the lookback start only contributes its in-window share."""
    # History starts at 2026-01-11 00:00. The meter reads 12h before that and
    # 12h after it, so half of the 24 kWh delta falls inside the window.
    provider, _recorder = _provider_with_history(
        hass,
        monkeypatch,
        [
            _row(datetime(2026, 1, 10, 12, 0, tzinfo=UTC), 100.0),
            _row(datetime(2026, 1, 11, 12, 0, tzinfo=UTC), 124.0),
        ],
        lookback_days=1,
        same_weekday_weight=1.0,
        other_weekday_weight=1.0,
        recency_decay=0.0,
    )

    values = await provider.async_values(
        _window(datetime(2026, 1, 12, 0, 0, tzinfo=UTC), slot_minutes=15, slots=4)
    )

    # 24 kWh over 24h is 0.25 kWh per 15 minutes; the clipped-duration divisor
    # would have produced 0.5.
    assert values == pytest.approx([0.25] * 4)


async def test_forecast_cache_fetches_incrementally_then_restarts_after_gap(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refreshes fetch only since the cache end, and a far later window resets it."""
    provider, recorder = _provider_with_history(
        hass,
        monkeypatch,
        [
            _row(datetime(2026, 1, 1, 0, 0, tzinfo=UTC), 100.0),
            _row(datetime(2026, 1, 1, 1, 0, tzinfo=UTC), 101.0),
        ],
        [
            _row(datetime(2026, 1, 1, 1, 0, tzinfo=UTC), 101.0),
            _row(datetime(2026, 1, 1, 2, 0, tzinfo=UTC), 102.0),
        ],
        [
            _row(datetime(2026, 1, 20, 0, 0, tzinfo=UTC), 200.0),
            _row(datetime(2026, 1, 21, 0, 0, tzinfo=UTC), 224.0),
        ],
        lookback_days=14,
        same_weekday_weight=1.0,
        other_weekday_weight=1.0,
        recency_decay=0.0,
    )

    await provider.async_values(_window(datetime(2026, 1, 1, 1, 0, tzinfo=UTC)))
    await provider.async_values(_window(datetime(2026, 1, 1, 2, 0, tzinfo=UTC)))
    # 20 days later the lookback no longer overlaps the cached history.
    await provider.async_values(_window(datetime(2026, 1, 21, 2, 0, tzinfo=UTC)))

    assert recorder.fetch_starts == [
        datetime(2025, 12, 18, 1, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 1, 0, tzinfo=UTC),
        datetime(2026, 1, 7, 2, 0, tzinfo=UTC),
    ]


class _BoundaryRowRecorder:
    """Recorder stub mimicking HA's include_start_time_state boundary row."""

    def __init__(self, entity_id: str, states: list[SimpleNamespace]) -> None:
        """Store the full real state history."""
        self._entity_id = entity_id
        self._states = states
        self.calls = 0

    async def async_add_executor_job(self, job: Any) -> dict[str, list[Any]]:
        """Answer history queries like Home Assistant does."""
        self.calls += 1
        start, end = job.args[1], job.args[2]
        before = [s for s in self._states if s.last_changed <= start]
        rows = [s for s in self._states if start < s.last_changed <= end]
        if before:
            # Like HA: last state at/before start, last_changed rewritten to start.
            rows.insert(0, _row(start, before[-1].state))
        return {self._entity_id: rows}


def _steady_hourly_meter() -> list[SimpleNamespace]:
    """Return 120 hourly readings of a meter using 1 kWh per hour."""
    base = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    return [_row(base + timedelta(hours=hour), 100.0 + hour) for hour in range(120)]


async def test_forecast_warm_cache_matches_cold_cache_with_boundary_rows(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated start-time rows from incremental fetches must not skew intervals."""
    hass.config.components.add("recorder")
    hass.states.async_set(_ENTITY_ID, "100.0", _VALID_LOAD_ATTRS)
    recorder = _BoundaryRowRecorder(_ENTITY_ID, _steady_hourly_meter())
    monkeypatch.setattr(cache_module, "get_instance", lambda _hass: recorder)

    def _provider() -> ForecastProvider:
        return ForecastProvider(
            hass,
            entity_id=_ENTITY_ID,
            lookback_days=2,
            same_weekday_weight=1.0,
            other_weekday_weight=1.0,
            recency_decay=0.0,
        )

    warm = _provider()
    start_at = datetime(2026, 1, 3, 0, 0, tzinfo=UTC)
    # The pattern is rebuilt at most hourly, so tick every 75 minutes: each tick
    # refetches, and the fetch boundaries rotate through the quarter hours
    # instead of always landing on a meter change.
    ticks = 22
    for tick in range(ticks):
        warm_values = await warm.async_values(
            _window(start_at + timedelta(minutes=75 * tick), slot_minutes=15, slots=96)
        )
    final_start = start_at + timedelta(minutes=75 * (ticks - 1))
    cold_values = await _provider().async_values(
        _window(final_start, slot_minutes=15, slots=96)
    )

    # The warm period covers a full day, so a full-day forecast exercises every
    # time-of-day slot that the repeated boundary rows touched.
    assert cold_values == pytest.approx([0.25] * 96)
    assert warm_values == pytest.approx(cold_values)


async def test_forecast_skips_first_segment_after_cold_fetch(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The start-time row of a cold fetch must not shorten the first segment."""
    hass.config.components.add("recorder")
    hass.states.async_set(_ENTITY_ID, "100.0", _VALID_LOAD_ATTRS)
    recorder = _BoundaryRowRecorder(_ENTITY_ID, _steady_hourly_meter())
    monkeypatch.setattr(cache_module, "get_instance", lambda _hass: recorder)

    provider = ForecastProvider(
        hass,
        entity_id=_ENTITY_ID,
        lookback_days=2,
        same_weekday_weight=1.0,
        other_weekday_weight=1.0,
        recency_decay=0.0,
    )
    # The lookback starts at hh:45, so the first real meter change is 15 minutes
    # in and the interval before it began at an unknown time.
    values = await provider.async_values(
        _window(datetime(2026, 1, 3, 0, 45, tzinfo=UTC), slot_minutes=15, slots=96)
    )

    assert values == pytest.approx([0.25] * 96)


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
        states.append(_row(at, total))
    return states


async def test_forecast_load_pattern_follows_local_time_across_dst(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """History from before a DST change must land on the same local hour."""
    await hass.config.async_set_time_zone("Europe/Copenhagen")
    # Peak usage at 08:00 local. Before the change on 2026-10-25 that is
    # 06:00 UTC (CEST); afterwards it is 07:00 UTC (CET).
    provider, _recorder = _provider_with_history(
        hass,
        monkeypatch,
        _hourly_meter_states(
            datetime(2026, 10, 20, 0, 0, tzinfo=UTC), 48, spike_hour_utc=6
        ),
        lookback_days=14,
    )

    values = await provider.async_values(
        _window(datetime(2026, 10, 27, 7, 0, tzinfo=UTC), slots=2)  # 08:00 CET
    )

    assert values == pytest.approx([5.0, 1.0])


async def test_forecast_reuses_load_pattern_within_the_hour_and_rebuilds_on_new_day(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The load pattern is rebuilt hourly, on slot changes, diagnostics and new days."""
    await hass.config.async_set_time_zone("Europe/Copenhagen")
    hass.config.components.add("recorder")
    hass.states.async_set(_ENTITY_ID, "100.0", _VALID_LOAD_ATTRS)
    recorder = _BoundaryRowRecorder(_ENTITY_ID, _steady_hourly_meter())
    monkeypatch.setattr(cache_module, "get_instance", lambda _hass: recorder)
    provider = ForecastProvider(hass, entity_id=_ENTITY_ID, lookback_days=2)
    start_at = datetime(2026, 1, 3, 10, 0, tzinfo=UTC)

    def _window_at(offset_minutes: int, slot_minutes: int = 60) -> SourceWindow:
        return _window(
            start_at + timedelta(minutes=offset_minutes), slot_minutes=slot_minutes, slots=3
        )

    first = await provider.async_values(_window_at(0))
    assert first == pytest.approx([1.0] * 3)
    assert recorder.calls == 1

    # Within the hour: no recorder query, same pattern.
    assert await provider.async_values(_window_at(15)) == pytest.approx(first)
    assert await provider.async_values(_window_at(59)) == pytest.approx(first)
    assert recorder.calls == 1

    # Exactly one hour later the pattern is rebuilt.
    await provider.async_values(_window_at(60))
    assert recorder.calls == 2

    # A different slot size cannot use the stored pattern.
    assert await provider.async_values(_window_at(75, 30)) == pytest.approx([0.5] * 3)
    assert recorder.calls == 3

    # Diagnostics always query recorder history.
    await provider.async_debug_payload(_window_at(80, 30))
    assert recorder.calls == 4

    # Observation ages are per local day, so a new local day needs a new pattern.
    # 22:30 UTC is 23:30 in Copenhagen; 23:15 UTC is 00:15 the next local day.
    await provider.async_values(_window(datetime(2026, 1, 3, 22, 30, tzinfo=UTC)))
    assert recorder.calls == 5
    await provider.async_values(_window(datetime(2026, 1, 3, 23, 15, tzinfo=UTC)))
    assert recorder.calls == 6
