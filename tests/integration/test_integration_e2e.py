"""Focused end-to-end integration tests for WattPlan runtime logic."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import patch

from custom_components.wattplan.const import (
    CONF_ACTION_EMISSION_ENABLED,
    CONF_CAN_CHARGE_FROM_GRID,
    CONF_CAN_CHARGE_FROM_PV,
    CONF_CAPACITY_KWH,
    CONF_CHARGE_EFFICIENCY,
    CONF_DISCHARGE_EFFICIENCY,
    CONF_DURATION_MINUTES,
    CONF_ENERGY_KWH,
    CONF_EXPECTED_POWER_KW,
    CONF_HOURS_TO_PLAN,
    CONF_MAX_CHARGE_KW,
    CONF_MAX_CONSECUTIVE_OFF_MINUTES,
    CONF_MAX_DISCHARGE_KW,
    CONF_MIN_CONSECUTIVE_OFF_MINUTES,
    CONF_MIN_CONSECUTIVE_ON_MINUTES,
    CONF_MIN_OPTION_GAP_MINUTES,
    CONF_MINIMUM_KWH,
    CONF_ON_OFF_SOURCE,
    CONF_OPTIONS_COUNT,
    CONF_PLANNING_ENABLED,
    CONF_ROLLING_WINDOW_HOURS,
    CONF_RUN_WITHIN_HOURS,
    CONF_SLOT_MINUTES,
    CONF_SOC_SOURCE,
    CONF_SOURCE_MODE,
    CONF_SOURCE_IMPORT_PRICE,
    CONF_SOURCE_PV,
    CONF_SOURCE_USAGE,
    CONF_SOURCES,
    CONF_TARGET_ON_HOURS_PER_WINDOW,
    CONF_TEMPLATE,
    DOMAIN,
    SERVICE_RUN_OPTIMIZE_NOW,
    SERVICE_RUN_PLAN_NOW,
    SOURCE_MODE_TEMPLATE,
    SUBENTRY_TYPE_BATTERY,
    SUBENTRY_TYPE_COMFORT,
    SUBENTRY_TYPE_OPTIONAL,
)
from custom_components.wattplan.coordinator import PlanningStageError
import pytest

from homeassistant import config_entries
from homeassistant.const import CONF_NAME, STATE_OFF, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util

from tests.common import MockConfigEntry, async_fire_time_changed

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture
def entity_registry_enabled_by_default() -> None:
    """Ensure entities disabled by default are enabled in these tests."""
    with (
        patch(
            "homeassistant.helpers.entity.Entity.entity_registry_enabled_default",
            return_value=True,
        ),
        patch(
            "homeassistant.components.device_tracker.config_entry.ScannerEntity.entity_registry_enabled_default",
            return_value=True,
        ),
    ):
        yield


def _name_of(model: Any) -> str:
    """Return name value from optimizer request models."""
    if hasattr(model, "name"):
        return str(model.name)
    return str(model["name"])


def _fake_optimize(_params: object) -> dict[str, object]:
    """Return minimal successful optimizer output."""
    return {
        "execution_time": 0.01,
        "fitness": 1.0,
        "avg_price": 0.2,
        "suboptimal": False,
        "suboptimal_reasons": [],
        "problems": [],
        "successful_solves": 1,
        "reused_steps": 0,
        "entities": [],
        "optional_entity_options": [],
        "state": "state-token",
    }


def _fake_optimize_with_entities(params: Any) -> dict[str, object]:
    """Return optimizer output that includes every configured subentry."""
    battery_entities = params.battery_entities
    comfort_entities = params.comfort_entities
    optional_entities = params.optional_entities

    if battery_entities:
        assert battery_entities[0].charge_efficiency == pytest.approx(0.9)
        assert battery_entities[0].discharge_efficiency == pytest.approx(0.9)

    battery_results = [
        {
            "name": _name_of(battery),
            "type": "battery",
            "schedule": [
                {"state": "charge", "charge_source": 1, "level": 5.0},
                {"state": "hold", "charge_source": 0, "level": 5.0},
            ],
        }
        for battery in battery_entities
    ]
    comfort_results = [
        {
            "name": _name_of(comfort),
            "type": "comfort",
            "schedule": [
                {"enabled": True, "level": 1.0},
                {"enabled": False, "level": 0.8},
            ],
        }
        for comfort in comfort_entities
    ]

    optional_results = [
        {
            "name": _name_of(optional),
            "options": [
                {
                    "start_timeslot": 1,
                    "end_timeslot": 2,
                    "incremental_cost": 0.1,
                    "delta_from_best": 0.0,
                }
            ],
        }
        for optional in optional_entities
    ]

    result = _fake_optimize(params)
    result["entities"] = [*battery_results, *comfort_results]
    result["optional_entity_options"] = optional_results
    return result


def _base_sources() -> dict[str, dict[str, Any]]:
    """Return valid source config with one template per source."""
    return {
        CONF_SOURCE_IMPORT_PRICE: {
            CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
            CONF_TEMPLATE: "{{ [0.2, 0.25, 0.3, 0.35] }}",
        },
        CONF_SOURCE_USAGE: {
            CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
            CONF_TEMPLATE: "{{ [1.0, 1.0, 1.0, 1.0] }}",
        },
        CONF_SOURCE_PV: {
            CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
            CONF_TEMPLATE: "{{ [0.0, 0.1, 0.2, 0.0] }}",
        },
    }


def _battery_subentry(*, subentry_id: str, name: str) -> config_entries.ConfigSubentryData:
    """Return battery subentry config."""
    return config_entries.ConfigSubentryData(
        subentry_id=subentry_id,
        subentry_type=SUBENTRY_TYPE_BATTERY,
        title=name,
        unique_id=f"battery:{subentry_id}",
        data={
            CONF_NAME: name,
            CONF_SOC_SOURCE: "sensor.battery_soc",
            CONF_CAPACITY_KWH: 10.0,
            CONF_MINIMUM_KWH: 1.0,
            CONF_MAX_CHARGE_KW: 3.0,
            CONF_MAX_DISCHARGE_KW: 3.0,
            CONF_CHARGE_EFFICIENCY: 0.9,
            CONF_DISCHARGE_EFFICIENCY: 0.9,
            CONF_CAN_CHARGE_FROM_GRID: True,
            CONF_CAN_CHARGE_FROM_PV: True,
        },
    )


def _comfort_subentry(*, subentry_id: str, name: str) -> config_entries.ConfigSubentryData:
    """Return comfort subentry config."""
    return config_entries.ConfigSubentryData(
        subentry_id=subentry_id,
        subentry_type=SUBENTRY_TYPE_COMFORT,
        title=name,
        unique_id=f"comfort:{subentry_id}",
        data={
            CONF_NAME: name,
            CONF_ROLLING_WINDOW_HOURS: 4,
            CONF_TARGET_ON_HOURS_PER_WINDOW: 1,
            CONF_MIN_CONSECUTIVE_ON_MINUTES: 60,
            CONF_MIN_CONSECUTIVE_OFF_MINUTES: 60,
            CONF_MAX_CONSECUTIVE_OFF_MINUTES: 120,
            CONF_ON_OFF_SOURCE: "binary_sensor.comfort_on_off",
            CONF_EXPECTED_POWER_KW: 1.2,
        },
    )


def _optional_subentry(*, subentry_id: str, name: str) -> config_entries.ConfigSubentryData:
    """Return optional subentry config."""
    return config_entries.ConfigSubentryData(
        subentry_id=subentry_id,
        subentry_type=SUBENTRY_TYPE_OPTIONAL,
        title=name,
        unique_id=f"optional:{subentry_id}",
        data={
            CONF_NAME: name,
            CONF_DURATION_MINUTES: 60,
            CONF_RUN_WITHIN_HOURS: 3,
            CONF_ENERGY_KWH: 1.2,
            CONF_OPTIONS_COUNT: 1,
            CONF_MIN_OPTION_GAP_MINUTES: 30,
        },
    )


def _entry(
    *,
    title: str,
    subentries_data: list[config_entries.ConfigSubentryData],
    sources: dict[str, dict[str, Any]] | None = None,
    options: dict[str, Any] | None = None,
) -> MockConfigEntry:
    """Build a mock WattPlan config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=title,
        data={
            CONF_NAME: title,
            CONF_SLOT_MINUTES: 60,
            CONF_HOURS_TO_PLAN: 4,
            CONF_SOURCES: sources or _base_sources(),
        },
        options=options
        or {
            CONF_PLANNING_ENABLED: False,
            CONF_ACTION_EMISSION_ENABLED: False,
        },
        subentries_data=subentries_data,
    )


async def _setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Set up one WattPlan entry with required state entities."""
    entry.add_to_hass(hass)
    hass.states.async_set("sensor.battery_soc", "5.0")
    hass.states.async_set("binary_sensor.comfort_on_off", STATE_OFF)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _run_optimize(
    hass: HomeAssistant,
    *,
    name: str | None = None,
    entry_id: str | None = None,
) -> None:
    """Call run_optimize_now with optional filters."""
    payload: dict[str, Any] = {}
    if name is not None:
        payload[CONF_NAME] = name
    if entry_id is not None:
        payload["entry_id"] = entry_id
    await hass.services.async_call(
        DOMAIN, SERVICE_RUN_OPTIMIZE_NOW, payload, blocking=True
    )


async def _run_emit(
    hass: HomeAssistant, *, name: str | None = None, entry_id: str | None = None
) -> None:
    """Call run_plan_now with optional filters."""
    payload: dict[str, Any] = {}
    if name is not None:
        payload[CONF_NAME] = name
    if entry_id is not None:
        payload["entry_id"] = entry_id
    await hass.services.async_call(DOMAIN, SERVICE_RUN_PLAN_NOW, payload, blocking=True)


async def test_run_services_are_isolated_by_name(hass: HomeAssistant) -> None:
    """Only the selected entry should run when name is provided."""
    # Purpose: verify multi-setup isolation so one service call cannot
    # accidentally update another home's entities.
    alpha = _entry(
        title="Alpha",
        subentries_data=[_battery_subentry(subentry_id="b1", name="batt")],
    )
    beta = _entry(
        title="Beta",
        subentries_data=[_battery_subentry(subentry_id="b1", name="batt")],
    )
    await _setup_entry(hass, alpha)
    await _setup_entry(hass, beta)
    alpha_before = alpha.runtime_data.coordinator.last_attempt_at
    beta_before = beta.runtime_data.coordinator.last_attempt_at
    assert alpha_before is not None
    assert beta_before is not None

    with patch(
        "custom_components.wattplan.coordinator.optimize",
        side_effect=_fake_optimize_with_entities,
    ):
        await _run_optimize(hass, name="Alpha")
        await _run_emit(hass, name="Alpha")

    assert hass.states.get("sensor.alpha_status") is not None
    assert hass.states.get("sensor.alpha_status").state == "ok"
    assert hass.states.get("sensor.beta_status") is not None
    assert hass.states.get("sensor.beta_status").state == "ok"
    assert alpha.runtime_data.coordinator.last_attempt_at is not None
    assert alpha.runtime_data.coordinator.last_attempt_at > alpha_before
    assert beta.runtime_data.coordinator.last_attempt_at == beta_before


@pytest.mark.parametrize(
    ("old_subentry", "new_subentry", "old_entities", "new_entities"),
    [
        (
            _battery_subentry(subentry_id="battery_old", name="battery_old"),
            _battery_subentry(subentry_id="battery_new", name="battery_new"),
            ["sensor.home_battery_old_target", "sensor.home_battery_old_action"],
            ["sensor.home_battery_new_target", "sensor.home_battery_new_action"],
        ),
        (
            _comfort_subentry(subentry_id="comfort_old", name="comfort_old"),
            _comfort_subentry(subentry_id="comfort_new", name="comfort_new"),
            ["sensor.home_comfort_old_action"],
            ["sensor.home_comfort_new_action"],
        ),
        (
            _optional_subentry(subentry_id="optional_old", name="optional_old"),
            _optional_subentry(subentry_id="optional_new", name="optional_new"),
            [
                "sensor.home_optional_old_next_start_option",
                "sensor.home_optional_old_next_end_option",
                "sensor.home_optional_old_option_1_start",
            ],
            [
                "sensor.home_optional_new_next_start_option",
                "sensor.home_optional_new_next_end_option",
                "sensor.home_optional_new_option_1_start",
            ],
        ),
    ],
)
async def test_subentry_replacement_replaces_entities(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,
    old_subentry: dict[str, Any],
    new_subentry: dict[str, Any],
    old_entities: list[str],
    new_entities: list[str],
) -> None:
    """Replacing a subentry should remove old entities and create new ones."""
    # Purpose: verify runtime reload behavior for add/remove asset changes.
    entry = _entry(title="Home", subentries_data=[old_subentry])
    await _setup_entry(hass, entry)

    for entity_id in old_entities:
        assert hass.states.get(entity_id) is not None

    assert hass.config_entries.async_remove_subentry(entry, str(old_subentry["subentry_id"]))
    assert hass.config_entries.async_add_subentry(
        entry,
        config_entries.ConfigSubentry(
            subentry_id=str(new_subentry["subentry_id"]),
            subentry_type=str(new_subentry["subentry_type"]),
            title=str(new_subentry["title"]),
            unique_id=str(new_subentry["unique_id"]),
            data=dict(new_subentry["data"]),
        ),
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    for entity_id in old_entities:
        old_state = hass.states.get(entity_id)
        assert old_state is not None
        assert old_state.state == STATE_UNAVAILABLE
        assert old_state.attributes.get("restored") is True
    for entity_id in new_entities:
        assert hass.states.get(entity_id) is not None


async def test_scheduler_runs_at_interval(hass: HomeAssistant) -> None:
    """Scheduled refresh should run another cycle when time advances."""
    # Purpose: prove the scheduler path works, independent of direct services.
    entry = _entry(
        title="Home",
        subentries_data=[_battery_subentry(subentry_id="battery", name="battery")],
        options={
            CONF_PLANNING_ENABLED: True,
            CONF_ACTION_EMISSION_ENABLED: True,
        },
    )

    with patch("custom_components.wattplan.coordinator.optimize", side_effect=_fake_optimize):
        await _setup_entry(hass, entry)
        coordinator = entry.runtime_data.coordinator
        initial_attempt_at = coordinator.last_attempt_at
        assert initial_attempt_at is not None
        assert coordinator.next_refresh_at is not None

        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=60))
        await hass.async_block_till_done()

    assert coordinator.last_attempt_at is not None
    assert coordinator.last_attempt_at > initial_attempt_at


@pytest.mark.parametrize(
    ("source_override", "patch_optimize", "expected_status", "expected_source"),
    [
        (
            {
                CONF_SOURCE_IMPORT_PRICE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: "{{ 'broken' }}",
                },
            },
            None,
            "failed",
            ("sensor.home_import_price_status", "failed"),
        ),
        (
            {
                CONF_SOURCE_USAGE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: "{{ 'broken' }}",
                },
            },
            None,
            "failed",
            ("sensor.home_usage_status", "failed"),
        ),
        (
            {
                CONF_SOURCE_PV: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: "{{ 'broken' }}",
                },
            },
            None,
            "degraded",
            ("sensor.home_pv_status", "degraded"),
        ),
        (
            {},
            RuntimeError("optimizer failed"),
            "failed",
            None,
        ),
    ],
)
async def test_status_sensors_reflect_failures(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,
    source_override: dict[str, dict[str, Any]],
    patch_optimize: Exception | None,
    expected_status: str,
    expected_source: tuple[str, str] | None,
) -> None:
    """Overall and per-source status sensors should expose failure classes."""
    sources = _base_sources()
    sources.update(source_override)
    entry = _entry(
        title="Home",
        subentries_data=[_battery_subentry(subentry_id="battery", name="battery")],
        sources=sources,
    )
    await _setup_entry(hass, entry)
    optimize_patch = (
        patch(
            "custom_components.wattplan.coordinator.optimize",
            side_effect=patch_optimize,
        )
        if patch_optimize
        else patch(
            "custom_components.wattplan.coordinator.optimize",
            side_effect=_fake_optimize,
        )
    )
    if patch_optimize or expected_status == "failed":
        with optimize_patch, pytest.raises(PlanningStageError):
            await _run_optimize(hass)
    else:
        with optimize_patch:
            await _run_optimize(hass)

    status = hass.states.get("sensor.home_status")
    assert status is not None
    assert status.state == expected_status
    if expected_source is not None:
        entity_id, source_state = expected_source
        source_status = hass.states.get(entity_id)
        assert source_status is not None
        assert source_status.state == source_state
    battery_action = hass.states.get("sensor.home_battery_action")
    assert battery_action is not None
    if expected_status == "failed":
        assert battery_action.state == STATE_UNAVAILABLE


async def test_status_recovers_after_source_recovery(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,
) -> None:
    """A later successful run should restore the new status model."""
    sources = _base_sources()
    sources[CONF_SOURCE_IMPORT_PRICE] = {
        CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
        CONF_TEMPLATE: "{{ 'broken' }}",
    }
    entry = _entry(
        title="Home",
        subentries_data=[_battery_subentry(subentry_id="battery", name="battery")],
        sources=sources,
    )
    await _setup_entry(hass, entry)
    with patch(
        "custom_components.wattplan.coordinator.optimize", side_effect=_fake_optimize
    ), pytest.raises(PlanningStageError):
        await _run_optimize(hass)

    assert hass.states.get("sensor.home_status").state == "failed"
    assert hass.states.get("sensor.home_import_price_status").state == "failed"

    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_SOURCES: _base_sources(),
        },
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    with patch("custom_components.wattplan.coordinator.optimize", side_effect=_fake_optimize):
        await _run_optimize(hass)

    assert hass.states.get("sensor.home_status").state == "ok"
    assert hass.states.get("sensor.home_import_price_status").state == "ok"


async def test_suboptimal_result_is_exposed(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,
) -> None:
    """Suboptimal solve state should be projected to status and diagnostics."""
    # Purpose: verify suboptimal solver outcomes are visible to users.
    entry = _entry(
        title="Home",
        subentries_data=[_battery_subentry(subentry_id="battery", name="battery")],
    )
    await _setup_entry(hass, entry)
    suboptimal_result = _fake_optimize(object())
    suboptimal_result["suboptimal"] = True
    suboptimal_result["suboptimal_reasons"] = ["constraint_tightness"]

    with patch("custom_components.wattplan.coordinator.optimize", return_value=suboptimal_result):
        await _run_optimize(hass)

    status = hass.states.get("sensor.home_status")
    assert status is not None
    assert status.state == "degraded"
    assert "optimizer_suboptimal" in list(status.attributes.get("reason_codes", []))


async def test_emit_without_snapshot_raises_and_sets_error(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,
) -> None:
    """Calling emit with no plan should fail and set has_error to on."""
    # Purpose: verify no-plan emit uses explicit failure path instead of stale state.
    entry = _entry(
        title="Home",
        subentries_data=[_battery_subentry(subentry_id="battery", name="battery")],
    )
    await _setup_entry(hass, entry)
    entry.runtime_data.coordinator._snapshot = None
    entry.runtime_data.coordinator.data = None
    with pytest.raises(ServiceValidationError):
        await _run_emit(hass)

    coordinator = entry.runtime_data.coordinator
    assert coordinator.has_error is True
    assert coordinator.error_attributes()["emit_error_kind"] == "emit_no_snapshot"


async def test_removed_binary_error_entities_are_not_created(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,
) -> None:
    """Legacy binary error entities should not exist anymore."""
    entry = _entry(
        title="Home",
        subentries_data=[_battery_subentry(subentry_id="battery", name="battery")],
    )
    await _setup_entry(hass, entry)

    assert hass.states.get("binary_sensor.home_has_error") is None
    assert hass.states.get("binary_sensor.home_source_import_price_error") is None
    assert hass.states.get("binary_sensor.home_source_usage_error") is None
    assert hass.states.get("binary_sensor.home_source_pv_error") is None
    assert hass.states.get("binary_sensor.home_optimize_error") is None


async def test_optional_source_status_entities_are_omitted_when_not_configured(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,
) -> None:
    """Unconfigured optional sources should not get status entities."""
    sources = _base_sources()
    sources.pop(CONF_SOURCE_PV, None)
    entry = _entry(
        title="Home",
        subentries_data=[_battery_subentry(subentry_id="battery", name="battery")],
        sources=sources,
    )
    await _setup_entry(hass, entry)

    assert hass.states.get("sensor.home_import_price_status") is not None
    assert hass.states.get("sensor.home_usage_status") is not None
    assert hass.states.get("sensor.home_pv_status") is None
    assert hass.states.get("sensor.home_export_price_status") is None
