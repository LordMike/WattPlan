"""Integration runtime test for WattPlan planning and emission."""

from __future__ import annotations

from datetime import timedelta
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
    CONF_SOURCE_PRICE,
    CONF_SOURCE_PV,
    CONF_SOURCE_USAGE,
    CONF_SOURCES,
    CONF_TARGET_ON_HOURS_PER_WINDOW,
    CONF_TEMPLATE,
    DOMAIN,
    SERVICE_CLEAR_TARGET,
    SERVICE_RUN_OPTIMIZE_NOW,
    SERVICE_RUN_PLAN_NOW,
    SERVICE_SET_TARGET,
    SOURCE_MODE_NOT_USED,
    SOURCE_MODE_TEMPLATE,
    SUBENTRY_TYPE_BATTERY,
    SUBENTRY_TYPE_COMFORT,
    SUBENTRY_TYPE_OPTIONAL,
)
from custom_components.wattplan.coordinator import STORAGE_VERSION, _snapshot_schema_id
import pytest

from homeassistant import config_entries
from homeassistant.const import CONF_NAME, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from tests.common import MockConfigEntry, async_fire_time_changed

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def _fake_optimize(_params: object) -> dict[str, object]:
    """Return deterministic optimizer output for integration projection tests."""
    return {
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
                    {"state": "charge", "charge_source": 1, "level": 5.1},
                    {"state": "hold", "charge_source": 0, "level": 5.1},
                    {"state": "hold", "charge_source": 0, "level": 5.1},
                    {"state": "discharge", "charge_source": 0, "level": 4.9},
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
    }


def _fake_optimize_with_target_behavior(params: object) -> dict[str, object]:
    """Return a deterministic plan that changes when a target is active."""
    battery = params.battery_entities[0]
    battery_schedule = (
        [
            {"state": "charge", "charge_source": 1, "level": 6.5},
            {"state": "charge", "charge_source": 1, "level": 8.0},
            {"state": "hold", "charge_source": 0, "level": 8.0},
            {"state": "hold", "charge_source": 0, "level": 8.0},
        ]
        if battery.target is not None
        else [
            {"state": "hold", "charge_source": 0, "level": 5.0},
            {"state": "hold", "charge_source": 0, "level": 5.0},
            {"state": "hold", "charge_source": 0, "level": 5.0},
            {"state": "hold", "charge_source": 0, "level": 5.0},
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




def _assert_valid_state(hass: HomeAssistant, entity_id: str) -> None:
    """Assert an entity exists and is not unknown/unavailable."""
    state = hass.states.get(entity_id)
    assert state is not None, f"{entity_id} missing"
    assert state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE), f"{entity_id} invalid"


async def test_full_runtime_optimize_and_emit_once(hass: HomeAssistant) -> None:
    """Set up entry with one of each asset and assert projected entities have data."""
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
                CONF_SOURCE_PRICE: {
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
        await hass.services.async_call(DOMAIN, SERVICE_RUN_PLAN_NOW, {}, blocking=True)
        await hass.async_block_till_done()

    _assert_valid_state(hass, "sensor.home_status")
    _assert_valid_state(hass, "sensor.home_last_run")
    _assert_valid_state(hass, "sensor.home_last_run_duration")
    _assert_valid_state(hass, "sensor.home_projected_cost_savings")
    _assert_valid_state(hass, "sensor.home_projected_savings_percentage")
    _assert_valid_state(hass, "sensor.home_battery_action")
    _assert_valid_state(hass, "sensor.home_comfort_action")
    _assert_valid_state(hass, "sensor.home_optional_next_start_option")
    _assert_valid_state(hass, "sensor.home_optional_next_end_option")
    _assert_valid_state(hass, "sensor.home_optional_option_1_start")

    savings = hass.states.get("sensor.home_projected_cost_savings")
    assert savings is not None
    assert float(savings.state) == 3.0
    assert "span_start" in savings.attributes
    assert "span_end" in savings.attributes
    assert savings.attributes["total"] == 3.0
    assert savings.attributes["values"] == [0.5, 1.0, 1.0, 0.5]

    savings_pct = hass.states.get("sensor.home_projected_savings_percentage")
    assert savings_pct is not None
    assert float(savings_pct.state) == 24.0
    assert savings_pct.attributes["span_start"] == savings.attributes["span_start"]
    assert savings_pct.attributes["span_end"] == savings.attributes["span_end"]
    assert savings_pct.attributes["total"] == 24.0
    assert savings_pct.attributes["values"] == [25.0, 33.333333, 25.0, 14.285714]

    battery_action = hass.states.get("sensor.home_battery_action")
    assert battery_action is not None
    assert battery_action.attributes["next_action"] == "hold"
    assert "next_action_timestamp" in battery_action.attributes


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
                CONF_SOURCE_PRICE: {
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
                "created_at": "2026-01-01T00:00:00+00:00",
                "planner_status": "planned",
                "planner_message": "Restored plan",
                "battery_charge_source": {},
                "diagnostics": {
                    "batteries": {},
                    "comforts": {},
                    "optionals": {
                        "optional_sub": {
                            "next_start_option": "2026-01-01T01:00:00+00:00",
                            "next_end_option": "2026-01-01T02:00:00+00:00",
                            "option_1_start": "2026-01-01T01:00:00+00:00",
                        }
                    },
                    "optimizer": {
                        "suboptimal": False,
                        "suboptimal_reasons": [],
                    },
                },
            },
            "last_success_at": "2026-01-01T00:00:00+00:00",
            "last_duration_ms": 123,
        }
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    _assert_valid_state(hass, "sensor.home_status")
    _assert_valid_state(hass, "sensor.home_optional_next_start_option")
    _assert_valid_state(hass, "sensor.home_optional_next_end_option")


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
                CONF_SOURCE_PRICE: {
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
    assert len(state.attributes["price_per_kwh"]) == 4
    assert len(state.attributes["usage_kwh"]) == 4
    assert len(state.attributes["solar_input_kwh"]) == 4
    assert len(state.attributes["projected_cost"]) == 4
    assert len(state.attributes["projected_savings_cost"]) == 4
    assert len(state.attributes["projected_savings_pct"]) == 4
    assert len(state.attributes["battery_battery_action"]) == 4
    assert len(state.attributes["battery_battery_level_kwh"]) == 4
    assert len(state.attributes["battery_battery_charge_source"]) == 4
    assert len(state.attributes["comfort_comfort_enabled"]) == 4
    assert len(state.attributes["optional_optional_enabled"]) == 4
    assert state.attributes["battery_battery_action"] == ["c", "h", "h", "d"]
    assert state.attributes["battery_battery_charge_source"] == ["g", "n", "n", "n"]
    assert state.attributes["comfort_comfort_enabled"] == [True, False, False, True]
    assert state.attributes["optional_optional_enabled"] == [False, True, True, False]


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
                CONF_SOURCE_PRICE: {
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
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        await hass.services.async_call(DOMAIN, SERVICE_RUN_OPTIMIZE_NOW, {}, blocking=True)
        await hass.async_block_till_done()

        plan_details = hass.states.get("sensor.home_plan_details")
        assert plan_details is not None
        assert plan_details.attributes["battery_battery_action"] == ["h", "h", "h", "h"]

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
        assert plan_details.attributes["battery_battery_action"] == ["c", "c", "h", "h"]

        async_fire_time_changed(hass, target_at + timedelta(minutes=1))
        await hass.async_block_till_done()

        await hass.services.async_call(DOMAIN, SERVICE_RUN_OPTIMIZE_NOW, {}, blocking=True)
        await hass.async_block_till_done()

        target_sensor = hass.states.get("sensor.home_battery_target")
        assert target_sensor is not None
        assert target_sensor.state == STATE_UNKNOWN
        assert target_sensor.attributes["by"] == "not_set"

        plan_details = hass.states.get("sensor.home_plan_details")
        assert plan_details is not None
        assert plan_details.attributes["battery_battery_action"] == ["h", "h", "h", "h"]


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
                CONF_SOURCE_PRICE: {
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
