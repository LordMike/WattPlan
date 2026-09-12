"""Focused end-to-end integration tests for WattPlan runtime logic."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import patch

from custom_components.wattplan.const import (
    CONF_ACTION_EMISSION_ENABLED,
    CONF_AVAILABILITY_SOURCE,
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
    CONF_OPTIMIZER_LOOKAHEAD_HOURS,
    CONF_OPTIMIZER_LOOKAHEAD_SLOTS,
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
    SERVICE_REFRESH_SENSORS,
    SERVICE_RUN_OPTIMIZE_NOW,
    SOURCE_MODE_TEMPLATE,
    SUBENTRY_TYPE_BATTERY,
    SUBENTRY_TYPE_COMFORT,
    SUBENTRY_TYPE_OPTIONAL,
)
from custom_components.wattplan.coordinator import PlanningStageError
from custom_components.wattplan.test_plan_invariants import assert_plan_invariants
import pytest

from homeassistant import config_entries
from homeassistant.const import (
    CONF_NAME,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
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
    return assert_plan_invariants({
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
    })


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
                {"state": "grid_charge", "level": 5.0},
                {"state": "self_consume", "level": 5.0},
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
    return assert_plan_invariants(result)


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


def _battery_subentry(
    *,
    subentry_id: str,
    name: str,
    soc_source: str = "sensor.battery_soc",
    availability_source: str | None = None,
) -> config_entries.ConfigSubentryData:
    """Return battery subentry config."""
    data = {
        CONF_NAME: name,
        CONF_SOC_SOURCE: soc_source,
        CONF_CAPACITY_KWH: 10.0,
        CONF_MINIMUM_KWH: 1.0,
        CONF_MAX_CHARGE_KW: 3.0,
        CONF_MAX_DISCHARGE_KW: 3.0,
        CONF_CHARGE_EFFICIENCY: 0.9,
        CONF_DISCHARGE_EFFICIENCY: 0.9,
        CONF_CAN_CHARGE_FROM_GRID: True,
        CONF_CAN_CHARGE_FROM_PV: True,
    }
    if availability_source is not None:
        data[CONF_AVAILABILITY_SOURCE] = availability_source
    return config_entries.ConfigSubentryData(
        subentry_id=subentry_id,
        subentry_type=SUBENTRY_TYPE_BATTERY,
        title=name,
        unique_id=f"battery:{subentry_id}",
        data=data,
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
    slot_minutes: int = 60,
    hours_to_plan: int = 4,
    minor_version: int = 2,
) -> MockConfigEntry:
    """Build a mock WattPlan config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=title,
        data={
            CONF_NAME: title,
            CONF_SLOT_MINUTES: slot_minutes,
            CONF_HOURS_TO_PLAN: hours_to_plan,
            CONF_SOURCES: sources or _base_sources(),
        },
        options=options
        or {
            CONF_PLANNING_ENABLED: False,
            CONF_ACTION_EMISSION_ENABLED: False,
        },
        minor_version=minor_version,
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
    """Call refresh_sensors with optional filters."""
    payload: dict[str, Any] = {}
    if name is not None:
        payload[CONF_NAME] = name
    if entry_id is not None:
        payload["entry_id"] = entry_id
    await hass.services.async_call(DOMAIN, SERVICE_REFRESH_SENSORS, payload, blocking=True)


@pytest.mark.parametrize(
    ("slot_minutes", "legacy_hours", "edited_slots"),
    [(15, 5.5, 48), (30, 11.0, 24), (60, 22.0, 12)],
)
async def test_legacy_lookahead_migrates_and_can_be_edited_in_hours(
    hass: HomeAssistant,
    slot_minutes: int,
    legacy_hours: float,
    edited_slots: int,
) -> None:
    """Legacy entries should keep 22 slots, display them honestly, then edit."""
    horizon_slots = 12 * 60 // slot_minutes
    values = [0.2] * horizon_slots
    sources = {
        CONF_SOURCE_IMPORT_PRICE: {
            CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
            CONF_TEMPLATE: f"{{{{ {values!r} }}}}",
        },
        CONF_SOURCE_USAGE: {
            CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
            CONF_TEMPLATE: f"{{{{ {[1.0] * horizon_slots!r} }}}}",
        },
        CONF_SOURCE_PV: {
            CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
            CONF_TEMPLATE: f"{{{{ {[0.0] * horizon_slots!r} }}}}",
        },
    }
    options = {
        CONF_PLANNING_ENABLED: True,
        CONF_ACTION_EMISSION_ENABLED: False,
    }
    entry = _entry(
        title=f"Legacy Home {slot_minutes}",
        subentries_data=[],
        sources=sources,
        options=options,
        slot_minutes=slot_minutes,
        hours_to_plan=12,
        minor_version=1,
    )
    captured = []

    def capture_params(params: Any) -> dict[str, object]:
        captured.append(params)
        return _fake_optimize(params)

    with patch(
        "custom_components.wattplan.coordinator.optimize", side_effect=capture_params
    ):
        await _setup_entry(hass, entry)
        assert entry.minor_version == 2
        assert entry.options[CONF_OPTIMIZER_LOOKAHEAD_SLOTS] == 22
        assert captured[-1].lookahead_slots == 22
        assert captured[-1].slot_minutes == slot_minutes
        assert captured[-1].plan_start is not None
        assert captured[-1].plan_start.utcoffset() == timedelta(0)
        assert captured[-1].plan_start.second == 0
        assert captured[-1].plan_start.minute % slot_minutes == 0

        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "planner_core"}
        )
        schema = result["data_schema"].schema
        marker = next(
            key
            for key in schema
            if getattr(key, "schema", None) == CONF_OPTIMIZER_LOOKAHEAD_HOURS
        )
        assert marker.default() == legacy_hours
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_SLOT_MINUTES: str(slot_minutes),
                CONF_HOURS_TO_PLAN: "12",
                CONF_OPTIMIZER_LOOKAHEAD_HOURS: 12,
            },
        )
        assert result["type"] is FlowResultType.MENU
        await _run_optimize(hass, entry_id=entry.entry_id)
        await hass.async_block_till_done()

    assert entry.options[CONF_OPTIMIZER_LOOKAHEAD_SLOTS] == edited_slots
    assert captured[-1].lookahead_slots == edited_slots


async def test_runtime_missing_lookahead_falls_back_to_22_slots(
    hass: HomeAssistant,
) -> None:
    """Runtime assembly should preserve old behavior even before migration."""
    entry = _entry(
        title="Unmigrated Runtime",
        subentries_data=[],
        options={
            CONF_PLANNING_ENABLED: True,
            CONF_ACTION_EMISSION_ENABLED: False,
        },
        minor_version=2,
    )
    captured = []

    def capture_params(params: Any) -> dict[str, object]:
        captured.append(params)
        return _fake_optimize(params)

    with patch(
        "custom_components.wattplan.coordinator.optimize", side_effect=capture_params
    ):
        await _setup_entry(hass, entry)

    assert CONF_OPTIMIZER_LOOKAHEAD_SLOTS not in entry.options
    assert captured[-1].lookahead_slots == 22


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
                "sensor.home_optional_old_option_1_start",
            ],
            [
                "sensor.home_optional_new_next_start_option",
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
            "degraded",
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
    elif patch_optimize is not None:
        assert battery_action.state != STATE_UNAVAILABLE


async def test_fixed_battery_with_numeric_soc_is_planned(
    hass: HomeAssistant,
) -> None:
    """A battery without an availability source remains planned when SoC is numeric."""
    entry = _entry(
        title="Home",
        subentries_data=[_battery_subentry(subentry_id="battery", name="battery")],
    )
    await _setup_entry(hass, entry)
    captured_params: list[Any] = []

    def capture(params: Any) -> dict[str, object]:
        captured_params.append(params)
        return _fake_optimize_with_entities(params)

    with patch("custom_components.wattplan.coordinator.optimize", side_effect=capture):
        await _run_optimize(hass)

    assert [_name_of(battery) for battery in captured_params[-1].battery_entities] == [
        "battery"
    ]
    assert hass.states.get("sensor.home_status").state == "ok"
    assert hass.states.get("sensor.home_battery_action").state == "grid_charge"


@pytest.mark.parametrize(
    ("enabled", "expected_action"),
    [(True, "on"), (False, "off")],
)
async def test_constant_comfort_schedule_publishes_current_actions(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,
    enabled: bool,
    expected_action: str,
) -> None:
    """A comfort schedule without changes should not block plan publication."""
    entry = _entry(
        title="Home",
        subentries_data=[
            _battery_subentry(subentry_id="battery", name="battery"),
            _comfort_subentry(subentry_id="comfort", name="comfort"),
        ],
    )
    await _setup_entry(hass, entry)

    def constant_comfort(params: Any) -> dict[str, object]:
        result = _fake_optimize_with_entities(params)
        for entity in result["entities"]:
            if entity["type"] == "comfort":
                entity["schedule"] = [
                    {"enabled": enabled, "level": 1.0 if enabled else 0.0}
                ] * 4
        return assert_plan_invariants(result)

    with patch(
        "custom_components.wattplan.coordinator.optimize",
        side_effect=constant_comfort,
    ):
        await _run_optimize(hass)

    assert hass.states.get("sensor.home_status").state == "ok"
    assert hass.states.get("sensor.home_comfort_action").state == expected_action
    comfort_next = hass.states.get("sensor.home_comfort_next_action")
    assert comfort_next is not None
    assert comfort_next.state == STATE_UNKNOWN
    assert "timestamp" not in comfort_next.attributes
    assert hass.states.get("sensor.home_battery_action").state == "grid_charge"
    assert hass.states.get("sensor.home_battery_next_action").state == "self_consume"


@pytest.mark.parametrize(
    (
        "availability_state",
        "soc_state",
        "expected_batteries",
        "expected_status",
        "expected_reason",
        "expected_status_reason",
    ),
    [
        (STATE_OFF, None, [], "ok", "not_available_for_planning", None),
        (STATE_ON, "5.0", ["battery"], "ok", None, None),
        (
            STATE_ON,
            STATE_UNAVAILABLE,
            [],
            "degraded",
            "soc_unavailable",
            "battery_soc_unavailable",
        ),
        (
            STATE_UNKNOWN,
            "5.0",
            [],
            "degraded",
            "availability_unavailable",
            "battery_availability_unavailable",
        ),
        (
            None,
            "5.0",
            [],
            "degraded",
            "availability_unavailable",
            "battery_availability_unavailable",
        ),
    ],
)
async def test_battery_availability_controls_planner_input_and_status(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,
    availability_state: str | None,
    soc_state: str | None,
    expected_batteries: list[str],
    expected_status: str,
    expected_reason: str | None,
    expected_status_reason: str | None,
) -> None:
    """Availability and SoC determine whether one battery is sent to the optimizer."""
    entry = _entry(
        title="Home",
        subentries_data=[
            _battery_subentry(
                subentry_id="battery",
                name="battery",
                soc_source="sensor.availability_case_soc",
                availability_source="binary_sensor.battery_available",
            )
        ],
    )
    await _setup_entry(hass, entry)
    if availability_state is not None:
        hass.states.async_set("binary_sensor.battery_available", availability_state)
    if soc_state is not None:
        hass.states.async_set("sensor.availability_case_soc", soc_state)
    captured_params: list[Any] = []

    def capture(params: Any) -> dict[str, object]:
        captured_params.append(params)
        return _fake_optimize_with_entities(params)

    with patch("custom_components.wattplan.coordinator.optimize", side_effect=capture):
        await _run_optimize(hass)

    assert [
        _name_of(battery) for battery in captured_params[-1].battery_entities
    ] == expected_batteries
    status = hass.states.get("sensor.home_status")
    assert status is not None
    assert status.state == expected_status
    if expected_status_reason is not None:
        assert expected_status_reason in status.attributes["reason_codes"]

    diagnostics = entry.runtime_data.coordinator.snapshot.diagnostics
    skipped = diagnostics["skipped_batteries"]
    if expected_reason is None:
        assert skipped == {}
        assert hass.states.get("sensor.home_battery_action").state == "grid_charge"
        assert hass.states.get("sensor.home_battery_next_action").state == "self_consume"
        return

    assert skipped["battery"]["reason"] == expected_reason
    assert status.attributes["skipped_batteries"]["battery"]["reason"] == expected_reason
    assert hass.states.get("sensor.home_battery_action").state == STATE_UNAVAILABLE
    assert hass.states.get("sensor.home_battery_next_action").state == STATE_UNAVAILABLE
    target = hass.states.get("sensor.home_battery_target")
    assert target is not None
    assert target.state != STATE_UNAVAILABLE


@pytest.mark.parametrize(
    ("invalid_soc", "unit", "recovered_soc"),
    [
        ("not-a-number", "kWh", "5"),
        ("nan", "%", "50"),
        ("inf", "%", "50"),
        ("-inf", "%", "50"),
        ("nan", "kWh", "5"),
        ("inf", "kWh", "5"),
        ("-inf", "kWh", "5"),
    ],
)
async def test_invalid_soc_without_availability_skips_only_that_battery(
    hass: HomeAssistant,
    invalid_soc: str,
    unit: str,
    recovered_soc: str,
) -> None:
    """A bad fixed-battery SoC degrades status but does not fail the whole plan."""
    entry = _entry(
        title="Home",
        subentries_data=[
            _battery_subentry(
                subentry_id="battery",
                name="battery",
                soc_source="sensor.invalid_battery_soc",
            ),
            _battery_subentry(
                subentry_id="available",
                name="available",
                soc_source="sensor.available_battery_soc",
            ),
            _comfort_subentry(subentry_id="comfort", name="comfort"),
            _optional_subentry(subentry_id="optional", name="optional"),
        ],
    )
    await _setup_entry(hass, entry)
    hass.states.async_set(
        "sensor.invalid_battery_soc",
        invalid_soc,
        {"unit_of_measurement": unit},
    )
    hass.states.async_set("sensor.available_battery_soc", "6.0")
    captured_params: list[Any] = []

    def capture(params: Any) -> dict[str, object]:
        captured_params.append(params)
        result = _fake_optimize_with_entities(params)
        result["state"] = None
        return result

    with patch("custom_components.wattplan.coordinator.optimize", side_effect=capture):
        await _run_optimize(hass)

    params = captured_params[-1]
    assert [_name_of(battery) for battery in params.battery_entities] == ["available"]
    assert [_name_of(comfort) for comfort in params.comfort_entities] == ["comfort"]
    assert [_name_of(optional) for optional in params.optional_entities] == ["optional"]
    status = hass.states.get("sensor.home_status")
    assert status is not None
    assert status.state == "degraded"
    assert "battery_soc_unavailable" in status.attributes["reason_codes"]
    assert hass.states.get("sensor.home_battery_action").state == STATE_UNAVAILABLE
    assert hass.states.get("sensor.home_available_action").state == "grid_charge"

    hass.states.async_set(
        "sensor.invalid_battery_soc",
        recovered_soc,
        {"unit_of_measurement": unit},
    )
    with patch("custom_components.wattplan.coordinator.optimize", side_effect=capture):
        await _run_optimize(hass)

    assert [
        _name_of(battery) for battery in captured_params[-1].battery_entities
    ] == ["battery", "available"]
    assert hass.states.get("sensor.home_status").state == "ok"
    assert hass.states.get("sensor.home_battery_action").state == "grid_charge"


async def test_mixed_batteries_send_only_available_batteries_to_optimizer(
    hass: HomeAssistant,
) -> None:
    """One skipped battery should not remove other usable batteries from the plan."""
    entry = _entry(
        title="Home",
        subentries_data=[
            _battery_subentry(
                subentry_id="available",
                name="available",
                soc_source="sensor.available_battery_soc",
                availability_source="binary_sensor.available_battery_available",
            ),
            _battery_subentry(
                subentry_id="away",
                name="away",
                soc_source="sensor.away_battery_soc",
                availability_source="binary_sensor.away_battery_available",
            ),
        ],
    )
    await _setup_entry(hass, entry)
    hass.states.async_set("binary_sensor.available_battery_available", STATE_ON)
    hass.states.async_set("sensor.available_battery_soc", "6.0")
    hass.states.async_set("binary_sensor.away_battery_available", STATE_OFF)
    captured_params: list[Any] = []

    def capture(params: Any) -> dict[str, object]:
        captured_params.append(params)
        return _fake_optimize_with_entities(params)

    with patch("custom_components.wattplan.coordinator.optimize", side_effect=capture):
        await _run_optimize(hass)

    assert [_name_of(battery) for battery in captured_params[-1].battery_entities] == [
        "available"
    ]
    assert hass.states.get("sensor.home_status").state == "ok"
    skipped = entry.runtime_data.coordinator.snapshot.diagnostics["skipped_batteries"]
    assert skipped["away"]["reason"] == "not_available_for_planning"


async def test_all_batteries_skipped_still_runs_no_battery_plan(
    hass: HomeAssistant,
) -> None:
    """The optimizer should still run when every configured battery is skipped."""
    entry = _entry(
        title="Home",
        subentries_data=[
            _battery_subentry(
                subentry_id="away",
                name="away",
                soc_source="sensor.away_battery_soc",
                availability_source="binary_sensor.away_battery_available",
            )
        ],
    )
    await _setup_entry(hass, entry)
    hass.states.async_set("binary_sensor.away_battery_available", STATE_OFF)
    captured_params: list[Any] = []

    def capture(params: Any) -> dict[str, object]:
        captured_params.append(params)
        return _fake_optimize_with_entities(params)

    with patch("custom_components.wattplan.coordinator.optimize", side_effect=capture):
        await _run_optimize(hass)

    assert captured_params[-1].battery_entities == []
    assert hass.states.get("sensor.home_status").state == "ok"


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
