"""Planning timeout tests: the optimizer deadline and recovery afterwards."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from unittest.mock import patch

from custom_components.wattplan.coordinator import (
    PlanningStageError,
    snapshot_storage_key,
)
from custom_components.wattplan.coordinator_parts import StageErrorKind
from custom_components.wattplan.optimizer.planners.presets import run_preset
import pytest

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant

from .test_integration_e2e import (
    _battery_subentry,
    _comfort_subentry,
    _entry,
    _fake_optimize_with_entities,
    _optional_subentry,
    _run_optimize,
    _setup_entry,
    entity_registry_enabled_by_default,  # noqa: F401
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


async def test_optimizer_timeout_aborts_work_and_allows_retry(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,  # noqa: F811
) -> None:
    entry = _entry(
        title="Home",
        subentries_data=[_battery_subentry(subentry_id="battery", name="battery")],
    )
    await _setup_entry(hass, entry)
    coordinator = entry.runtime_data.coordinator

    original = hass.async_add_executor_job
    release = asyncio.Event()
    completed = asyncio.Event()
    optimizer_calls = 0

    async def hang_on_optimizer(target: Any, *args: Any) -> Any:
        nonlocal optimizer_calls
        # The test hass runs executor jobs inline, so simulate a stuck solve.
        if target is run_preset:
            optimizer_calls += 1
        try:
            if target is run_preset and optimizer_calls == 1:
                await release.wait()
            return await original(target, *args)
        finally:
            if target is run_preset:
                completed.set()

    with (
        patch.object(coordinator, "_optimizer_timeout_seconds", return_value=0.05),
        patch.object(hass, "async_add_executor_job", hang_on_optimizer),
        patch("custom_components.wattplan.optimizer.optimize", side_effect=_fake_optimize_with_entities),
    ):
        try:
            with pytest.raises(PlanningStageError) as err:
                await _run_optimize(hass)
            assert err.value.kind is StageErrorKind.PLANNER_TIMEOUT
            assert "timed out" in str(err.value)
            assert coordinator._plan_error.kind is StageErrorKind.PLANNER_TIMEOUT
            assert hass.states.get("sensor.home_status").state == "failed"
            assert coordinator.snapshot is None
            assert completed.is_set()
            assert optimizer_calls == 1
            await hass.async_block_till_done()
            assert entry.runtime_data.optimizer_state is None
            # The timed-out call is gone: a fresh attempt runs immediately.
            await _run_optimize(hass)
            assert optimizer_calls == 2
            assert coordinator.has_usable_plan
            assert not coordinator._plan_error.has_error
        finally:
            release.set()
            await completed.wait()


@pytest.mark.parametrize("previous_plan", [False, True])
async def test_180_second_timeout_invalidates_plan_and_preserves_diagnostics(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    freezer: Any,
    previous_plan: bool,
    entity_registry_enabled_by_default: None,  # noqa: F811
) -> None:
    """A timeout exposes elapsed time and recovery guidance without old actions."""
    entry = _entry(
        title="Home",
        subentries_data=[
            _battery_subentry(subentry_id="battery", name="battery"),
            _comfort_subentry(subentry_id="comfort", name="comfort"),
            _optional_subentry(subentry_id="optional", name="optional"),
        ],
    )
    with patch(
        "custom_components.wattplan.optimizer.optimize",
        side_effect=(
            _fake_optimize_with_entities
            if previous_plan else RuntimeError("No initial plan")
        ),
    ):
        await _setup_entry(hass, entry)

    coordinator = entry.runtime_data.coordinator
    storage_key = snapshot_storage_key(entry.entry_id)
    assert (storage_key in hass_storage) is previous_plan
    for slot_minutes in (15, 30, 60):
        assert coordinator._optimizer_timeout_seconds(slot_minutes) == 180

    async def timeout_after_deadline(*_args: Any) -> None:
        # Simulate the elapsed deadline; cancellation itself is tested separately.
        freezer.tick(timedelta(seconds=180))
        raise TimeoutError

    with (
        patch(
            "custom_components.wattplan.coordinator.async_run_preset",
            side_effect=timeout_after_deadline,
        ),
        pytest.raises(PlanningStageError) as err,
    ):
        await _run_optimize(hass)
    await hass.async_block_till_done()

    assert err.value.kind is StageErrorKind.PLANNER_TIMEOUT
    assert err.value.details["timeout_seconds"] == 180
    assert coordinator.snapshot is None
    assert coordinator.data is None
    assert coordinator.has_usable_plan is False
    assert coordinator.action_recommendations_validated is False
    assert entry.runtime_data.optimizer_state is None
    assert storage_key not in hass_storage
    assert await coordinator.async_restore_snapshot() is False

    plan_entities = (
        "sensor.home_battery_action",
        "sensor.home_battery_next_action",
        "sensor.home_comfort_action",
        "sensor.home_comfort_next_action",
        "sensor.home_optional_next_start_option",
        "sensor.home_optional_option_1_start",
        "sensor.home_plan_details",
        "sensor.home_plan_details_hourly",
        "sensor.home_plan_outlook_en",
    )
    for entity_id in plan_entities:
        state = hass.states.get(entity_id)
        assert state is not None, entity_id
        assert state.state == STATE_UNAVAILABLE, entity_id

    message = "Planner timed out after 180 seconds. Pick another planner."
    status = hass.states.get("sensor.home_status")
    assert status.state == "failed"
    assert status.attributes["reason_summary"] == message
    assert status.attributes["has_usable_plan"] is False
    assert hass.states.get("sensor.home_status_message").state == message
    duration = hass.states.get("sensor.home_last_run_duration")
    assert float(duration.state) == 180_000
    assert ("Optimizer process (timed out)", 180_000) in duration.attributes["timings"]
    assert duration.attributes["timings"][-1] == ("total", 180_000)

    with patch(
        "custom_components.wattplan.optimizer.optimize",
        side_effect=_fake_optimize_with_entities,
    ):
        await _run_optimize(hass)
    assert coordinator.has_usable_plan
    assert coordinator.action_recommendations_validated
    assert not coordinator._plan_error.has_error
    assert storage_key in hass_storage
    for entity_id in plan_entities:
        assert hass.states.get(entity_id).state != STATE_UNAVAILABLE, entity_id
