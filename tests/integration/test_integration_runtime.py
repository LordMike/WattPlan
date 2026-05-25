"""Integration runtime test for WattPlan planning and emission."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo
from unittest.mock import patch

from custom_components.wattplan.const import (
    ADAPTER_TYPE_ATTRIBUTE_OBJECTS,
    CONF_ACTION_EMISSION_ENABLED,
    CONF_ADAPTER_TYPE,
    CONF_CAN_CHARGE_FROM_GRID,
    CONF_CAN_CHARGE_FROM_PV,
    CONF_CAPACITY_KWH,
    CONF_CHARGE_EFFICIENCY,
    CONF_DISCHARGE_EFFICIENCY,
    CONF_DURATION_MINUTES,
    CONF_ENERGY_KWH,
    CONF_EXPECTED_POWER_KW,
    CONF_HOURS_TO_PLAN,
    CONF_HISTORICAL_COST_TRACKING_ENABLED,
    CONF_HISTORICAL_GRID_EXPORT_SENSOR,
    CONF_HISTORICAL_GRID_IMPORT_SENSOR,
    CONF_HISTORICAL_PV_SENSOR,
    CONF_HISTORICAL_SIMULATE_NO_BATTERY,
    CONF_HISTORICAL_SIMULATE_SELF_CONSUMPTION,
    CONF_HISTORICAL_USAGE_SENSOR,
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
    CONF_PROVIDERS,
    CONF_ROLLING_WINDOW_HOURS,
    CONF_RUN_WITHIN_HOURS,
    CONF_SLOT_MINUTES,
    CONF_SOC_SOURCE,
    CONF_SOURCE_MODE,
    CONF_SOURCE_EXPORT_PRICE,
    CONF_SOURCE_IMPORT_PRICE,
    CONF_SOURCE_PV,
    CONF_SOURCE_USAGE,
    CONF_SOURCES,
    CONF_TARGET_ON_HOURS_PER_WINDOW,
    CONF_TEMPLATE,
    CONF_TIME_KEY,
    CONF_VALUE_KEY,
    DOMAIN,
    SERVICE_CLEAR_TARGET,
    SERVICE_REFRESH_SENSORS,
    SERVICE_RUN_OPTIMIZE_NOW,
    SERVICE_SET_TARGET,
    SOURCE_MODE_ENTITY_ADAPTER,
    SOURCE_MODE_NOT_USED,
    SOURCE_MODE_TEMPLATE,
    SUBENTRY_TYPE_BATTERY,
    SUBENTRY_TYPE_COMFORT,
    SUBENTRY_TYPE_OPTIONAL,
)
from custom_components.wattplan.coordinator import (
    STORAGE_VERSION,
    CycleTrigger,
    _snapshot_schema_id,
)
from custom_components.wattplan.coordinator_parts import PlanningStageError, StageErrorKind
from custom_components.wattplan.historical_cost.models import (
    FLAG_METER_RESET,
    FLAG_MISSING_IMPORT_PRICE,
)
from custom_components.wattplan.historical_cost.store import HistoricalCostStore
from custom_components.wattplan.test_plan_invariants import assert_plan_invariants
import pytest

from homeassistant import config_entries
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import (
    CONF_NAME,
    EntityCategory,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfEnergy,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from tests.common import MockConfigEntry, async_fire_time_changed

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def _fake_optimize(_params: object) -> dict[str, object]:
    """Return deterministic optimizer output for integration projection tests."""
    return assert_plan_invariants({
        "execution_time": 0.01,
        "fitness": 1.23,
        "avg_price": 0.25,
        "projections": {
            "baseline_cost": 12.5,
            "projected_cost": 9.5,
            "projected_savings_cost": 3.0,
            "projected_savings_pct": 24.0,
            "per_slot": [
                {
                    "baseline_cost": 2.0,
                    "projected_cost": 1.5,
                    "projected_savings_cost": 0.5,
                    "projected_savings_pct": 25.0,
                },
                {
                    "baseline_cost": 3.0,
                    "projected_cost": 2.0,
                    "projected_savings_cost": 1.0,
                    "projected_savings_pct": 33.333333,
                },
                {
                    "baseline_cost": 4.0,
                    "projected_cost": 3.0,
                    "projected_savings_cost": 1.0,
                    "projected_savings_pct": 25.0,
                },
                {
                    "baseline_cost": 3.5,
                    "projected_cost": 3.0,
                    "projected_savings_cost": 0.5,
                    "projected_savings_pct": 14.285714,
                },
            ],
        },
        "suboptimal": False,
        "suboptimal_reasons": [],
        "problems": [],
        "successful_solves": 1,
        "reused_steps": 0,
        "entities": [
            {
                "name": "battery",
                "type": "battery",
                "schedule": [
                    {"state": "grid_charge", "level": 5.1},
                    {"state": "self_consume", "level": 5.1},
                    {"state": "self_consume", "level": 5.1},
                    {"state": "self_consume", "level": 4.9},
                ],
            },
            {
                "name": "comfort",
                "type": "comfort",
                "schedule": [
                    {"enabled": True, "level": 1.0},
                    {"enabled": False, "level": 0.9},
                    {"enabled": False, "level": 0.8},
                    {"enabled": True, "level": 0.9},
                ],
            },
        ],
        "optional_entity_options": [
            {
                "name": "optional",
                "options": [
                    {
                        "start_timeslot": 1,
                        "end_timeslot": 2,
                        "incremental_cost": 0.1,
                        "delta_from_best": 0.0,
                    },
                    {
                        "start_timeslot": 2,
                        "end_timeslot": 3,
                        "incremental_cost": 0.2,
                        "delta_from_best": 0.1,
                    },
                ],
            }
        ],
        "state": None,
    })


def _fake_optimize_with_target_behavior(params: object) -> dict[str, object]:
    """Return a deterministic plan that changes when a target is active."""
    battery = params.battery_entities[0]
    battery_schedule = (
        [
            {"state": "grid_charge", "level": 6.5},
            {"state": "grid_charge", "level": 8.0},
            {"state": "self_consume", "level": 8.0},
            {"state": "self_consume", "level": 8.0},
        ]
        if battery.target is not None
        else [
            {"state": "self_consume", "level": 5.0},
            {"state": "self_consume", "level": 5.0},
            {"state": "self_consume", "level": 5.0},
            {"state": "self_consume", "level": 5.0},
        ]
    )
    return {
        "execution_time": 0.01,
        "fitness": 1.0,
        "avg_price": 0.25,
        "projections": {
            "baseline_cost": 1.0,
            "projected_cost": 1.0,
            "projected_savings_cost": 0.0,
            "projected_savings_pct": 0.0,
            "per_slot": [
                {
                    "baseline_cost": 0.25,
                    "projected_cost": 0.25,
                    "projected_savings_cost": 0.0,
                    "projected_savings_pct": 0.0,
                }
                for _ in range(4)
            ],
        },
        "suboptimal": False,
        "suboptimal_reasons": [],
        "problems": [],
        "successful_solves": 1,
        "reused_steps": 0,
        "entities": [{"name": "battery", "type": "battery", "schedule": battery_schedule}],
        "optional_entity_options": [],
        "state": None,
    }


def _fake_optimize_with_extreme_savings(_params: object) -> dict[str, object]:
    """Return a plan whose percentage savings should be hidden as unknown."""
    return {
        "execution_time": 0.01,
        "fitness": 1.0,
        "avg_price": 0.25,
        "projections": {
            "baseline_cost": 0.1,
            "projected_cost": -1.4,
            "projected_savings_cost": 1.5,
            "projected_savings_pct": 1500.0,
            "per_slot": [
                {
                    "baseline_cost": 0.1,
                    "projected_cost": -1.4,
                    "projected_savings_cost": 1.5,
                    "projected_savings_pct": 1500.0,
                }
            ],
        },
        "suboptimal": False,
        "suboptimal_reasons": [],
        "problems": [],
        "successful_solves": 1,
        "reused_steps": 0,
        "entities": [],
        "optional_entity_options": [],
        "state": None,
    }




def _assert_valid_state(hass: HomeAssistant, entity_id: str) -> None:
    """Assert an entity exists and is not unknown/unavailable."""
    state = hass.states.get(entity_id)
    assert state is not None, f"{entity_id} missing"
    assert state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE), f"{entity_id} invalid"


def _set_energy_meter(hass: HomeAssistant, entity_id: str, value: float | str) -> None:
    """Set a cumulative kWh energy sensor state."""
    hass.states.async_set(
        entity_id,
        str(value),
        {
            "device_class": SensorDeviceClass.ENERGY,
            "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
        },
    )


def _historical_options() -> dict[str, object]:
    """Return standard enabled historical tracking options."""
    return {
        CONF_PLANNING_ENABLED: False,
        CONF_ACTION_EMISSION_ENABLED: False,
        CONF_HISTORICAL_COST_TRACKING_ENABLED: True,
        CONF_HISTORICAL_GRID_IMPORT_SENSOR: "sensor.grid_import_total",
        CONF_HISTORICAL_GRID_EXPORT_SENSOR: "sensor.grid_export_total",
        CONF_HISTORICAL_USAGE_SENSOR: "sensor.usage_total",
        CONF_HISTORICAL_PV_SENSOR: "sensor.pv_total",
        CONF_HISTORICAL_SIMULATE_NO_BATTERY: True,
        CONF_HISTORICAL_SIMULATE_SELF_CONSUMPTION: True,
    }


async def test_runtime_diagnostic_sensors_disabled_by_default(
    hass: HomeAssistant,
) -> None:
    """Register noisy runtime sensors disabled while keeping last run enabled."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data={
            CONF_NAME: "Home",
            CONF_SLOT_MINUTES: 60,
            CONF_HOURS_TO_PLAN: 4,
            CONF_SOURCES: {
                CONF_SOURCE_IMPORT_PRICE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: "{{ [0.2, 0.25, 0.3, 0.35] }}",
                },
                CONF_SOURCE_USAGE: {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED},
                CONF_SOURCE_PV: {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED},
            },
        },
        options={
            CONF_PLANNING_ENABLED: False,
            CONF_ACTION_EMISSION_ENABLED: False,
        },
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.wattplan.coordinator.optimize",
        side_effect=_fake_optimize,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        await hass.services.async_call(
            DOMAIN, SERVICE_RUN_OPTIMIZE_NOW, {}, blocking=True
        )
        await hass.async_block_till_done()

    entity_registry = er.async_get(hass)
    last_run_entry = entity_registry.async_get("sensor.home_last_run")
    next_run_entry = entity_registry.async_get("sensor.home_next_run")
    duration_entry = entity_registry.async_get("sensor.home_last_run_duration")

    assert last_run_entry is not None
    assert last_run_entry.disabled_by is None
    assert hass.states.get("sensor.home_last_run") is not None
    _assert_valid_state(hass, "sensor.home_last_run")

    assert next_run_entry is not None
    assert next_run_entry.disabled_by == er.RegistryEntryDisabler.INTEGRATION
    assert next_run_entry.entity_category == EntityCategory.DIAGNOSTIC
    assert hass.states.get("sensor.home_next_run") is None

    assert duration_entry is not None
    assert duration_entry.disabled_by == er.RegistryEntryDisabler.INTEGRATION
    assert duration_entry.entity_category == EntityCategory.DIAGNOSTIC
    assert hass.states.get("sensor.home_last_run_duration") is None


async def test_full_runtime_optimize_and_emit_once(hass: HomeAssistant) -> None:
    """Set up entry with one of each asset and assert runtime entities have data."""
    price_template = "{{ [0.2, 0.25, 0.3, 0.35] }}"
    usage_template = "{{ [1.0, 1.1, 1.0, 0.9] }}"
    pv_template = "{{ [0.0, 0.2, 0.3, 0.1] }}"
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data={
            CONF_NAME: "Home",
            CONF_SLOT_MINUTES: 60,
            CONF_HOURS_TO_PLAN: 4,
            CONF_SOURCES: {
                CONF_SOURCE_IMPORT_PRICE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: price_template,
                },
                CONF_SOURCE_USAGE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: usage_template,
                },
                CONF_SOURCE_PV: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: pv_template,
                },
            },
        },
        options={
            CONF_PLANNING_ENABLED: False,
            CONF_ACTION_EMISSION_ENABLED: False,
        },
        subentries_data=[
            config_entries.ConfigSubentryData(
                subentry_id="battery_sub",
                subentry_type=SUBENTRY_TYPE_BATTERY,
                title="battery",
                unique_id="battery:battery",
                data={
                    CONF_NAME: "battery",
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
            ),
            config_entries.ConfigSubentryData(
                subentry_id="comfort_sub",
                subentry_type=SUBENTRY_TYPE_COMFORT,
                title="comfort",
                unique_id="comfort:comfort",
                data={
                    CONF_NAME: "comfort",
                    CONF_ROLLING_WINDOW_HOURS: 4,
                    CONF_TARGET_ON_HOURS_PER_WINDOW: 1,
                    CONF_MIN_CONSECUTIVE_ON_MINUTES: 60,
                    CONF_MIN_CONSECUTIVE_OFF_MINUTES: 60,
                    CONF_MAX_CONSECUTIVE_OFF_MINUTES: 120,
                    CONF_ON_OFF_SOURCE: "binary_sensor.comfort_on_off",
                    CONF_EXPECTED_POWER_KW: 1.2,
                },
            ),
            config_entries.ConfigSubentryData(
                subentry_id="optional_sub",
                subentry_type=SUBENTRY_TYPE_OPTIONAL,
                title="optional",
                unique_id="optional:optional",
                data={
                    CONF_NAME: "optional",
                    CONF_DURATION_MINUTES: 60,
                    CONF_RUN_WITHIN_HOURS: 3,
                    CONF_ENERGY_KWH: 1.5,
                    CONF_OPTIONS_COUNT: 2,
                    CONF_MIN_OPTION_GAP_MINUTES: 60,
                },
            ),
        ],
    )
    entry.add_to_hass(hass)

    hass.states.async_set("sensor.battery_soc", "5.0")
    hass.states.async_set("binary_sensor.comfort_on_off", "off")

    with patch("custom_components.wattplan.coordinator.optimize", side_effect=_fake_optimize):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        await hass.services.async_call(
            DOMAIN, SERVICE_RUN_OPTIMIZE_NOW, {}, blocking=True
        )
        await hass.async_block_till_done()
        last_run_after_plan = hass.states.get("sensor.home_last_run")
        assert last_run_after_plan is not None

        await hass.services.async_call(DOMAIN, SERVICE_REFRESH_SENSORS, {}, blocking=True)
        await hass.async_block_till_done()

    last_run_after_refresh = hass.states.get("sensor.home_last_run")
    assert last_run_after_refresh is not None
    assert last_run_after_refresh.state == last_run_after_plan.state

    _assert_valid_state(hass, "sensor.home_status")
    _assert_valid_state(hass, "sensor.home_last_run")
    _assert_valid_state(hass, "sensor.home_battery_action")
    _assert_valid_state(hass, "sensor.home_comfort_action")
    _assert_valid_state(hass, "sensor.home_optional_next_start_option")
    _assert_valid_state(hass, "sensor.home_optional_option_1_start")

    entity_registry = er.async_get(hass)
    for entity_id in (
        "sensor.home_projected_cost_savings",
        "sensor.home_projected_savings_percentage",
        "sensor.home_projected_cost_savings_this_interval",
        "sensor.home_projected_savings_percentage_this_interval",
    ):
        assert entity_registry.async_get(entity_id) is None

    next_option = hass.states.get("sensor.home_optional_next_start_option")
    assert next_option is not None
    next_option_start = dt_util.parse_datetime(next_option.state)
    next_option_end = dt_util.parse_datetime(next_option.attributes["end_timestamp"])
    assert next_option_start is not None
    assert next_option_end is not None
    assert next_option_end - next_option_start == timedelta(hours=1)

    option_1 = hass.states.get("sensor.home_optional_option_1_start")
    assert option_1 is not None
    option_1_start = dt_util.parse_datetime(option_1.state)
    option_1_end = dt_util.parse_datetime(option_1.attributes["end_timestamp"])
    assert option_1_start is not None
    assert option_1_end is not None
    assert option_1_end - option_1_start == timedelta(hours=1)
    assert option_1.state == next_option.state
    assert option_1.attributes["end_timestamp"] == next_option.attributes["end_timestamp"]

    battery_action = hass.states.get("sensor.home_battery_action")
    assert battery_action is not None
    assert battery_action.attributes["friendly_name"] == "(battery) Action"
    assert battery_action.state == "grid_charge"
    assert "next_action" not in battery_action.attributes
    assert "next_action_timestamp" not in battery_action.attributes

    next_option = hass.states.get("sensor.home_optional_next_start_option")
    assert next_option is not None
    assert next_option.attributes["friendly_name"] == "(optional) Next Start Option"

    option_1 = hass.states.get("sensor.home_optional_option_1_start")
    assert option_1 is not None
    assert option_1.attributes["friendly_name"] == "(optional) Option 1 Start"


async def test_historical_cost_tracking_seeds_without_fake_first_slot(
    hass: HomeAssistant,
) -> None:
    """First historical run should seed cursors without creating cost history."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data={
            CONF_NAME: "Home",
            CONF_SLOT_MINUTES: 60,
            CONF_HOURS_TO_PLAN: 4,
            CONF_SOURCES: {
                CONF_SOURCE_IMPORT_PRICE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: "{{ [1.0, 1.0, 1.0, 1.0] }}",
                },
            },
        },
        options=_historical_options(),
    )
    entry.add_to_hass(hass)
    _set_energy_meter(hass, "sensor.grid_import_total", 100.0)
    _set_energy_meter(hass, "sensor.grid_export_total", 10.0)
    _set_energy_meter(hass, "sensor.usage_total", 200.0)
    _set_energy_meter(hass, "sensor.pv_total", 50.0)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    tracker = entry.runtime_data.historical_tracker
    assert tracker is not None
    assert tracker.store.data["days"] == {}
    assert hass.states.get("sensor.home_historical_actual_cost_today").state in {
        STATE_UNAVAILABLE,
        STATE_UNKNOWN,
    }


async def test_historical_cost_tracking_processes_scenarios_and_entities(
    hass: HomeAssistant,
    freezer,
) -> None:
    """Historical tracker should aggregate actual, no-battery, and self-consumption costs."""
    start = datetime(2026, 5, 24, 12, 0, tzinfo=UTC)
    freezer.move_to(start + timedelta(hours=1, seconds=2))
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data={
            CONF_NAME: "Home",
            CONF_SLOT_MINUTES: 60,
            CONF_HOURS_TO_PLAN: 4,
            CONF_SOURCES: {
                CONF_SOURCE_IMPORT_PRICE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: "{{ [1.0, 1.0, 1.0, 1.0] }}",
                },
                CONF_SOURCE_EXPORT_PRICE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: "{{ [0.1, 0.1, 0.1, 0.1] }}",
                },
            },
        },
        options=_historical_options(),
        subentries_data=[
            config_entries.ConfigSubentryData(
                subentry_id="battery_sub",
                subentry_type=SUBENTRY_TYPE_BATTERY,
                title="battery",
                unique_id="battery:battery",
                data={
                    CONF_NAME: "battery",
                    CONF_SOC_SOURCE: "sensor.battery_soc",
                    CONF_CAPACITY_KWH: 10.0,
                    CONF_MINIMUM_KWH: 0.0,
                    CONF_MAX_CHARGE_KW: 3.0,
                    CONF_MAX_DISCHARGE_KW: 3.0,
                    CONF_CHARGE_EFFICIENCY: 1.0,
                    CONF_DISCHARGE_EFFICIENCY: 1.0,
                    CONF_CAN_CHARGE_FROM_GRID: False,
                    CONF_CAN_CHARGE_FROM_PV: True,
                },
            )
        ],
    )
    entry.add_to_hass(hass)
    _set_energy_meter(hass, "sensor.grid_import_total", 100.0)
    _set_energy_meter(hass, "sensor.grid_export_total", 10.0)
    _set_energy_meter(hass, "sensor.usage_total", 200.0)
    _set_energy_meter(hass, "sensor.pv_total", 50.0)
    hass.states.async_set(
        "sensor.battery_soc",
        "1.0",
        {"unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR},
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    tracker = entry.runtime_data.historical_tracker
    assert tracker is not None
    tracker.store.update_metadata(
        last_processed_slot=start - timedelta(hours=1),
        last_meter_values={
            "grid_import": 100.0,
            "grid_export": 10.0,
            "usage": 200.0,
            "pv": 50.0,
        },
        meter_config={
            "grid_import": "sensor.grid_import_total",
            "grid_export": "sensor.grid_export_total",
            "usage": "sensor.usage_total",
            "pv": "sensor.pv_total",
        },
    )
    tracker.store.update_simulation_soc({"battery_sub": 1.0})

    _set_energy_meter(hass, "sensor.grid_import_total", 101.0)
    _set_energy_meter(hass, "sensor.grid_export_total", 10.2)
    _set_energy_meter(hass, "sensor.usage_total", 201.5)
    _set_energy_meter(hass, "sensor.pv_total", 51.0)

    await tracker.async_process_completed_slot(start + timedelta(hours=1, seconds=1))
    await hass.async_block_till_done()

    actual = hass.states.get("sensor.home_historical_actual_cost_today")
    no_battery = hass.states.get("sensor.home_historical_no_battery_cost_today")
    self_consumption = hass.states.get(
        "sensor.home_historical_self_consumption_cost_today"
    )
    savings = hass.states.get("sensor.home_historical_savings_vs_no_battery_today")

    assert actual is not None
    assert float(actual.state) == pytest.approx(0.98)
    assert actual.attributes["slots"] == 1
    assert actual.attributes["missing_slots"] == 0
    assert actual.attributes["scenario"] == "actual"
    assert no_battery is not None
    assert float(no_battery.state) == pytest.approx(0.5)
    assert self_consumption is not None
    assert float(self_consumption.state) == pytest.approx(0.0)
    assert savings is not None
    assert float(savings.state) == pytest.approx(-0.48)

    entity_registry = er.async_get(hass)
    monthly = entity_registry.async_get(
        "sensor.home_historical_actual_cost_this_month"
    )
    assert monthly is not None
    assert monthly.disabled


async def test_historical_cost_tracking_flags_meter_reset_and_missing_price(
    hass: HomeAssistant,
    freezer,
) -> None:
    """Invalid deltas and missing prices should create gap records instead of costs."""
    start = datetime(2026, 5, 24, 12, 0, tzinfo=UTC)
    freezer.move_to(start + timedelta(hours=1, seconds=2))
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data={
            CONF_NAME: "Home",
            CONF_SLOT_MINUTES: 60,
            CONF_HOURS_TO_PLAN: 4,
            CONF_SOURCES: {
                CONF_SOURCE_IMPORT_PRICE: {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED},
            },
        },
        options={
            **_historical_options(),
            CONF_HISTORICAL_GRID_EXPORT_SENSOR: None,
            CONF_HISTORICAL_PV_SENSOR: None,
            CONF_HISTORICAL_SIMULATE_SELF_CONSUMPTION: False,
        },
    )
    entry.add_to_hass(hass)
    _set_energy_meter(hass, "sensor.grid_import_total", 100.0)
    _set_energy_meter(hass, "sensor.usage_total", 200.0)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    tracker = entry.runtime_data.historical_tracker
    assert tracker is not None
    tracker.store.update_metadata(
        last_processed_slot=start - timedelta(hours=1),
        last_meter_values={
            "grid_import": 100.0,
            "grid_export": 0.0,
            "usage": 200.0,
            "pv": 0.0,
        },
        meter_config={
            "grid_import": "sensor.grid_import_total",
            "grid_export": None,
            "usage": "sensor.usage_total",
            "pv": None,
        },
    )

    _set_energy_meter(hass, "sensor.grid_import_total", 99.0)
    _set_energy_meter(hass, "sensor.usage_total", 201.0)

    await tracker.async_process_completed_slot(start + timedelta(hours=1, seconds=1))

    day = tracker.store.data["days"]["2026-05-24"]
    assert day["flags"] == [FLAG_METER_RESET | FLAG_MISSING_IMPORT_PRICE]
    assert hass.states.get("sensor.home_historical_actual_cost_today").state in {
        STATE_UNAVAILABLE,
        STATE_UNKNOWN,
    }


async def test_historical_cost_store_prunes_old_days(hass: HomeAssistant) -> None:
    """Historical store should keep only the fixed local-day retention window."""
    store = HistoricalCostStore(
        hass,
        entry_id="history-entry",
        slot_minutes=60,
        currency="DKK",
    )
    await store.async_load()
    store.data["days"] = {
        "2026-03-01": {"starts": []},
        "2026-05-01": {"starts": []},
    }

    store.prune(datetime(2026, 5, 24, 12, 0, tzinfo=UTC))

    assert "2026-03-01" not in store.data["days"]
    assert "2026-05-01" in store.data["days"]


async def test_battery_action_sensor_uses_source_specific_charge_state(
    hass: HomeAssistant,
) -> None:
    """Battery action sensor should expose the chosen charging ingress in the state."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data={
            CONF_NAME: "Home",
            CONF_SLOT_MINUTES: 60,
            CONF_HOURS_TO_PLAN: 4,
            CONF_SOURCES: {
                CONF_SOURCE_IMPORT_PRICE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: "{{ [0.2, 0.25, 0.3, 0.35] }}",
                },
                CONF_SOURCE_USAGE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: "{{ [1.0, 1.1, 1.0, 0.9] }}",
                },
                CONF_SOURCE_PV: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: "{{ [0.0, 0.2, 0.3, 0.1] }}",
                },
            },
        },
        options={
            CONF_PLANNING_ENABLED: False,
            CONF_ACTION_EMISSION_ENABLED: False,
        },
        subentries_data=[
            config_entries.ConfigSubentryData(
                subentry_id="battery_sub",
                subentry_type=SUBENTRY_TYPE_BATTERY,
                title="battery",
                unique_id="battery:battery",
                data={
                    CONF_NAME: "battery",
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
        ],
    )
    entry.add_to_hass(hass)

    hass.states.async_set("sensor.battery_soc", "5.0")

    with patch("custom_components.wattplan.coordinator.optimize") as optimize_mock:
        optimize_mock.return_value = {
            **_fake_optimize(None),
            "entities": [
                {
                    "name": "battery",
                    "type": "battery",
                    "schedule": [
                        {"state": "grid_charge", "level": 5.2},
                        {"state": "self_consume", "level": 5.2},
                        {"state": "self_consume", "level": 5.2},
                        {"state": "self_consume", "level": 5.2},
                    ],
                }
            ],
            "optional_entity_options": [],
        }
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        await hass.services.async_call(
            DOMAIN, SERVICE_RUN_OPTIMIZE_NOW, {}, blocking=True
        )
        await hass.async_block_till_done()

    battery_action = hass.states.get("sensor.home_battery_action")
    assert battery_action is not None
    assert battery_action.state == "grid_charge"


async def test_battery_next_action_sensor_exposes_timestamp_and_state(
    hass: HomeAssistant,
) -> None:
    """Next-action sensor should expose the next planned action and timestamp."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data={
            CONF_NAME: "Home",
            CONF_SLOT_MINUTES: 60,
            CONF_HOURS_TO_PLAN: 4,
            CONF_SOURCES: {
                CONF_SOURCE_IMPORT_PRICE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: "{{ [0.2, 0.25, 0.3, 0.35] }}",
                },
                CONF_SOURCE_USAGE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: "{{ [1.0, 1.1, 1.0, 0.9] }}",
                },
                CONF_SOURCE_PV: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: "{{ [0.0, 0.2, 0.3, 0.1] }}",
                },
            },
        },
        options={
            CONF_PLANNING_ENABLED: False,
            CONF_ACTION_EMISSION_ENABLED: False,
        },
        subentries_data=[
            config_entries.ConfigSubentryData(
                subentry_id="battery_sub",
                subentry_type=SUBENTRY_TYPE_BATTERY,
                title="battery",
                unique_id="battery:battery",
                data={
                    CONF_NAME: "battery",
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
        ],
    )
    entry.add_to_hass(hass)

    hass.states.async_set("sensor.battery_soc", "5.0")

    with patch(
        "homeassistant.helpers.entity.Entity.entity_registry_enabled_default",
        return_value=True,
    ):
        with patch("custom_components.wattplan.coordinator.optimize") as optimize_mock:
            optimize_mock.return_value = {
                **_fake_optimize(None),
                "entities": [
                    {
                        "name": "battery",
                        "type": "battery",
                        "schedule": [
                            {"state": "self_consume", "level": 5.0},
                            {"state": "grid_charge", "level": 5.2},
                            {"state": "self_consume", "level": 5.2},
                            {"state": "self_consume", "level": 5.2},
                        ],
                    }
                ],
                "optional_entity_options": [],
            }
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

            await hass.services.async_call(
                DOMAIN, SERVICE_RUN_OPTIMIZE_NOW, {}, blocking=True
            )
            await hass.async_block_till_done()

    next_action = hass.states.get("sensor.home_battery_next_action")
    assert next_action is not None
    assert next_action.state == "grid_charge"
    assert "timestamp" in next_action.attributes


async def test_restore_snapshot_on_startup(hass: HomeAssistant) -> None:
    """Restore the serialized coordinator snapshot so entities keep their last plan."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data={
            CONF_NAME: "Home",
            CONF_SLOT_MINUTES: 60,
            CONF_HOURS_TO_PLAN: 4,
            CONF_SOURCES: {
                CONF_SOURCE_IMPORT_PRICE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: "{{ [0.2, 0.25, 0.3, 0.35] }}",
                },
                CONF_SOURCE_USAGE: {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED},
                CONF_SOURCE_PV: {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED},
            },
        },
        options={
            CONF_PLANNING_ENABLED: False,
            CONF_ACTION_EMISSION_ENABLED: False,
        },
        subentries_data=[
            config_entries.ConfigSubentryData(
                subentry_id="optional_sub",
                subentry_type=SUBENTRY_TYPE_OPTIONAL,
                title="optional",
                unique_id="optional:optional",
                data={
                    CONF_NAME: "optional",
                    CONF_DURATION_MINUTES: 60,
                    CONF_RUN_WITHIN_HOURS: 3,
                    CONF_ENERGY_KWH: 1.5,
                    CONF_OPTIONS_COUNT: 2,
                    CONF_MIN_OPTION_GAP_MINUTES: 60,
                },
            ),
        ],
    )
    entry.add_to_hass(hass)

    store = Store[dict[str, object]](
        hass,
        STORAGE_VERSION,
        f"{DOMAIN}.snapshot.{entry.entry_id}",
        private=True,
    )
    await store.async_save(
        {
            "schema_id": _snapshot_schema_id(),
            "snapshot": {
                "created_at": "2099-01-01T00:00:00+00:00",
                "planner_status": "planned",
                "planner_message": "Restored plan",
                "diagnostics": {
                    "batteries": {},
                    "comforts": {},
                    "optionals": {
                        "optional_sub": {
                            "next_start_option": "2026-01-01T01:00:00+00:00",
                            "next_end_option": "2026-01-01T02:00:00+00:00",
                            "option_1_start": "2026-01-01T01:00:00+00:00",
                            "option_1_end": "2026-01-01T02:00:00+00:00",
                        }
                    },
                    "optimizer": {
                        "suboptimal": False,
                        "suboptimal_reasons": [],
                        "span_start": "2099-01-01T00:00:00+00:00",
                        "span_end": "2099-01-01T04:00:00+00:00",
                    },
                },
            },
            "last_success_at": "2099-01-01T00:00:00+00:00",
            "last_duration_ms": 123,
            "last_run_timings": [
                ["Import price source fetch", 12],
                ["Optimizer plan calculation", 34],
                ["total", 46],
            ],
        }
    )

    with patch(
        "homeassistant.helpers.entity.Entity.entity_registry_enabled_default",
        return_value=True,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    _assert_valid_state(hass, "sensor.home_status")
    _assert_valid_state(hass, "sensor.home_optional_next_start_option")
    _assert_valid_state(hass, "sensor.home_last_run_duration")

    duration_state = hass.states.get("sensor.home_last_run_duration")
    assert duration_state is not None
    restored_timings = duration_state.attributes["timings"]
    assert isinstance(restored_timings, list)
    assert [entry[0] for entry in restored_timings] == [
        "Import price source fetch",
        "Optimizer plan calculation",
        "total",
    ]
    assert all(
        isinstance(entry, list | tuple) and len(entry) == 2 for entry in restored_timings
    )
    assert all(isinstance(entry[1], int) for entry in restored_timings)

    next_option = hass.states.get("sensor.home_optional_next_start_option")
    assert next_option is not None
    next_option_start = dt_util.parse_datetime(next_option.state)
    next_option_end = dt_util.parse_datetime(next_option.attributes["end_timestamp"])
    assert next_option_start is not None
    assert next_option_end is not None
    assert next_option_end - next_option_start == timedelta(hours=1)


async def test_successful_plan_persists_completed_last_run(
    hass: HomeAssistant,
) -> None:
    """Persist the successful run timestamp from the run that just completed."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data={
            CONF_NAME: "Home",
            CONF_SLOT_MINUTES: 60,
            CONF_HOURS_TO_PLAN: 4,
            CONF_SOURCES: {
                CONF_SOURCE_IMPORT_PRICE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: "{{ [0.2, 0.25, 0.3, 0.35] }}",
                },
                CONF_SOURCE_USAGE: {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED},
                CONF_SOURCE_PV: {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED},
            },
        },
        options={
            CONF_PLANNING_ENABLED: False,
            CONF_ACTION_EMISSION_ENABLED: False,
        },
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.wattplan.coordinator.optimize",
        side_effect=_fake_optimize,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    store = Store[dict[str, object]](
        hass,
        STORAGE_VERSION,
        f"{DOMAIN}.snapshot.{entry.entry_id}",
        private=True,
    )
    payload = await store.async_load()

    assert payload is not None
    assert payload["last_success_at"] == coordinator.last_success_at.isoformat()


async def test_failed_plan_keeps_restored_snapshot_usable(
    hass: HomeAssistant,
) -> None:
    """A failed follow-up plan should not make restored plan entities unavailable."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data={
            CONF_NAME: "Home",
            CONF_SLOT_MINUTES: 60,
            CONF_HOURS_TO_PLAN: 4,
            CONF_SOURCES: {
                CONF_SOURCE_IMPORT_PRICE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: "{{ [0.2, 0.25, 0.3, 0.35] }}",
                },
                CONF_SOURCE_USAGE: {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED},
                CONF_SOURCE_PV: {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED},
            },
        },
        options={
            CONF_PLANNING_ENABLED: False,
            CONF_ACTION_EMISSION_ENABLED: False,
        },
        subentries_data=[
            config_entries.ConfigSubentryData(
                subentry_id="battery_sub",
                subentry_type=SUBENTRY_TYPE_BATTERY,
                title="battery",
                unique_id="battery:battery",
                data={
                    CONF_NAME: "battery",
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
            ),
        ],
    )
    entry.add_to_hass(hass)

    store = Store[dict[str, object]](
        hass,
        STORAGE_VERSION,
        f"{DOMAIN}.snapshot.{entry.entry_id}",
        private=True,
    )
    await store.async_save(
        {
            "schema_id": _snapshot_schema_id(),
            "snapshot": {
                "created_at": "2099-01-01T00:00:00+00:00",
                "planner_status": "planned",
                "planner_message": "Restored plan",
                "diagnostics": {
                    "batteries": {
                        "battery_sub": {
                            "action": "grid_charge",
                        }
                    },
                    "comforts": {},
                    "optionals": {},
                    "optimizer": {
                        "suboptimal": False,
                        "suboptimal_reasons": [],
                        "span_start": "2099-01-01T00:00:00+00:00",
                        "span_end": "2099-01-01T04:00:00+00:00",
                    },
                },
            },
            "last_success_at": "2099-01-01T00:00:00+00:00",
            "last_duration_ms": 123,
            "last_run_timings": [["total", 123]],
        }
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    _assert_valid_state(hass, "sensor.home_battery_action")

    coordinator = entry.runtime_data.coordinator
    with patch.object(
        coordinator,
        "_async_build_planning_request",
        side_effect=PlanningStageError(
            StageErrorKind.PLANNER_INPUT,
            "import_price source entity `sensor.missing` was not found",
        ),
    ):
        with pytest.raises(PlanningStageError):
            await coordinator.async_plan(trigger=CycleTrigger.SERVICE)
        await hass.async_block_till_done()

    status = hass.states.get("sensor.home_status")
    assert status is not None
    assert status.state == "degraded"
    assert status.attributes["has_usable_plan"] is True
    assert status.attributes["reason_codes"] == ["planner_failed_using_previous_plan"]
    assert status.attributes["expires_at"] == "2099-01-01T04:00:00+00:00"

    action = hass.states.get("sensor.home_battery_action")
    assert action is not None
    assert action.state == "grid_charge"


async def test_retained_plan_expires_and_plan_entities_become_unavailable(
    hass: HomeAssistant,
) -> None:
    """A retained previous plan should stop being usable after its coverage ends."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data={
            CONF_NAME: "Home",
            CONF_SLOT_MINUTES: 60,
            CONF_HOURS_TO_PLAN: 4,
            CONF_SOURCES: {
                CONF_SOURCE_IMPORT_PRICE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: "{{ [0.2, 0.25, 0.3, 0.35] }}",
                },
                CONF_SOURCE_USAGE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: "{{ [1.0, 1.1, 1.0, 0.9] }}",
                },
            },
        },
        options={
            CONF_PLANNING_ENABLED: False,
            CONF_ACTION_EMISSION_ENABLED: False,
        },
        subentries_data=[
            config_entries.ConfigSubentryData(
                subentry_id="battery_sub",
                subentry_type=SUBENTRY_TYPE_BATTERY,
                title="battery",
                unique_id="battery:battery",
                data={
                    CONF_NAME: "battery",
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
            ),
        ],
    )
    entry.add_to_hass(hass)
    hass.states.async_set("sensor.battery_soc", "5.0")

    with patch("custom_components.wattplan.coordinator.optimize", side_effect=_fake_optimize):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await hass.services.async_call(
            DOMAIN, SERVICE_RUN_OPTIMIZE_NOW, {}, blocking=True
        )
        await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    status = hass.states.get("sensor.home_status")
    assert status is not None
    assert status.state == "ok"
    expires_at = dt_util.parse_datetime(status.attributes["expires_at"])
    assert expires_at is not None

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            expired_at = expires_at + timedelta(minutes=1)
            return expired_at if tz is not None else expired_at.replace(tzinfo=None)

    with patch(
        "custom_components.wattplan.coordinator_logic.source_status.datetime",
        FrozenDateTime,
    ):
        coordinator.async_update_listeners()
        await hass.async_block_till_done()

    status = hass.states.get("sensor.home_status")
    assert status is not None
    assert status.state == "failed"
    assert status.attributes["reason_codes"] == ["plan_stale"]
    assert status.attributes["is_stale"] is True
    assert status.attributes["has_usable_plan"] is False

    action = hass.states.get("sensor.home_battery_action")
    assert action is not None
    assert action.state == STATE_UNAVAILABLE

    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    status = hass.states.get("sensor.home_status")
    assert status is not None
    assert status.state == "ok"

    with patch.object(
        coordinator,
        "_async_build_planning_request",
        side_effect=PlanningStageError(
            StageErrorKind.PLANNER_INPUT,
            "import_price source entity `sensor.missing` was not found",
        ),
    ):
        with pytest.raises(PlanningStageError):
            await coordinator.async_plan(trigger=CycleTrigger.SERVICE)
        await hass.async_block_till_done()

    status = hass.states.get("sensor.home_status")
    assert status is not None
    assert status.state == "degraded"
    assert status.attributes["reason_codes"] == ["planner_failed_using_previous_plan"]
    assert status.attributes["expires_at"] == expires_at.isoformat()
    assert status.attributes["has_usable_plan"] is True

    with patch(
        "custom_components.wattplan.coordinator_logic.source_status.datetime",
        FrozenDateTime,
    ):
        coordinator.async_update_listeners()
        await hass.async_block_till_done()

    status = hass.states.get("sensor.home_status")
    assert status is not None
    assert status.state == "failed"
    assert status.attributes["reason_codes"] == ["plan_stale"]
    assert status.attributes["is_stale"] is True
    assert status.attributes["has_usable_plan"] is False

    action = hass.states.get("sensor.home_battery_action")
    assert action is not None
    assert action.state == STATE_UNAVAILABLE


async def test_plan_details_sensor_exposes_horizon_length_arrays(
    hass: HomeAssistant,
) -> None:
    """Enable the plan details sensor and assert graph payload shape is compact."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data={
            CONF_NAME: "Home",
            CONF_SLOT_MINUTES: 60,
            CONF_HOURS_TO_PLAN: 4,
            CONF_SOURCES: {
                CONF_SOURCE_IMPORT_PRICE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: "{{ [0.2, 0.25, 0.3, 0.35] }}",
                },
                CONF_SOURCE_USAGE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: "{{ [1.0, 1.1, 1.0, 0.9] }}",
                },
                CONF_SOURCE_PV: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: "{{ [0.0, 0.2, 0.3, 0.1] }}",
                },
            },
        },
        options={
            CONF_PLANNING_ENABLED: False,
            CONF_ACTION_EMISSION_ENABLED: False,
        },
        subentries_data=[
            config_entries.ConfigSubentryData(
                subentry_id="battery_sub",
                subentry_type=SUBENTRY_TYPE_BATTERY,
                title="battery",
                unique_id="battery:battery",
                data={
                    CONF_NAME: "battery",
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
            ),
            config_entries.ConfigSubentryData(
                subentry_id="comfort_sub",
                subentry_type=SUBENTRY_TYPE_COMFORT,
                title="comfort",
                unique_id="comfort:comfort",
                data={
                    CONF_NAME: "comfort",
                    CONF_ROLLING_WINDOW_HOURS: 4,
                    CONF_TARGET_ON_HOURS_PER_WINDOW: 1,
                    CONF_MIN_CONSECUTIVE_ON_MINUTES: 60,
                    CONF_MIN_CONSECUTIVE_OFF_MINUTES: 60,
                    CONF_MAX_CONSECUTIVE_OFF_MINUTES: 120,
                    CONF_ON_OFF_SOURCE: "binary_sensor.comfort_on_off",
                    CONF_EXPECTED_POWER_KW: 1.2,
                },
            ),
            config_entries.ConfigSubentryData(
                subentry_id="optional_sub",
                subentry_type=SUBENTRY_TYPE_OPTIONAL,
                title="optional",
                unique_id="optional:optional",
                data={
                    CONF_NAME: "optional",
                    CONF_DURATION_MINUTES: 60,
                    CONF_RUN_WITHIN_HOURS: 3,
                    CONF_ENERGY_KWH: 1.5,
                    CONF_OPTIONS_COUNT: 2,
                    CONF_MIN_OPTION_GAP_MINUTES: 60,
                },
            ),
        ],
    )
    entry.add_to_hass(hass)

    hass.states.async_set("sensor.battery_soc", "5.0")
    hass.states.async_set("binary_sensor.comfort_on_off", "off")

    with patch(
        "homeassistant.helpers.entity.Entity.entity_registry_enabled_default",
        return_value=True,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    with patch("custom_components.wattplan.coordinator.optimize", side_effect=_fake_optimize):
        await hass.services.async_call(
            DOMAIN, SERVICE_RUN_OPTIMIZE_NOW, {}, blocking=True
        )
        await hass.async_block_till_done()

    state = hass.states.get("sensor.home_plan_details")
    assert state is not None
    assert state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE)
    assert "T" in state.state
    assert state.attributes["slot_minutes"] == 60
    assert state.attributes["slots"] == 4
    assert len(state.attributes["grid_import_price_per_kwh"]) == 4
    assert len(state.attributes["grid_export_price_per_kwh"]) == 4
    assert state.attributes["grid_export_price_per_kwh"] == [0.0, 0.0, 0.0, 0.0]
    assert len(state.attributes["usage_kwh"]) == 4
    assert len(state.attributes["solar_input_kwh"]) == 4
    assert len(state.attributes["projected_cost"]) == 4
    assert len(state.attributes["projected_savings_cost"]) == 4
    assert len(state.attributes["projected_savings_pct"]) == 4
    assert len(state.attributes["battery_battery_action"]) == 4
    assert len(state.attributes["battery_battery_level_kwh"]) == 4
    assert len(state.attributes["comfort_comfort_enabled"]) == 4
    assert len(state.attributes["optional_optional_enabled"]) == 4
    assert state.attributes["battery_battery_action"] == ["gc", "sc", "sc", "sc"]
    assert state.attributes["comfort_comfort_enabled"] == [True, False, False, True]
    assert state.attributes["optional_optional_enabled"] == [False, True, True, False]
    assert "timings" not in state.attributes

    duration_state = hass.states.get("sensor.home_last_run_duration")
    assert duration_state is not None
    timings = duration_state.attributes["timings"]
    assert isinstance(timings, list)
    assert [entry[0] for entry in timings] == [
        "Import price source fetch",
        "Usage source fetch",
        "PV source fetch",
        "Optimizer plan calculation",
        "Plan details payload build",
        "total",
    ]
    assert all(isinstance(entry, list | tuple) and len(entry) == 2 for entry in timings)
    assert all(isinstance(entry[1], int) for entry in timings)

    hourly_state = hass.states.get("sensor.home_plan_details_hourly")
    assert hourly_state is not None
    assert "timings" not in hourly_state.attributes
    assert duration_state.attributes["timings"] == timings


async def test_plan_details_timings_omit_unconfigured_sources(
    hass: HomeAssistant,
) -> None:
    """Timing entries should only include sources that were actually configured."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data={
            CONF_NAME: "Home",
            CONF_SLOT_MINUTES: 60,
            CONF_HOURS_TO_PLAN: 4,
            CONF_SOURCES: {
                CONF_SOURCE_IMPORT_PRICE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: "{{ [0.2, 0.25, 0.3, 0.35] }}",
                },
                CONF_SOURCE_USAGE: {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED},
                CONF_SOURCE_PV: {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED},
            },
        },
        options={
            CONF_PLANNING_ENABLED: False,
            CONF_ACTION_EMISSION_ENABLED: False,
        },
    )
    entry.add_to_hass(hass)

    with patch(
        "homeassistant.helpers.entity.Entity.entity_registry_enabled_default",
        return_value=True,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    with patch("custom_components.wattplan.coordinator.optimize", side_effect=_fake_optimize):
        await hass.services.async_call(
            DOMAIN, SERVICE_RUN_OPTIMIZE_NOW, {}, blocking=True
        )
        await hass.async_block_till_done()

    state = hass.states.get("sensor.home_plan_details")
    assert state is not None
    assert "timings" not in state.attributes
    duration_state = hass.states.get("sensor.home_last_run_duration")
    assert duration_state is not None
    tasks = [entry[0] for entry in duration_state.attributes["timings"]]
    assert "Import price source fetch" in tasks
    assert "Export price source fetch" not in tasks
    assert "Usage source fetch" not in tasks
    assert "PV source fetch" not in tasks
    assert tasks[-2:] == ["Plan details payload build", "total"]


async def test_plan_details_timings_keep_merged_source_as_single_source_entry(
    hass: HomeAssistant,
) -> None:
    """Merged sources should still expose one public source timing entry."""
    start_at = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data={
            CONF_NAME: "Home",
            CONF_SLOT_MINUTES: 60,
            CONF_HOURS_TO_PLAN: 4,
            CONF_SOURCES: {
                CONF_SOURCE_IMPORT_PRICE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_ENTITY_ADAPTER,
                    CONF_PROVIDERS: [
                        {
                            CONF_SOURCE_MODE: SOURCE_MODE_ENTITY_ADAPTER,
                            "entity_id": "sensor.prices_today",
                            CONF_ADAPTER_TYPE: ADAPTER_TYPE_ATTRIBUTE_OBJECTS,
                            CONF_NAME: "prices",
                            CONF_TIME_KEY: "start",
                            CONF_VALUE_KEY: "value",
                        },
                        {
                            CONF_SOURCE_MODE: SOURCE_MODE_ENTITY_ADAPTER,
                            "entity_id": "sensor.prices_tomorrow",
                            CONF_ADAPTER_TYPE: ADAPTER_TYPE_ATTRIBUTE_OBJECTS,
                            CONF_NAME: "prices",
                            CONF_TIME_KEY: "start",
                            CONF_VALUE_KEY: "value",
                        },
                    ],
                },
                CONF_SOURCE_USAGE: {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED},
                CONF_SOURCE_PV: {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED},
            },
        },
        options={
            CONF_PLANNING_ENABLED: False,
            CONF_ACTION_EMISSION_ENABLED: False,
        },
    )
    entry.add_to_hass(hass)
    hass.states.async_set(
        "sensor.prices_today",
        "ok",
        {
            "prices": [
                {"start": start_at.isoformat(), "value": 0.2},
                {"start": (start_at + timedelta(hours=1)).isoformat(), "value": 0.25},
            ]
        },
    )
    hass.states.async_set(
        "sensor.prices_tomorrow",
        "ok",
        {
            "prices": [
                {"start": (start_at + timedelta(hours=2)).isoformat(), "value": 0.3},
                {"start": (start_at + timedelta(hours=3)).isoformat(), "value": 0.35},
            ]
        },
    )

    with patch(
        "homeassistant.helpers.entity.Entity.entity_registry_enabled_default",
        return_value=True,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    with patch("custom_components.wattplan.coordinator.optimize", side_effect=_fake_optimize):
        await hass.services.async_call(
            DOMAIN, SERVICE_RUN_OPTIMIZE_NOW, {}, blocking=True
        )
        await hass.async_block_till_done()

    state = hass.states.get("sensor.home_plan_details")
    assert state is not None
    assert "timings" not in state.attributes
    duration_state = hass.states.get("sensor.home_last_run_duration")
    assert duration_state is not None
    tasks = [entry[0] for entry in duration_state.attributes["timings"]]
    assert tasks.count("Import price source fetch") == 1
    assert all("provider" not in task for task in tasks)


async def test_battery_target_changes_plan_and_expires_after_deadline(
    hass: HomeAssistant,
) -> None:
    """Targets should affect planning until their deadline, then disappear."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data={
            CONF_NAME: "Home",
            CONF_SLOT_MINUTES: 60,
            CONF_HOURS_TO_PLAN: 4,
            CONF_SOURCES: {
                CONF_SOURCE_IMPORT_PRICE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: "{{ [0.2, 0.2, 0.2, 0.2] }}",
                },
                CONF_SOURCE_USAGE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: "{{ [0.0, 0.0, 0.0, 0.0] }}",
                },
                CONF_SOURCE_PV: {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED},
            },
        },
        options={
            CONF_PLANNING_ENABLED: False,
            CONF_ACTION_EMISSION_ENABLED: False,
        },
        subentries_data=[
            config_entries.ConfigSubentryData(
                subentry_id="battery_sub",
                subentry_type=SUBENTRY_TYPE_BATTERY,
                title="battery",
                unique_id="battery:battery",
                data={
                    CONF_NAME: "battery",
                    CONF_SOC_SOURCE: "sensor.battery_soc",
                    CONF_CAPACITY_KWH: 10.0,
                    CONF_MINIMUM_KWH: 1.0,
                    CONF_MAX_CHARGE_KW: 3.0,
                    CONF_MAX_DISCHARGE_KW: 3.0,
                    CONF_CHARGE_EFFICIENCY: 0.9,
                    CONF_DISCHARGE_EFFICIENCY: 0.9,
                    CONF_CAN_CHARGE_FROM_GRID: True,
                    CONF_CAN_CHARGE_FROM_PV: False,
                },
            )
        ],
    )
    entry.add_to_hass(hass)
    hass.states.async_set("sensor.battery_soc", "5.0")

    with patch(
        "custom_components.wattplan.coordinator.optimize",
        side_effect=_fake_optimize_with_target_behavior,
    ):
        with patch(
            "homeassistant.helpers.entity.Entity.entity_registry_enabled_default",
            return_value=True,
        ):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

        await hass.services.async_call(DOMAIN, SERVICE_RUN_OPTIMIZE_NOW, {}, blocking=True)
        await hass.async_block_till_done()

        plan_details = hass.states.get("sensor.home_plan_details")
        assert plan_details is not None
        assert plan_details.attributes["battery_battery_action"] == [
            "sc",
            "sc",
            "sc",
            "sc",
        ]

        target_at = dt_util.utcnow() + timedelta(hours=2)
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_TARGET,
            {
                "battery": "battery",
                "soc_kwh": 8.0,
                "reach_at": target_at,
            },
            blocking=True,
        )
        await hass.services.async_call(DOMAIN, SERVICE_RUN_OPTIMIZE_NOW, {}, blocking=True)
        await hass.async_block_till_done()

        target_sensor = hass.states.get("sensor.home_battery_target")
        assert target_sensor is not None
        assert float(target_sensor.state) == 8.0

        plan_details = hass.states.get("sensor.home_plan_details")
        assert plan_details is not None
        assert plan_details.attributes["battery_battery_action"] == [
            "gc",
            "gc",
            "sc",
            "sc",
        ]

        expired_at = target_at + timedelta(minutes=1)
        async_fire_time_changed(hass, expired_at)
        await hass.async_block_till_done()

        with patch(
            "custom_components.wattplan.target_runtime.dt_util.utcnow",
            return_value=expired_at,
        ):
            await hass.services.async_call(
                DOMAIN, SERVICE_RUN_OPTIMIZE_NOW, {}, blocking=True
            )
            await hass.async_block_till_done()

            target_sensor = hass.states.get("sensor.home_battery_target")
            assert target_sensor is not None
            assert target_sensor.state == STATE_UNKNOWN
            assert target_sensor.attributes["by"] == "not_set"

            plan_details = hass.states.get("sensor.home_plan_details")
            assert plan_details is not None
            assert plan_details.attributes["battery_battery_action"] == [
                "sc",
                "sc",
                "sc",
                "sc",
            ]


async def test_clear_target_service_removes_active_battery_target(
    hass: HomeAssistant,
) -> None:
    """Clear target should immediately unset the target entity."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data={
            CONF_NAME: "Home",
            CONF_SLOT_MINUTES: 60,
            CONF_HOURS_TO_PLAN: 4,
            CONF_SOURCES: {
                CONF_SOURCE_IMPORT_PRICE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: "{{ [0.2, 0.2, 0.2, 0.2] }}",
                },
                CONF_SOURCE_USAGE: {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED},
                CONF_SOURCE_PV: {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED},
            },
        },
        options={
            CONF_PLANNING_ENABLED: False,
            CONF_ACTION_EMISSION_ENABLED: False,
        },
        subentries_data=[
            config_entries.ConfigSubentryData(
                subentry_id="battery_sub",
                subentry_type=SUBENTRY_TYPE_BATTERY,
                title="battery",
                unique_id="battery:battery",
                data={
                    CONF_NAME: "battery",
                    CONF_SOC_SOURCE: "sensor.battery_soc",
                    CONF_CAPACITY_KWH: 10.0,
                    CONF_MINIMUM_KWH: 1.0,
                    CONF_MAX_CHARGE_KW: 3.0,
                    CONF_MAX_DISCHARGE_KW: 3.0,
                    CONF_CHARGE_EFFICIENCY: 0.9,
                    CONF_DISCHARGE_EFFICIENCY: 0.9,
                    CONF_CAN_CHARGE_FROM_GRID: True,
                    CONF_CAN_CHARGE_FROM_PV: False,
                },
            )
        ],
    )
    entry.add_to_hass(hass)
    hass.states.async_set("sensor.battery_soc", "5.0")

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_TARGET,
        {
            "battery": "battery",
            "soc_kwh": 8.0,
            "reach_at": dt_util.utcnow() + timedelta(hours=2),
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_CLEAR_TARGET,
        {"battery": "battery"},
        blocking=True,
    )
    await hass.async_block_till_done()

    target_sensor = hass.states.get("sensor.home_battery_target")
    assert target_sensor is not None
    assert target_sensor.state == STATE_UNKNOWN
    assert target_sensor.attributes["by"] == "not_set"


async def test_button_entities_registered_and_pressable(hass: HomeAssistant) -> None:
    """Button entities appear in registry and pressing triggers a plan cycle."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data={
            CONF_NAME: "Home",
            CONF_SLOT_MINUTES: 60,
            CONF_HOURS_TO_PLAN: 4,
            CONF_SOURCES: {
                CONF_SOURCE_IMPORT_PRICE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: "{{ [0.2, 0.25, 0.3, 0.35] }}",
                },
                CONF_SOURCE_USAGE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: "{{ [1.0, 1.1, 1.0, 0.9] }}",
                },
            },
        },
        options={
            CONF_PLANNING_ENABLED: False,
            CONF_ACTION_EMISSION_ENABLED: False,
        },
    )
    entry.add_to_hass(hass)

    with patch("custom_components.wattplan.coordinator.optimize", side_effect=_fake_optimize):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        entity_registry = er.async_get(hass)
        optimize_entry = entity_registry.async_get("button.home_run_optimize_now")
        refresh_entry = entity_registry.async_get("button.home_refresh_sensors")
        assert optimize_entry is not None, "Run Optimize Now button not found in entity registry"
        assert refresh_entry is not None, "Refresh Sensors button not found in entity registry"

        assert optimize_entry.entity_category == EntityCategory.DIAGNOSTIC
        assert refresh_entry.entity_category == EntityCategory.DIAGNOSTIC

        coordinator = entry.runtime_data.coordinator
        snapshot_before = coordinator.snapshot

        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": "button.home_run_optimize_now"},
            blocking=True,
        )
        await hass.async_block_till_done()

        assert coordinator.snapshot is not None, "Snapshot should exist after pressing Run Optimize Now"
        assert coordinator.snapshot is not snapshot_before, "Pressing button should produce a new snapshot"


async def test_button_optimize_raises_when_already_running(hass: HomeAssistant) -> None:
    """Pressing the optimize button while a plan is running raises ServiceValidationError."""
    from homeassistant.exceptions import ServiceValidationError as HAServiceValidationError

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data={
            CONF_NAME: "Home",
            CONF_SLOT_MINUTES: 60,
            CONF_HOURS_TO_PLAN: 4,
            CONF_SOURCES: {
                CONF_SOURCE_IMPORT_PRICE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: "{{ [0.2, 0.25, 0.3, 0.35] }}",
                },
                CONF_SOURCE_USAGE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: "{{ [1.0, 1.1, 1.0, 0.9] }}",
                },
            },
        },
        options={
            CONF_PLANNING_ENABLED: False,
            CONF_ACTION_EMISSION_ENABLED: False,
        },
    )
    entry.add_to_hass(hass)

    with patch("custom_components.wattplan.coordinator.optimize", side_effect=_fake_optimize):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator

    async with coordinator._plan_lock:
        with pytest.raises(HAServiceValidationError):
            await coordinator.async_plan(trigger=CycleTrigger.SERVICE)
