"""Tests for historical on/off provider behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import custom_components.wattplan.historical_on_off_provider as provider_module
import custom_components.wattplan.rolling_history_cache as cache_module
from custom_components.wattplan.historical_on_off_provider import HistoricalOnOffProvider
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

    is_on_now, on_history, on_slots, off_streak_slots = await provider.async_runtime_state(
        rolling_window_slots=4, slot_minutes=15
    )

    assert is_on_now is True
    assert on_history is None
    assert on_slots == 0
    assert off_streak_slots == 0


@pytest.mark.parametrize("minute", [0, 10])
async def test_provider_uses_history_to_compute_on_slots(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, minute: int
) -> None:
    """Use recorder data to compute on-time over the rolling window."""
    monkeypatch.setattr(provider_module, "datetime", _TestDatetime)
    _TestDatetime._now = datetime(2026, 1, 1, 1, minute, tzinfo=UTC)
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
    monkeypatch.setattr(cache_module, "get_instance", lambda _hass: recorder)

    provider = HistoricalOnOffProvider(hass, entity_id)
    is_on_now, on_history, on_slots, _off_streak_slots = await provider.async_runtime_state(
        rolling_window_slots=4, slot_minutes=15
    )

    assert is_on_now is True
    assert on_history == [True, True, False]
    assert on_slots == 2
    assert recorder.fetch_starts == [datetime(2026, 1, 1, 0, 15, tzinfo=UTC)]


async def test_provider_preserves_history_order_with_identical_on_counts(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return slot order, not only an aggregate ON count."""
    monkeypatch.setattr(provider_module, "datetime", _TestDatetime)
    _TestDatetime._now = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)
    hass.config.components.add("recorder")
    entity_id = "binary_sensor.heating_order"
    states = [("on", 15), ("off", 30), ("on", 45)]
    hass.states.async_set(entity_id, states[-1][0])
    recorder = _FakeRecorder(
        responses=[
            {
                entity_id: [
                    SimpleNamespace(
                        state=state,
                        last_changed=datetime(2026, 1, 1, 0, minute, tzinfo=UTC),
                    )
                    for state, minute in states
                ]
            }
        ]
    )
    monkeypatch.setattr(cache_module, "get_instance", lambda _hass: recorder)

    provider = HistoricalOnOffProvider(hass, entity_id)
    _is_on, on_history, on_slots, _off_streak = await provider.async_runtime_state(
        rolling_window_slots=4, slot_minutes=15
    )

    assert on_history == [True, False, True]
    assert on_slots == 2


async def test_provider_aligns_history_to_explicit_forecast_boundary(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the request boundary when source fetching crosses a slot boundary."""
    monkeypatch.setattr(provider_module, "datetime", _TestDatetime)
    _TestDatetime._now = datetime(2026, 1, 1, 1, 40, tzinfo=UTC)
    hass.config.components.add("recorder")
    entity_id = "binary_sensor.heating_boundary"
    hass.states.async_set(entity_id, "on")
    recorder = _FakeRecorder(
        responses=[
            {
                entity_id: [
                    SimpleNamespace(
                        state="off",
                        last_changed=datetime(2026, 1, 1, 0, 15, tzinfo=UTC),
                    ),
                    SimpleNamespace(
                        state="on",
                        last_changed=datetime(2026, 1, 1, 0, 45, tzinfo=UTC),
                    ),
                    SimpleNamespace(
                        state="off",
                        last_changed=datetime(2026, 1, 1, 1, 0, tzinfo=UTC),
                    ),
                ]
            }
        ]
    )
    monkeypatch.setattr(cache_module, "get_instance", lambda _hass: recorder)

    provider = HistoricalOnOffProvider(hass, entity_id)
    _is_on, on_history, on_slots, _off_streak = await provider.async_runtime_state(
        rolling_window_slots=4,
        slot_minutes=15,
        forecast_start=datetime(2026, 1, 1, 1, 15, tzinfo=UTC),
    )

    assert recorder.fetch_starts == [datetime(2026, 1, 1, 0, 30, tzinfo=UTC)]
    assert on_history == [False, True, False]
    assert on_slots == 1


@pytest.mark.parametrize(("off_minutes", "expected_streak"), [(44, 2), (45, 3)])
async def test_provider_counts_whole_slots_since_load_turned_off(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    off_minutes: int,
    expected_streak: int,
) -> None:
    """The OFF streak is the number of whole slots since the load turned off."""
    monkeypatch.setattr(provider_module, "datetime", _TestDatetime)
    hass.states.async_set("binary_sensor.heating", "off")
    turned_off_at = hass.states.get("binary_sensor.heating").last_changed
    _TestDatetime._now = turned_off_at + timedelta(minutes=off_minutes)
    provider = HistoricalOnOffProvider(hass, "binary_sensor.heating")

    is_on_now, on_history, _on_slots, off_streak_slots = await provider.async_runtime_state(
        rolling_window_slots=4, slot_minutes=15
    )

    assert is_on_now is False
    assert on_history is None
    assert off_streak_slots == expected_streak


async def test_provider_keeps_boundary_sample_when_cache_window_moves(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later query still knows the state at its window start from the cache."""
    monkeypatch.setattr(provider_module, "datetime", _TestDatetime)
    hass.config.components.add("recorder")
    entity_id = "binary_sensor.heating_cache"
    hass.states.async_set(entity_id, "off")
    recorder = _FakeRecorder(
        responses=[
            {
                entity_id: [
                    SimpleNamespace(
                        state="off", last_changed=datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
                    ),
                    SimpleNamespace(
                        state="on", last_changed=datetime(2026, 1, 1, 0, 10, tzinfo=UTC)
                    ),
                    SimpleNamespace(
                        state="off", last_changed=datetime(2026, 1, 1, 0, 40, tzinfo=UTC)
                    ),
                ]
            },
            {entity_id: []},
        ]
    )
    monkeypatch.setattr(cache_module, "get_instance", lambda _hass: recorder)
    provider = HistoricalOnOffProvider(hass, entity_id)

    _TestDatetime._now = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)
    _is_on, first_history, _on_slots, _off_streak = await provider.async_runtime_state(
        rolling_window_slots=5, slot_minutes=15
    )
    # The next query only fetches since the cache end and drops pre-window rows,
    # but must keep the 00:10 ON row that was in effect at the new window start.
    _TestDatetime._now = datetime(2026, 1, 1, 1, 15, tzinfo=UTC)
    _is_on, second_history, _on_slots, _off_streak = await provider.async_runtime_state(
        rolling_window_slots=5, slot_minutes=15
    )

    assert first_history == [False, True, True, False]
    assert second_history == [True, True, False, False]
    assert recorder.fetch_starts == [
        datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2026, 1, 1, 1, 0, tzinfo=UTC),
    ]
