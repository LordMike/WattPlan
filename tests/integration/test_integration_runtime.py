"""Integration runtime test for WattPlan planning and emission."""

from __future__ import annotations

import asyncio
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
    CONF_FIXUP_PROFILE,
    CONF_HOURS_TO_PLAN,
    CONF_HISTORICAL_COST_TRACKING_ENABLED,
    CONF_HISTORICAL_GRID_EXPORT_SENSOR,
    CONF_HISTORICAL_GRID_IMPORT_SENSOR,
    CONF_HISTORICAL_PV_SENSOR,
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
    FIXUP_PROFILE_STRICT,
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
    FLAG_GAP,
    FLAG_METER_RESET,
    FLAG_MISSING_IMPORT_PRICE,
    FLAG_MISSING_METER,
    HistoricalMetric,
    SCENARIO_ACTUAL,
    SlotRecord,
)
from custom_components.wattplan.historical_cost.store import HistoricalCostStore
from custom_components.wattplan.historical_cost.tracker import HistoricalCostTracker
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
from homeassistant.exceptions import ServiceValidationError
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


@pytest.mark.parametrize("startup_minute", [0, 7])
async def test_historical_cost_tracking_processes_first_observed_partial_slot(
    hass: HomeAssistant,
    freezer,
    startup_minute: int,
) -> None:
    """The startup reading should be the current bucket's measured baseline."""
    start = datetime(2026, 5, 24, 10, 0, tzinfo=UTC)
    freezer.move_to(start + timedelta(minutes=startup_minute))
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data={
            CONF_NAME: "Home",
            CONF_SLOT_MINUTES: 15,
            CONF_HOURS_TO_PLAN: 1,
            CONF_SOURCES: {
                CONF_SOURCE_IMPORT_PRICE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: "{{ [1.0, 3.0, 1.0, 1.0] }}",
                },
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
    assert tracker.store.data["days"] == {}
    actual = hass.states.get("sensor.home_historical_actual_cost_today")
    assert actual is not None
    assert float(actual.state) == pytest.approx(0.0)
    assert tracker.store.last_processed_slot() == start
    assert tracker.store.data["meter_cursor_seeded"] is True

    tracker.remember_price_series(
        start_at=start,
        slot_minutes=15,
        import_prices=[1.0, 3.0],
        export_prices=[0.0, 0.0],
    )

    freezer.move_to(start + timedelta(minutes=15, seconds=2))
    _set_energy_meter(hass, "sensor.grid_import_total", 101.0)
    _set_energy_meter(hass, "sensor.usage_total", 201.5)
    await hass.services.async_call(DOMAIN, SERVICE_REFRESH_SENSORS, {}, blocking=True)
    await hass.async_block_till_done()

    day = tracker.store.data["days"]["2026-05-24"]
    assert day["starts"] == ["2026-05-24T10:00:00Z"]
    assert day["import_price"] == pytest.approx([1.0])
    assert day["grid_import"] == pytest.approx([1.0])
    assert day["usage"] == pytest.approx([1.5])
    assert tracker.store.last_processed_slot() == start
    assert "meter_cursor_seeded" not in tracker.store.data
    actual = hass.states.get("sensor.home_historical_actual_cost_today")
    assert actual is not None
    assert float(actual.state) == pytest.approx(1.0)

    freezer.move_to(start + timedelta(minutes=30, seconds=2))
    _set_energy_meter(hass, "sensor.grid_import_total", 103.0)
    _set_energy_meter(hass, "sensor.usage_total", 202.0)
    await hass.services.async_call(DOMAIN, SERVICE_REFRESH_SENSORS, {}, blocking=True)
    await hass.async_block_till_done()

    assert day["starts"] == [
        "2026-05-24T10:00:00Z",
        "2026-05-24T10:15:00Z",
    ]
    assert day["import_price"] == pytest.approx([1.0, 3.0])
    assert day["grid_import"] == pytest.approx([1.0, 2.0])
    assert day["usage"] == pytest.approx([1.5, 0.5])
    assert tracker.store.last_processed_slot() == start + timedelta(minutes=15)
    assert tracker.store.last_meter_values() == {
        "grid_import": pytest.approx(103.0),
        "grid_export": pytest.approx(0.0),
        "usage": pytest.approx(202.0),
        "pv": pytest.approx(0.0),
    }
    actual = hass.states.get("sensor.home_historical_actual_cost_today")
    grid_only = hass.states.get("sensor.home_historical_grid_only_cost_today")
    assert actual is not None
    assert grid_only is not None
    assert float(actual.state) == pytest.approx(7.0)
    assert float(grid_only.state) == pytest.approx(3.0)

    await hass.services.async_call(DOMAIN, SERVICE_REFRESH_SENSORS, {}, blocking=True)
    assert day["starts"] == [
        "2026-05-24T10:00:00Z",
        "2026-05-24T10:15:00Z",
    ]
    assert tracker.store.last_meter_values()["grid_import"] == pytest.approx(103.0)


async def test_historical_cost_startup_missed_boundary_keeps_gap_policy(
    hass: HomeAssistant,
    freezer,
) -> None:
    """Missing the first boundary should mark gaps, reseed, and resume normally."""
    start = datetime(2026, 5, 24, 10, 0, tzinfo=UTC)
    freezer.move_to(start + timedelta(minutes=7))
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data={
            CONF_NAME: "Home",
            CONF_SLOT_MINUTES: 15,
            CONF_HOURS_TO_PLAN: 1,
            CONF_SOURCES: {
                CONF_SOURCE_IMPORT_PRICE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: "{{ [2.0, 2.0, 2.0, 2.0] }}",
                },
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

    freezer.move_to(start + timedelta(minutes=31))
    _set_energy_meter(hass, "sensor.grid_import_total", 105.0)
    _set_energy_meter(hass, "sensor.usage_total", 205.0)
    await hass.services.async_call(DOMAIN, SERVICE_REFRESH_SENSORS, {}, blocking=True)

    day = tracker.store.data["days"]["2026-05-24"]
    assert day["starts"] == [
        "2026-05-24T10:00:00Z",
        "2026-05-24T10:15:00Z",
    ]
    assert day["flags"] == [FLAG_GAP, FLAG_GAP]
    assert day["grid_import"] == [None, None]
    assert day["usage"] == [None, None]
    assert tracker.store.last_processed_slot() == start + timedelta(minutes=15)
    assert tracker.store.last_meter_values()["grid_import"] == pytest.approx(105.0)
    assert "meter_cursor_seeded" not in tracker.store.data

    tracker.remember_price_series(
        start_at=start + timedelta(minutes=30),
        slot_minutes=15,
        import_prices=[2.0],
        export_prices=[0.0],
    )
    freezer.move_to(start + timedelta(minutes=45, seconds=2))
    _set_energy_meter(hass, "sensor.grid_import_total", 105.5)
    _set_energy_meter(hass, "sensor.usage_total", 205.25)
    await hass.services.async_call(DOMAIN, SERVICE_REFRESH_SENSORS, {}, blocking=True)
    await hass.async_block_till_done()

    assert day["starts"] == [
        "2026-05-24T10:00:00Z",
        "2026-05-24T10:15:00Z",
        "2026-05-24T10:30:00Z",
    ]
    assert day["flags"] == [FLAG_GAP, FLAG_GAP, 0]
    assert day["grid_import"] == [None, None, pytest.approx(0.5)]
    assert day["usage"] == [None, None, pytest.approx(0.25)]
    actual = hass.states.get("sensor.home_historical_actual_cost_today")
    assert actual is not None
    assert float(actual.state) == pytest.approx(1.0)
    assert actual.attributes["slots"] == 3
    assert actual.attributes["missing_slots"] == 2


async def test_historical_cost_tracking_processes_scenarios_and_entities(
    hass: HomeAssistant,
    freezer,
) -> None:
    """Historical tracker should aggregate actual, grid-only, and self-consumption costs."""
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
    grid_only = hass.states.get("sensor.home_historical_grid_only_cost_today")
    self_consumption = hass.states.get(
        "sensor.home_historical_self_consumption_cost_today"
    )
    savings = hass.states.get("sensor.home_historical_savings_vs_grid_only_today")
    self_savings = hass.states.get(
        "sensor.home_historical_savings_vs_self_consumption_today"
    )

    assert actual is not None
    assert float(actual.state) == pytest.approx(0.98)
    assert actual.attributes["slots"] == 1
    assert actual.attributes["missing_slots"] == 0
    assert actual.attributes["scenario"] == "actual"
    assert grid_only is not None
    assert float(grid_only.state) == pytest.approx(1.5)
    assert self_consumption is not None
    assert float(self_consumption.state) == pytest.approx(0.0)
    assert self_consumption.attributes["simulated_soc_kwh_by_battery"] == {
        "battery_sub": pytest.approx(0.5)
    }
    assert self_consumption.attributes["simulated_soc_percent_by_battery"] == {
        "battery_sub": pytest.approx(5.0)
    }
    assert self_consumption.attributes["simulation_soc_seeded_from_real_soc"] is True
    assert self_consumption.attributes["simulation_soc_resyncs_each_slot"] is False
    assert savings is not None
    assert float(savings.state) == pytest.approx(0.52)
    assert self_savings is not None
    assert self_savings.attributes["simulated_soc_kwh_by_battery"] == {
        "battery_sub": pytest.approx(0.5)
    }
    assert hass.states.get("sensor.home_historical_no_battery_cost_today") is None
    assert (
        hass.states.get("sensor.home_historical_savings_vs_no_battery_today") is None
    )

    entity_registry = er.async_get(hass)
    monthly = entity_registry.async_get(
        "sensor.home_historical_actual_cost_this_month"
    )
    assert monthly is not None
    assert monthly.disabled
    monthly_grid_only = entity_registry.async_get(
        "sensor.home_historical_grid_only_cost_this_month"
    )
    assert monthly_grid_only is not None
    assert monthly_grid_only.disabled


@pytest.mark.parametrize(
    "missing_pv",
    [
        False,
        pytest.param(True, marks=pytest.mark.xfail(
            strict=True,
            reason="G1: missing PV freezes reference SoC and creates false negative savings",
        )),
    ],
)
async def test_identical_self_consumption_execution_has_zero_reported_savings(
    hass: HomeAssistant, freezer, missing_pv: bool,
) -> None:
    """A fixed self-consumption policy must not lose to itself after a meter gap.

    Physical trace: initial battery 1 kWh; no PV; load 1 kWh in each
    of three hours. Both actual and reference discharge in hour 1, then
    import 1 kWh in hours 2 and 3. At unit tariff every slot's savings
    must be zero, including any subset retained after missing readings.
    No optimizer choices participate in this reference calculation.
    """
    start = datetime(2026, 9, 1, 12, tzinfo=UTC)
    freezer.move_to(start)
    entry = MockConfigEntry(
        domain=DOMAIN, title="Home",
        data={
            CONF_NAME: "Home", CONF_SLOT_MINUTES: 60, CONF_HOURS_TO_PLAN: 4,
            CONF_SOURCES: {
                CONF_SOURCE_IMPORT_PRICE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: "{{ [1.0, 1.0, 1.0, 1.0] }}",
                },
                CONF_SOURCE_EXPORT_PRICE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: "{{ [0.0, 0.0, 0.0, 0.0] }}",
                },
            },
        },
        options=_historical_options(),
        subentries_data=[config_entries.ConfigSubentryData(
            subentry_id="battery_sub", subentry_type=SUBENTRY_TYPE_BATTERY,
            title="battery", unique_id="battery:battery",
            data={
                CONF_NAME: "battery", CONF_SOC_SOURCE: "sensor.battery_soc",
                CONF_CAPACITY_KWH: 1.0, CONF_MINIMUM_KWH: 0.0,
                CONF_MAX_CHARGE_KW: 1.0, CONF_MAX_DISCHARGE_KW: 1.0,
                CONF_CHARGE_EFFICIENCY: 1.0, CONF_DISCHARGE_EFFICIENCY: 1.0,
                CONF_CAN_CHARGE_FROM_GRID: False, CONF_CAN_CHARGE_FROM_PV: True,
            },
        )],
    )
    entry.add_to_hass(hass)
    for entity_id in ("sensor.grid_import_total", "sensor.grid_export_total",
                      "sensor.usage_total", "sensor.pv_total"):
        _set_energy_meter(hass, entity_id, 0.0)
    hass.states.async_set("sensor.battery_soc", "1.0",
                         {"unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    tracker = entry.runtime_data.historical_tracker
    assert tracker is not None
    tracker.store.update_metadata(
        last_processed_slot=start - timedelta(hours=1),
        last_meter_values=dict(grid_import=0., grid_export=0., usage=0., pv=0.),
    )
    tracker.store.data.pop("meter_cursor_seeded", None)
    tracker.store.update_simulation_soc({"battery_sub": 1.0})
    for hour in (1, 2, 3):
        now = start + timedelta(hours=hour, seconds=1)
        freezer.move_to(now)
        _set_energy_meter(hass, "sensor.grid_import_total", float(hour - 1))
        _set_energy_meter(hass, "sensor.usage_total", float(hour))
        _set_energy_meter(hass, "sensor.pv_total",
                          float("nan") if missing_pv and hour == 1 else 0.0)
        hass.states.async_set("sensor.battery_soc", "0.0",
                             {"unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR})
        await tracker.async_process_completed_slot(now)
        await hass.async_block_till_done()
    sensor = hass.states.get("sensor.home_historical_savings_vs_self_consumption_today")
    assert sensor is not None
    assert sensor.attributes["slots"] == 3
    assert sensor.attributes["missing_slots"] == (2 if missing_pv else 0)
    assert float(sensor.state) == pytest.approx(0.0)


async def test_refresh_sensors_service_processes_historical_costs(
    hass: HomeAssistant,
    freezer,
) -> None:
    """Manual sensor refresh should process due historical cost slots."""
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
    )
    entry.add_to_hass(hass)
    _set_energy_meter(hass, "sensor.grid_import_total", 100.0)
    _set_energy_meter(hass, "sensor.grid_export_total", 10.0)
    _set_energy_meter(hass, "sensor.usage_total", 200.0)
    _set_energy_meter(hass, "sensor.pv_total", 50.0)

    with patch(
        "custom_components.wattplan.coordinator.optimize",
        side_effect=_fake_optimize,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await entry.runtime_data.coordinator.async_plan(trigger=CycleTrigger.SERVICE)

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

    _set_energy_meter(hass, "sensor.grid_import_total", 101.0)
    _set_energy_meter(hass, "sensor.grid_export_total", 10.2)
    _set_energy_meter(hass, "sensor.usage_total", 201.5)
    _set_energy_meter(hass, "sensor.pv_total", 51.0)

    await hass.services.async_call(DOMAIN, SERVICE_REFRESH_SENSORS, {}, blocking=True)
    await hass.async_block_till_done()

    actual = hass.states.get("sensor.home_historical_actual_cost_today")
    assert actual is not None
    assert float(actual.state) == pytest.approx(0.98)


async def test_overlapping_historical_refresh_paths_process_each_slot_once(
    hass: HomeAssistant,
    freezer,
) -> None:
    """Timer, coordinator, and service refreshes should share one slot transaction."""
    start = datetime(2026, 5, 24, 12, 0, tzinfo=UTC)
    first_refresh = start + timedelta(hours=1, seconds=2)
    freezer.move_to(first_refresh)
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

    price_lookup_started = asyncio.Event()
    release_price_lookup = asyncio.Event()
    price_lookups: list[tuple[str, datetime]] = []

    async def _delayed_price(source_key: str, slot_start: datetime) -> float:
        price_lookups.append((source_key, slot_start))
        if not price_lookup_started.is_set():
            price_lookup_started.set()
            await release_price_lookup.wait()
        return 1.0 if source_key == CONF_SOURCE_IMPORT_PRICE else 0.1

    with patch.object(tracker, "_async_price", side_effect=_delayed_price):
        timer_refresh = asyncio.create_task(tracker._async_timer(first_refresh))
        await price_lookup_started.wait()
        coordinator_refresh = asyncio.create_task(
            entry.runtime_data.coordinator.async_tick(trigger=CycleTrigger.SCHEDULE)
        )
        service_refresh = asyncio.create_task(
            hass.services.async_call(
                DOMAIN,
                SERVICE_REFRESH_SENSORS,
                {},
                blocking=True,
            )
        )
        await asyncio.sleep(0)
        release_price_lookup.set()
        await asyncio.gather(timer_refresh, coordinator_refresh, service_refresh)
        await hass.async_block_till_done()

        day = tracker.store.data["days"]["2026-05-24"]
        assert day["starts"] == ["2026-05-24T12:00:00Z"]
        assert day["grid_import"] == pytest.approx([1.0])
        assert day["grid_export"] == pytest.approx([0.2])
        assert day["usage"] == pytest.approx([1.5])
        assert day["pv"] == pytest.approx([1.0])
        assert tracker.store.simulation_soc() == {"battery_sub": pytest.approx(0.5)}
        assert tracker.store.last_processed_slot() == start
        assert tracker.store.last_meter_values() == {
            "grid_import": pytest.approx(101.0),
            "grid_export": pytest.approx(10.2),
            "usage": pytest.approx(201.5),
            "pv": pytest.approx(51.0),
        }
        assert len(price_lookups) == 2
        actual = hass.states.get("sensor.home_historical_actual_cost_today")
        grid_only = hass.states.get("sensor.home_historical_grid_only_cost_today")
        self_consumption = hass.states.get(
            "sensor.home_historical_self_consumption_cost_today"
        )
        assert actual is not None
        assert grid_only is not None
        assert self_consumption is not None
        assert float(actual.state) == pytest.approx(0.98)
        assert float(grid_only.state) == pytest.approx(1.5)
        assert float(self_consumption.state) == pytest.approx(0.0)
        assert actual.attributes["slots"] == 1

        await hass.services.async_call(
            DOMAIN,
            SERVICE_REFRESH_SENSORS,
            {},
            blocking=True,
        )
        assert day["starts"] == ["2026-05-24T12:00:00Z"]
        assert tracker.store.simulation_soc() == {"battery_sub": pytest.approx(0.5)}
        assert len(price_lookups) == 2

        second_refresh = start + timedelta(hours=2, seconds=2)
        freezer.move_to(second_refresh)
        _set_energy_meter(hass, "sensor.grid_import_total", 101.4)
        _set_energy_meter(hass, "sensor.grid_export_total", 10.2)
        _set_energy_meter(hass, "sensor.usage_total", 202.0)
        _set_energy_meter(hass, "sensor.pv_total", 51.2)
        await entry.runtime_data.coordinator.async_tick(trigger=CycleTrigger.SCHEDULE)
        await hass.async_block_till_done()

    assert day["starts"] == [
        "2026-05-24T12:00:00Z",
        "2026-05-24T13:00:00Z",
    ]
    assert day["grid_import"] == pytest.approx([1.0, 0.4])
    assert day["grid_export"] == pytest.approx([0.2, 0.0])
    assert day["usage"] == pytest.approx([1.5, 0.5])
    assert day["pv"] == pytest.approx([1.0, 0.2])
    assert tracker.store.simulation_soc() == {"battery_sub": pytest.approx(0.2)}
    assert tracker.store.last_processed_slot() == start + timedelta(hours=1)
    assert tracker.store.last_meter_values() == {
        "grid_import": pytest.approx(101.4),
        "grid_export": pytest.approx(10.2),
        "usage": pytest.approx(202.0),
        "pv": pytest.approx(51.2),
    }
    assert len(price_lookups) == 4
    actual = hass.states.get("sensor.home_historical_actual_cost_today")
    grid_only = hass.states.get("sensor.home_historical_grid_only_cost_today")
    self_consumption = hass.states.get(
        "sensor.home_historical_self_consumption_cost_today"
    )
    assert actual is not None
    assert grid_only is not None
    assert self_consumption is not None
    assert float(actual.state) == pytest.approx(1.38)
    assert float(grid_only.state) == pytest.approx(2.0)
    assert float(self_consumption.state) == pytest.approx(0.0)
    assert actual.attributes["slots"] == 2


async def test_historical_cost_uses_cached_planner_prices_after_forecast_rolls(
    hass: HomeAssistant,
    freezer,
) -> None:
    """Completed historical slots should use retained prices from successful plans."""
    start = datetime(2026, 5, 24, 12, 0, tzinfo=UTC)
    freezer.move_to(start + timedelta(seconds=2))

    def _points(first: datetime, values: list[float]) -> list[dict[str, object]]:
        return [
            {
                "start": (first + timedelta(hours=index)).isoformat(),
                "value": value,
            }
            for index, value in enumerate(values)
        ]

    hass.states.async_set(
        "sensor.import_price_forecast",
        "ok",
        {"prices": _points(start, [1.0, 1.1, 1.2, 1.3])},
    )
    hass.states.async_set(
        "sensor.export_price_forecast",
        "ok",
        {"prices": _points(start, [0.1, 0.11, 0.12, 0.13])},
    )
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
                    "entity_id": "sensor.import_price_forecast",
                    CONF_ADAPTER_TYPE: ADAPTER_TYPE_ATTRIBUTE_OBJECTS,
                    CONF_NAME: "prices",
                    CONF_TIME_KEY: "start",
                    CONF_VALUE_KEY: "value",
                    CONF_FIXUP_PROFILE: FIXUP_PROFILE_STRICT,
                },
                CONF_SOURCE_EXPORT_PRICE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_ENTITY_ADAPTER,
                    "entity_id": "sensor.export_price_forecast",
                    CONF_ADAPTER_TYPE: ADAPTER_TYPE_ATTRIBUTE_OBJECTS,
                    CONF_NAME: "prices",
                    CONF_TIME_KEY: "start",
                    CONF_VALUE_KEY: "value",
                    CONF_FIXUP_PROFILE: FIXUP_PROFILE_STRICT,
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

    with patch(
        "custom_components.wattplan.coordinator.optimize",
        side_effect=_fake_optimize,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await entry.runtime_data.coordinator.async_plan(trigger=CycleTrigger.SERVICE)

    tracker = entry.runtime_data.historical_tracker
    assert tracker is not None
    assert tracker.store.cached_price(start, "import") == pytest.approx(1.0)
    assert tracker.store.cached_price(start, "export") == pytest.approx(0.1)

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
    hass.states.async_set(
        "sensor.import_price_forecast",
        "ok",
        {"prices": _points(start + timedelta(hours=1), [2.0, 2.1, 2.2, 2.3])},
    )
    hass.states.async_set(
        "sensor.export_price_forecast",
        "ok",
        {"prices": _points(start + timedelta(hours=1), [0.2, 0.21, 0.22, 0.23])},
    )
    _set_energy_meter(hass, "sensor.grid_import_total", 101.0)
    _set_energy_meter(hass, "sensor.grid_export_total", 10.2)
    _set_energy_meter(hass, "sensor.usage_total", 201.5)
    _set_energy_meter(hass, "sensor.pv_total", 51.0)

    await tracker.async_process_completed_slot(start + timedelta(hours=1, seconds=1))
    await hass.async_block_till_done()

    day = tracker.store.data["days"]["2026-05-24"]
    assert day["import_price"] == [1.0]
    assert day["export_price"] == [0.1]
    assert day["flags"] == [0]
    actual = hass.states.get("sensor.home_historical_actual_cost_today")
    assert actual is not None
    assert float(actual.state) == pytest.approx(0.98)


async def test_scheduled_tick_processes_historical_costs(
    hass: HomeAssistant,
    freezer,
) -> None:
    """Scheduled coordinator ticks should process due historical cost slots."""
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

    _set_energy_meter(hass, "sensor.grid_import_total", 101.0)
    _set_energy_meter(hass, "sensor.grid_export_total", 10.2)
    _set_energy_meter(hass, "sensor.usage_total", 201.5)
    _set_energy_meter(hass, "sensor.pv_total", 51.0)

    await entry.runtime_data.coordinator.async_tick(trigger=CycleTrigger.SCHEDULE)
    await hass.async_block_till_done()

    actual = hass.states.get("sensor.home_historical_actual_cost_today")
    assert actual is not None
    assert float(actual.state) == pytest.approx(0.98)


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


@pytest.mark.parametrize("invalid_state", ["nan", "inf", "-inf"])
async def test_historical_meter_nonfinite_recovery_requires_new_finite_baseline(
    hass: HomeAssistant,
    freezer,
    invalid_state: str,
) -> None:
    """Nonfinite readings must leave two missing slots before finite deltas resume."""
    start = datetime(2026, 5, 24, 10, 0, tzinfo=UTC)
    freezer.move_to(start)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data={
            CONF_NAME: "Home",
            CONF_SLOT_MINUTES: 15,
            CONF_HOURS_TO_PLAN: 1,
            CONF_SOURCES: {
                CONF_SOURCE_IMPORT_PRICE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: "{{ [1.0, 1.0, 1.0, 1.0] }}",
                },
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
    tracker.remember_price_series(
        start_at=start,
        slot_minutes=15,
        import_prices=[1.0] * 4,
        export_prices=[0.0] * 4,
    )

    for minutes, grid_import, usage in (
        (15, invalid_state, invalid_state),
        (30, 102.0, 203.0),
        (45, 103.0, 204.0),
    ):
        freezer.move_to(start + timedelta(minutes=minutes, seconds=2))
        _set_energy_meter(hass, "sensor.grid_import_total", grid_import)
        _set_energy_meter(hass, "sensor.usage_total", usage)
        await tracker.async_process_completed_slot()

    day = tracker.store.data["days"]["2026-05-24"]
    assert day["flags"] == [FLAG_MISSING_METER, FLAG_MISSING_METER, 0]
    assert day["grid_import"] == [None, None, pytest.approx(1.0)]
    assert day["usage"] == [None, None, pytest.approx(1.0)]
    assert tracker.store.last_meter_values()["grid_import"] == pytest.approx(103.0)
    summary = tracker.store.summary(
        metric=HistoricalMetric.COST,
        period="today",
        scenario=SCENARIO_ACTUAL,
        now=start + timedelta(hours=1),
    )
    assert summary.value == pytest.approx(1.0)
    assert summary.slots == 3
    assert summary.missing_slots == 2


async def test_historical_meter_delta_overflow_is_missing(hass: HomeAssistant) -> None:
    """Finite readings whose subtraction overflows must not create a valid delta."""
    tracker = type("Tracker", (), {})()
    tracker._meter_deltas = HistoricalCostTracker._meter_deltas.__get__(tracker)

    deltas, flags = tracker._meter_deltas(
        {"grid_import": -1e308, "grid_export": 0.0, "usage": 0.0, "pv": 0.0},
        {"grid_import": 1e308, "grid_export": 0.0, "usage": 0.0, "pv": 0.0},
    )

    assert deltas["grid_import"] is None
    assert flags == FLAG_MISSING_METER


async def test_historical_store_sanitizes_nonfinite_persisted_values(
    hass: HomeAssistant,
) -> None:
    """Load must preserve records but make corrupt numerics explicitly missing."""
    store = HistoricalCostStore(
        hass, entry_id="corrupt-entry", slot_minutes=60, currency="DKK"
    )
    starts = [f"2026-09-11T{hour:02d}:00:00Z" for hour in range(12, 16)]
    payload = {
        "version": 1,
        "slot_minutes": 60,
        "currency": "DKK",
        "tracking_started_at": starts[0],
        "last_processed_slot": starts[-1],
        "last_meter_values": {"grid_import": float("nan"), "usage": 4.0},
        "meter_config": {},
        "price_cache": {},
        "simulation_state": {
            "self_consumption": {
                "batteries": {
                    "bad": {"soc_kwh": float("inf")},
                    "good": {"soc_kwh": 1.0},
                }
            }
        },
        "days": {
            "2026-09-11": {
                "starts": starts,
                "import_price": [-1.0, float("nan"), 1e308, 1e308],
                "export_price": [0.0, 0.0, 0.0, 0.0],
                "grid_import": [1.0, 1.0, 1e308, 1e308],
                "grid_export": [0.0, 0.0, 0.0, 0.0],
                "usage": [1.0, 1.0, 1.0, 1.0],
                "pv": [0.0, 0.0, 0.0, 0.0],
                "self_consumption_grid_import": [1.0, 1.0, 1.0, 1.0],
                "self_consumption_grid_export": [0.0, 0.0, 0.0, 0.0],
                "flags": [0, 0, 0, 0],
            }
        },
    }
    with patch.object(store._store, "async_load", return_value=payload):
        await store.async_load()

    day = store.data["days"]["2026-09-11"]
    assert day["import_price"] == [-1.0, None, 1e308, 1e308]
    assert day["flags"] == [0, FLAG_MISSING_IMPORT_PRICE, 0, 0]
    assert store.last_meter_values() == {"grid_import": None, "usage": 4.0}
    assert store.simulation_soc() == {"good": 1.0}
    summary = store.summary(
        metric=HistoricalMetric.COST,
        period="today",
        scenario=SCENARIO_ACTUAL,
        now=datetime(2026, 9, 11, 18, 0, tzinfo=UTC),
    )
    assert summary.value == pytest.approx(-1.0)
    assert summary.slots == 4
    assert summary.missing_slots == 3

    store.append_slot(
        SlotRecord(
            start=datetime(2026, 9, 11, 16, 0, tzinfo=UTC),
            import_price=float("-inf"),
            export_price=0.0,
            grid_import=float("nan"),
            grid_export=0.0,
            usage=1.0,
            pv=0.0,
        )
    )
    assert day["import_price"][-1] is None
    assert day["grid_import"][-1] is None
    assert day["flags"][-1] == FLAG_MISSING_IMPORT_PRICE | FLAG_MISSING_METER


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
    store.data["price_cache"] = {
        "2026-03-01T12:00:00Z": {"import_price": 1.0, "export_price": 0.1},
        "2026-05-01T12:00:00Z": {"import_price": 1.0, "export_price": 0.1},
        "2026-05-25T12:00:00Z": {"import_price": 1.0, "export_price": 0.1},
    }

    store.prune(datetime(2026, 5, 24, 12, 0, tzinfo=UTC))

    assert "2026-03-01" not in store.data["days"]
    assert "2026-05-01" in store.data["days"]
    assert "2026-03-01T12:00:00Z" not in store.data["price_cache"]
    assert "2026-05-01T12:00:00Z" in store.data["price_cache"]
    assert "2026-05-25T12:00:00Z" in store.data["price_cache"]


async def test_historical_cost_store_migrates_missing_price_cache(
    hass: HomeAssistant,
) -> None:
    """Older historical stores should gain an empty retained price cache."""
    store = HistoricalCostStore(
        hass,
        entry_id="history-entry",
        slot_minutes=60,
        currency="DKK",
    )

    migrated = store._migrate(
        {
            "version": 1,
            "slot_minutes": 60,
            "currency": "DKK",
            "days": {},
            "last_meter_values": {},
            "meter_config": {},
            "simulation_state": {},
        }
    )

    assert migrated["price_cache"] == {}


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
    """Keep restored diagnostics but suppress actions until a fresh plan succeeds."""
    now = datetime.now(tz=UTC)
    plan_start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    plan_end = plan_start + timedelta(hours=4)
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
                "created_at": plan_start.isoformat(),
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
                        "span_start": plan_start.isoformat(),
                        "span_end": plan_end.isoformat(),
                    },
                },
                "action_schedules": {
                    "start_at": plan_start.isoformat(),
                    "slot_minutes": 60,
                    "batteries": {
                        "battery_sub": [
                            "grid_charge",
                            "preserve",
                            "self_consume",
                            "self_consume",
                        ]
                    },
                    "comforts": {"comfort_sub": ["on", "off", "on", "on"]},
                },
            },
            "last_success_at": plan_start.isoformat(),
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
    _assert_valid_state(hass, "sensor.home_last_run_duration")
    status = hass.states.get("sensor.home_status")
    assert status is not None
    assert status.state == "degraded"
    assert status.attributes["reason_codes"] == [
        "restored_plan_awaiting_validation"
    ]
    assert status.attributes["scheduler_stale"] is False
    assert status.attributes["action_recommendations_validated"] is False
    assert status.attributes["has_usable_plan"] is True
    assert hass.states.get("sensor.home_battery_action").state == STATE_UNAVAILABLE
    assert hass.states.get("sensor.home_comfort_action").state == STATE_UNAVAILABLE
    assert (
        hass.states.get("sensor.home_optional_next_start_option").state
        == STATE_UNAVAILABLE
    )

    battery_next = hass.states.get("sensor.home_battery_next_action")
    assert battery_next is not None
    assert battery_next.state == STATE_UNAVAILABLE
    comfort_next = hass.states.get("sensor.home_comfort_next_action")
    assert comfort_next is not None
    assert comfort_next.state == STATE_UNAVAILABLE

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

    coordinator = entry.runtime_data.coordinator
    expired_at = plan_end + timedelta(minutes=1)

    class ExpiredPlanDateTime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return expired_at if tz is not None else expired_at.replace(tzinfo=None)

    with patch(
        "custom_components.wattplan.coordinator_logic.source_status.datetime",
        ExpiredPlanDateTime,
    ):
        coordinator.async_update_listeners()
        await hass.async_block_till_done()
        status = hass.states.get("sensor.home_status")
        assert status is not None
        assert status.attributes["reason_codes"] == ["plan_stale"]
        assert status.attributes["scheduler_stale"] is False
        assert status.attributes["has_usable_plan"] is False
        _assert_valid_state(hass, "sensor.home_last_run_duration")

    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    with pytest.raises(ServiceValidationError, match="fresh successful plan"):
        await coordinator.async_emit(trigger=CycleTrigger.SERVICE)
    assert coordinator.action_recommendations_validated is False

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

    assert coordinator.action_recommendations_validated is False
    assert hass.states.get("sensor.home_battery_action").state == STATE_UNAVAILABLE
    assert (
        hass.states.get("sensor.home_optional_next_start_option").state
        == STATE_UNAVAILABLE
    )

    with patch("custom_components.wattplan.coordinator.optimize", _fake_optimize):
        await hass.services.async_call(
            DOMAIN, SERVICE_RUN_OPTIMIZE_NOW, {}, blocking=True
        )
        await hass.async_block_till_done()

    assert coordinator.action_recommendations_validated is True
    assert hass.states.get("sensor.home_battery_action").state == "grid_charge"
    assert hass.states.get("sensor.home_comfort_action").state == "on"
    _assert_valid_state(hass, "sensor.home_optional_next_start_option")

    payload = coordinator.restore_payload()
    assert payload is not None
    assert coordinator.async_restore_payload(payload)
    await hass.async_block_till_done()
    assert coordinator.action_recommendations_validated is False
    assert hass.states.get("sensor.home_battery_action").state == STATE_UNAVAILABLE
    assert (
        hass.states.get("sensor.home_optional_next_start_option").state
        == STATE_UNAVAILABLE
    )


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


async def test_failed_plan_advances_retained_actions_across_tariff_boundary(
    hass: HomeAssistant,
) -> None:
    """A failed replan should retain the schedule, not its first actions."""
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
                    CONF_TEMPLATE: "{{ [0.05, 0.4, 0.4, 0.4] }}",
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
        ],
    )
    entry.add_to_hass(hass)
    hass.states.async_set("sensor.battery_soc", "5.0")
    hass.states.async_set("binary_sensor.comfort_on_off", "off")

    with (
        patch(
            "homeassistant.helpers.entity.Entity.entity_registry_enabled_default",
            return_value=True,
        ),
        patch("custom_components.wattplan.coordinator.optimize") as optimize_mock,
    ):
        optimize_mock.return_value = {
            **_fake_optimize(None),
            "entities": [
                {
                    "name": "battery",
                    "type": "battery",
                    "schedule": [
                        {"state": "grid_charge", "level": 5.5},
                        {"state": "preserve", "level": 5.5},
                        {"state": "self_consume", "level": 5.0},
                        {"state": "self_consume", "level": 4.5},
                    ],
                },
                {
                    "name": "comfort",
                    "type": "comfort",
                    "schedule": [
                        {"enabled": True, "level": 1.0},
                        {"enabled": False, "level": 0.9},
                        {"enabled": True, "level": 1.0},
                        {"enabled": True, "level": 1.1},
                    ],
                },
            ],
            "optional_entity_options": [],
        }
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await hass.services.async_call(
            DOMAIN, SERVICE_RUN_OPTIMIZE_NOW, {}, blocking=True
        )
        await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    schedules = coordinator.snapshot.action_schedules
    plan_start = dt_util.parse_datetime(schedules["start_at"])
    assert plan_start is not None
    expensive_slot = plan_start + timedelta(hours=1, minutes=1)

    class ExpensiveSlotDateTime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return (
                expensive_slot
                if tz is not None
                else expensive_slot.replace(tzinfo=None)
            )

    with (
        patch(
            "custom_components.wattplan.coordinator_parts.snapshot.datetime",
            ExpensiveSlotDateTime,
        ),
        patch(
            "custom_components.wattplan.coordinator_logic.source_status.datetime",
            ExpensiveSlotDateTime,
        ),
        patch.object(
            coordinator,
            "_async_build_planning_request",
            side_effect=PlanningStageError(
                StageErrorKind.PLANNER_INPUT,
                "import_price source entity `sensor.missing` was not found",
            ),
        ),
    ):
        with pytest.raises(PlanningStageError):
            await coordinator.async_plan(trigger=CycleTrigger.SERVICE)
        await hass.async_block_till_done()

        status = hass.states.get("sensor.home_status")
        assert status is not None
        assert status.state == "degraded"
        assert status.attributes["has_usable_plan"] is True
        assert status.attributes["reason_codes"] == [
            "planner_failed_using_previous_plan"
        ]

        action = hass.states.get("sensor.home_battery_action")
        assert action is not None
        assert action.state == "preserve"
        assert hass.states.get("sensor.home_comfort_action").state == "off"

        battery_next = hass.states.get("sensor.home_battery_next_action")
        assert battery_next is not None
        assert battery_next.state == "self_consume"
        assert battery_next.attributes["timestamp"] == (
            plan_start + timedelta(hours=2)
        ).isoformat()
        comfort_next = hass.states.get("sensor.home_comfort_next_action")
        assert comfort_next is not None
        assert comfort_next.state == "on"
        assert comfort_next.attributes["timestamp"] == (
            plan_start + timedelta(hours=2)
        ).isoformat()

    later_slot = plan_start + timedelta(hours=2, minutes=1)

    class LaterSlotDateTime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return later_slot if tz is not None else later_slot.replace(tzinfo=None)

    with (
        patch(
            "custom_components.wattplan.coordinator_parts.snapshot.datetime",
            LaterSlotDateTime,
        ),
        patch(
            "custom_components.wattplan.coordinator_logic.source_status.datetime",
            LaterSlotDateTime,
        ),
    ):
        await coordinator.async_emit(trigger=CycleTrigger.SERVICE)
        await hass.async_block_till_done()

        assert hass.states.get("sensor.home_battery_action").state == "self_consume"
        assert hass.states.get("sensor.home_comfort_action").state == "on"
        assert hass.states.get("sensor.home_battery_next_action").state == STATE_UNKNOWN
        assert hass.states.get("sensor.home_comfort_next_action").state == STATE_UNKNOWN

    status = hass.states.get("sensor.home_status")
    assert status is not None
    assert status.state == "degraded"
    assert status.attributes["has_usable_plan"] is True
    assert status.attributes["reason_codes"] == ["planner_failed_using_previous_plan"]


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
            expired_at = expires_at
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


async def test_historical_cost_tracking_records_each_skipped_slot(
    hass: HomeAssistant,
    freezer,
) -> None:
    """Skipped historical intervals should create one gap record per slot."""
    start = datetime(2026, 5, 24, 12, 0, tzinfo=UTC)
    freezer.move_to(start + timedelta(hours=3, seconds=2))
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

    await tracker.async_process_completed_slot(start + timedelta(hours=3, seconds=1))

    day = tracker.store.data["days"]["2026-05-24"]
    assert day["starts"] == [
        "2026-05-24T12:00:00Z",
        "2026-05-24T13:00:00Z",
        "2026-05-24T14:00:00Z",
    ]
    assert day["flags"] == [FLAG_GAP, FLAG_GAP, FLAG_GAP]
    assert tracker.store.last_processed_slot() == start + timedelta(hours=2)
