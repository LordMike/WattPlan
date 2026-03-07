"""Tests for historical on/off provider behavior."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import custom_components.wattplan.historical_on_off_provider as provider_module
from custom_components.wattplan.historical_on_off_provider import (
    HistoricalOnOffProvider,
    OnOffSample,
)
import pytest

from homeassistant.core import HomeAssistant

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


class _FakeRecorder:
    """Recorder stub with queued history responses."""

    def __init__(self, responses: list[dict[str, list[Any]]]) -> None:
        """Initialize fake recorder."""
        self._responses = responses
        self.fetch_starts: list[datetime] = []

    async def async_add_executor_job(self, job: Any) -> dict[str, list[Any]]:
        """Return queued response and track requested fetch start."""
        self.fetch_starts.append(job.args[1])
        return self._responses.pop(0)


class _TestDatetime(datetime):
    """Datetime shim with mutable current time for provider tests."""

    _now: datetime = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)

    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        """Return mutable test now."""
        if tz is None:
            return cls._now.replace(tzinfo=None)
        return cls._now.astimezone(tz)


async def test_provider_without_recorder_uses_current_state(
    hass: HomeAssistant,
) -> None:
    """Return immediate state data without querying recorder."""
    hass.states.async_set("binary_sensor.heating", "on")
    provider = HistoricalOnOffProvider(hass, "binary_sensor.heating")

    is_on_now, on_slots, off_streak_slots = await provider.async_runtime_state(
        rolling_window_slots=4, slot_minutes=15
    )

    assert is_on_now is True
    assert on_slots == 0
    assert off_streak_slots == 0


async def test_provider_uses_history_to_compute_on_slots(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Use recorder data to compute on-time over the rolling window."""
    monkeypatch.setattr(provider_module, "datetime", _TestDatetime)
    _TestDatetime._now = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)
    hass.config.components.add("recorder")
    hass.states.async_set("binary_sensor.heating", "on")

    entity_id = "binary_sensor.heating"
    recorder = _FakeRecorder(
        responses=[
            {
                entity_id: [
                    SimpleNamespace(
                        state="off",
                        last_changed=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
                    ),
                    SimpleNamespace(
                        state="on",
                        last_changed=datetime(2026, 1, 1, 0, 15, tzinfo=UTC),
                    ),
                    SimpleNamespace(
                        state="off",
                        last_changed=datetime(2026, 1, 1, 0, 45, tzinfo=UTC),
                    ),
                ]
            }
        ]
    )
    monkeypatch.setattr(provider_module, "get_instance", lambda _hass: recorder)

    provider = HistoricalOnOffProvider(hass, entity_id)
    is_on_now, on_slots, _off_streak_slots = await provider.async_runtime_state(
        rolling_window_slots=4, slot_minutes=15
    )

    assert is_on_now is True
    assert on_slots == 2
    assert recorder.fetch_starts == [datetime(2026, 1, 1, 0, 0, tzinfo=UTC)]


async def test_provider_fetches_incrementally_from_cache_end(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second query should only fetch the delta from cache end."""
    monkeypatch.setattr(provider_module, "datetime", _TestDatetime)
    hass.config.components.add("recorder")
    hass.states.async_set("binary_sensor.heating", "on")

    entity_id = "binary_sensor.heating"
    recorder = _FakeRecorder(
        responses=[
            {
                entity_id: [
                    SimpleNamespace(
                        state="off",
                        last_changed=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
                    ),
                    SimpleNamespace(
                        state="on",
                        last_changed=datetime(2026, 1, 1, 0, 30, tzinfo=UTC),
                    ),
                ]
            },
            {entity_id: []},
        ]
    )
    monkeypatch.setattr(provider_module, "get_instance", lambda _hass: recorder)

    provider = HistoricalOnOffProvider(hass, entity_id)

    _TestDatetime._now = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)
    await provider.async_runtime_state(rolling_window_slots=4, slot_minutes=15)

    _TestDatetime._now = datetime(2026, 1, 1, 1, 15, tzinfo=UTC)
    await provider.async_runtime_state(rolling_window_slots=4, slot_minutes=15)

    assert recorder.fetch_starts == [
        datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 1, 0, tzinfo=UTC),
    ]


def test_prune_cache_keeps_boundary_sample_and_newer() -> None:
    """Prune should retain last pre-window sample and all newer samples."""
    provider = HistoricalOnOffProvider(hass=SimpleNamespace(), entity_id="binary_sensor.x")
    provider._samples = [
        OnOffSample(datetime(2026, 1, 1, 0, 0, tzinfo=UTC), is_on=False),
        OnOffSample(datetime(2026, 1, 1, 0, 5, tzinfo=UTC), is_on=True),
        OnOffSample(datetime(2026, 1, 1, 0, 10, tzinfo=UTC), is_on=False),
        OnOffSample(datetime(2026, 1, 1, 0, 20, tzinfo=UTC), is_on=True),
    ]

    provider._prune_cache(window_start=datetime(2026, 1, 1, 0, 12, tzinfo=UTC))

    assert provider._samples == [
        OnOffSample(datetime(2026, 1, 1, 0, 10, tzinfo=UTC), is_on=False),
        OnOffSample(datetime(2026, 1, 1, 0, 20, tzinfo=UTC), is_on=True),
    ]
