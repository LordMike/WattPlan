"""Tests for WattPlan forecast provider behavior."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import custom_components.wattplan.forecast_provider as provider_module
from custom_components.wattplan.forecast_provider import ForecastProvider
from custom_components.wattplan.source_provider import SourceProviderError, SourceWindow
import pytest

from homeassistant.core import HomeAssistant

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

_VALID_LOAD_ATTRS = {
    "state_class": "measurement",
    "device_class": "energy",
    "unit_of_measurement": "kWh",
}


class _FakeRecorder:
    """Recorder stub with one queued history response."""

    def __init__(
        self,
        history_response: dict[str, list[Any]],
        statistics_response: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        """Initialize fake recorder response."""
        self._history_response = history_response
        self._statistics_response = statistics_response or {}
        self.calls = 0

    async def async_add_executor_job(self, _job: Any) -> dict[str, list[Any]]:
        """Return queued recorder response."""
        self.calls += 1
        if self.calls == 1:
            return self._history_response
        return self._statistics_response


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
