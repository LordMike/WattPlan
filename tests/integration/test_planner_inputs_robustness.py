"""Robustness tests for planner inputs: failing services, comfort states, targets."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.const import CONF_NAME, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse

from custom_components.wattplan.const import (
    ADAPTER_TYPE_SERVICE_RESPONSE,
    CONF_ADAPTER_TYPE,
    CONF_SERVICE,
    CONF_SOURCE_IMPORT_PRICE,
    CONF_SOURCE_MODE,
    CONF_SOURCE_PV,
    SOURCE_MODE_SERVICE_ADAPTER,
)
from custom_components.wattplan.coordinator import (
    PlanningStageError,
    snapshot_storage_key,
)
from custom_components.wattplan.coordinator_parts import StageErrorKind
from custom_components.wattplan.optimizer.planners.presets import run_preset
from custom_components.wattplan.historical_on_off_provider import (
    HistoricalOnOffProvider,
)
from custom_components.wattplan.source_providers.payloads import (
    async_service_response,
    split_service_name,
)
from custom_components.wattplan.source_types import SourceProviderError

from .test_integration_e2e import (
    _base_sources,
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


def _missing_service_source(service: str = "nowhere.prices") -> dict[str, Any]:
    return {
        CONF_SOURCE_MODE: SOURCE_MODE_SERVICE_ADAPTER,
        CONF_SERVICE: service,
        CONF_ADAPTER_TYPE: ADAPTER_TYPE_SERVICE_RESPONSE,
        CONF_NAME: "prices",
        "time_key": "start",
        "value_key": "price",
    }


@pytest.mark.parametrize("name", ["", "nodot", ".service", "domain.", " . "])
def test_split_service_name_rejects_malformed_names(name: str) -> None:
    with pytest.raises(SourceProviderError) as err:
        split_service_name(name, label="Service")
    assert err.value.code == "source_validation"


def test_split_service_name_splits_domain_and_service() -> None:
    assert split_service_name("test.prices", label="Service") == ("test", "prices")


async def test_async_service_response_converts_missing_service(
    hass: HomeAssistant,
) -> None:
    with pytest.raises(SourceProviderError) as err:
        await async_service_response(hass, "nowhere.prices")
    assert err.value.code == "source_fetch"


async def test_async_service_response_converts_timeout(hass: HomeAssistant) -> None:
    async def slow(_call: ServiceCall) -> dict[str, Any]:
        await asyncio.sleep(5)
        return {}

    hass.services.async_register(
        "test", "slow", slow, supports_response=SupportsResponse.ONLY
    )

    with (
        patch(
            "custom_components.wattplan.source_providers.payloads."
            "SERVICE_CALL_TIMEOUT_SECONDS",
            0.05,
        ),
        pytest.raises(SourceProviderError) as err,
    ):
        await async_service_response(hass, "test.slow")
    assert err.value.code == "source_fetch"


async def test_missing_service_for_required_source_fails_plan_with_status(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,  # noqa: F811
) -> None:
    sources = _base_sources()
    sources[CONF_SOURCE_IMPORT_PRICE] = _missing_service_source()
    entry = _entry(
        title="Home",
        subentries_data=[_battery_subentry(subentry_id="battery", name="battery")],
        sources=sources,
    )
    await _setup_entry(hass, entry)

    with (
        patch(
            "custom_components.wattplan.optimizer.optimize",
            side_effect=_fake_optimize_with_entities,
        ),
        pytest.raises(PlanningStageError) as err,
    ):
        await _run_optimize(hass)

    assert err.value.kind is StageErrorKind.SOURCE_FETCH
    coordinator = entry.runtime_data.coordinator
    assert coordinator._plan_error.has_error
    assert coordinator._plan_error.kind is StageErrorKind.SOURCE_FETCH
    assert hass.states.get("sensor.home_status").state == "failed"
    assert hass.states.get("sensor.home_import_price_status").state == "failed"
    assert hass.states.get("sensor.home_battery_action").state == STATE_UNAVAILABLE


async def test_missing_service_for_pv_source_degrades_plan(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,  # noqa: F811
) -> None:
    sources = _base_sources()
    sources[CONF_SOURCE_PV] = _missing_service_source()
    entry = _entry(
        title="Home",
        subentries_data=[_battery_subentry(subentry_id="battery", name="battery")],
        sources=sources,
    )
    await _setup_entry(hass, entry)

    with patch(
        "custom_components.wattplan.optimizer.optimize",
        side_effect=_fake_optimize_with_entities,
    ):
        await _run_optimize(hass)

    assert hass.states.get("sensor.home_status").state == "degraded"
    assert hass.states.get("sensor.home_pv_status").state == "degraded"
    assert entry.runtime_data.coordinator.data is not None


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


@pytest.mark.parametrize("state", [STATE_UNAVAILABLE, STATE_UNKNOWN])
async def test_unavailable_comfort_state_is_unknown_history_not_off(
    hass: HomeAssistant, freezer: Any, state: str
) -> None:
    hass.states.async_set("binary_sensor.heating", state)
    # An old state would give a naive OFF streak of 24 slots.
    freezer.tick(timedelta(hours=6))
    runtime = await HistoricalOnOffProvider(
        hass, "binary_sensor.heating"
    ).async_runtime_state(rolling_window_slots=4, slot_minutes=15)

    assert runtime == (False, None, 0, 0)


async def test_unavailable_comfort_entity_sends_no_off_streak_to_the_planner(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,  # noqa: F811
) -> None:
    entry = _entry(
        title="Home",
        subentries_data=[
            _battery_subentry(subentry_id="battery", name="battery"),
            _comfort_subentry(subentry_id="comfort", name="comfort"),
        ],
    )
    await _setup_entry(hass, entry)
    hass.states.async_set("binary_sensor.comfort_on_off", STATE_UNAVAILABLE)
    captured: list[Any] = []

    def capture(params: Any) -> dict[str, object]:
        captured.append(params)
        return _fake_optimize_with_entities(params)

    with patch("custom_components.wattplan.optimizer.optimize", side_effect=capture):
        await _run_optimize(hass)

    comfort = captured[-1].comfort_entities[0]
    assert comfort.on_history is None
    assert comfort.off_streak_slots_now == 0
    assert comfort.is_on_now is False
