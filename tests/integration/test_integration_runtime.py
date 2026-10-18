"""Integration runtime test for WattPlan planning and emission."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

from custom_components.wattplan.const import (
    ADAPTER_TYPE_ATTRIBUTE_OBJECTS,
    CONF_ACTION_EMISSION_ENABLED,
    CONF_ADAPTER_TYPE,
    CONF_CAN_CHARGE_FROM_GRID,
    CONF_CAN_CHARGE_FROM_PV,
    CONF_CAPACITY_KWH,
    CONF_CHARGE_EFFICIENCY,
    CONF_DISCHARGE_EFFICIENCY,
    CONF_FIXUP_PROFILE,
    CONF_HOURS_TO_PLAN,
    CONF_HISTORICAL_COST_TRACKING_ENABLED,
    CONF_HISTORICAL_GRID_EXPORT_SENSOR,
    CONF_HISTORICAL_GRID_IMPORT_SENSOR,
    CONF_HISTORICAL_PV_SENSOR,
    CONF_HISTORICAL_SIMULATE_SELF_CONSUMPTION,
    CONF_HISTORICAL_USAGE_SENSOR,
    CONF_MAX_CHARGE_KW,
    CONF_MAX_DISCHARGE_KW,
    CONF_MINIMUM_KWH,
    CONF_OUTLOOK_LANGUAGES,
    CONF_PLANNING_ENABLED,
    CONF_PROVIDERS,
    CONF_SLOT_MINUTES,
    CONF_SOC_SOURCE,
    CONF_SOURCE_MODE,
    CONF_SOURCE_EXPORT_PRICE,
    CONF_SOURCE_IMPORT_PRICE,
    CONF_SOURCE_PV,
    CONF_SOURCE_USAGE,
    CONF_SOURCES,
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
)
from custom_components.wattplan.coordinator import (
    STORAGE_VERSION,
    CycleTrigger,
    WattPlanCoordinator,
    snapshot_schema_id,
)
from custom_components.wattplan.coordinator_logic.source_status import SourceStatusManager
from custom_components.wattplan.coordinator_parts import (
    PlanningStageError,
    Stage,
    StageErrorKind,
)
from custom_components.wattplan.historical_cost.models import (
    FLAG_GAP,
    FLAG_MISSING_METER,
    FLAG_SELF_CONSUMPTION_UNAVAILABLE,
)
from custom_components.wattplan.entry_setup import config_fingerprint
from custom_components.wattplan.historical_cost.tracker import HistoricalCostTracker
from custom_components.wattplan.source_fixup import SourceHealthKind, SourceHealthState
from tests.plan_invariants import assert_plan_invariants
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
from tests.integration.test_integration_e2e import (  # noqa: F401
    _base_sources,
    _battery_subentry,
    _comfort_subentry,
    _entry,
    _optional_subentry,
    _setup_entry,
    entity_registry_enabled_by_default,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def test_source_failure_debounce_counts_distinct_slots_and_recovers(
    hass: HomeAssistant, freezer
) -> None:
    """Retries do not advance the streak; recovery, restart and a slot gap clear it."""
    manager = SourceStatusManager(hass)
    unavailable = SourceHealthState(
        kind=SourceHealthKind.UNAVAILABLE,
        available_count=0,
        required_count=4,
        using_stale=False,
        expires_at=None,
    )
    source_config = {CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE}
    base = datetime(2026, 9, 23, 12, tzinfo=UTC)

    def build(manager: SourceStatusManager, health: SourceHealthState) -> dict:
        return manager._build_source_status(
            source_key=CONF_SOURCE_PV,
            source_config=source_config,
            health=health,
            slot_minutes=30,
        )

    for slot_index in range(4):
        freezer.move_to(base + timedelta(minutes=30 * slot_index))
        build(manager, unavailable)
        retry = build(manager, unavailable)
        assert retry["failure_slot_count"] == slot_index + 1

    recovered = build(
        manager,
        SourceHealthState(
            kind=SourceHealthKind.OK,
            available_count=4,
            required_count=4,
            using_stale=False,
            expires_at=None,
        ),
    )
    assert recovered["failure_slot_count"] == 0

    restarted_manager = SourceStatusManager(hass)
    assert build(restarted_manager, unavailable)["failure_slot_count"] == 1

    # Skipping an affected slot starts a new consecutive streak.
    after_gap_at = base + timedelta(minutes=90 + 60)
    freezer.move_to(after_gap_at)
    after_gap = build(restarted_manager, unavailable)
    assert after_gap["failure_slot_count"] == 1
    assert after_gap["failure_started_at"] == after_gap_at.isoformat()


def test_planning_failure_debounce_counts_consecutive_slots_only(
    hass: HomeAssistant, freezer
) -> None:
    """Manual planning retries do not count, and a skipped slot resets the streak."""
    coordinator = WattPlanCoordinator(
        hass,
        entry_id="planning_failure_debounce",
        update_interval=timedelta(minutes=30),
        planning_enabled=False,
        action_emission_enabled=False,
    )
    base = datetime(2026, 9, 23, 12, tzinfo=UTC)

    def fail_plan_at(moment: datetime) -> dict:
        freezer.move_to(moment)
        coordinator._set_stage_error(
            Stage.PLAN, StageErrorKind.PLANNER_EXECUTION, "planner failed"
        )
        return coordinator.outlook_failure_status

    first = fail_plan_at(base)
    retry = fail_plan_at(base)
    adjacent = fail_plan_at(base + timedelta(minutes=30))
    after_gap = fail_plan_at(base + timedelta(minutes=90))

    assert first["slot_count"] == 1
    assert retry["slot_count"] == 1
    assert adjacent["slot_count"] == 2
    assert adjacent["started_at"] == base
    assert after_gap["slot_count"] == 1
    assert after_gap["started_at"] == base + timedelta(minutes=90)


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



def _import_only_sources(
    template: str = "{{ [0.2, 0.25, 0.3, 0.35] }}",
) -> dict[str, dict[str, Any]]:
    """Return sources with one import price template and no usage or PV."""
    return {
        CONF_SOURCE_IMPORT_PRICE: {
            CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
            CONF_TEMPLATE: template,
        },
        CONF_SOURCE_USAGE: {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED},
        CONF_SOURCE_PV: {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED},
    }


def _all_assets() -> list[config_entries.ConfigSubentryData]:
    """Return one battery, one comfort and one optional subentry."""
    return [
        _battery_subentry(subentry_id="battery_sub", name="battery"),
        _comfort_subentry(subentry_id="comfort_sub", name="comfort"),
        _optional_subentry(subentry_id="optional_sub", name="optional"),
    ]


def _hourly_sources(
    *, import_price: float = 1.0, export_price: float = 0.1
) -> dict[str, dict[str, Any]]:
    """Return flat four-slot import and export price templates."""
    return {
        CONF_SOURCE_IMPORT_PRICE: {
            CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
            CONF_TEMPLATE: f"{{{{ [{import_price}] * 4 }}}}",
        },
        CONF_SOURCE_EXPORT_PRICE: {
            CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
            CONF_TEMPLATE: f"{{{{ [{export_price}] * 4 }}}}",
        },
    }


def _self_consumption_battery(
    *, capacity_kwh: float = 10.0, max_kw: float = 3.0
) -> config_entries.ConfigSubentryData:
    """Return a lossless PV-charged battery for historical reference tests."""
    return config_entries.ConfigSubentryData(
        subentry_id="battery_sub",
        subentry_type=SUBENTRY_TYPE_BATTERY,
        title="battery",
        unique_id="battery:battery",
        data={
            CONF_NAME: "battery",
            CONF_SOC_SOURCE: "sensor.battery_soc",
            CONF_CAPACITY_KWH: capacity_kwh,
            CONF_MINIMUM_KWH: 0.0,
            CONF_MAX_CHARGE_KW: max_kw,
            CONF_MAX_DISCHARGE_KW: max_kw,
            CONF_CHARGE_EFFICIENCY: 1.0,
            CONF_DISCHARGE_EFFICIENCY: 1.0,
            CONF_CAN_CHARGE_FROM_GRID: False,
            CONF_CAN_CHARGE_FROM_PV: True,
        },
    )


def _set_hourly_meters(
    hass: HomeAssistant,
    grid_import: float,
    grid_export: float,
    usage: float,
    pv: float,
) -> None:
    """Set all four cumulative meters."""
    _set_energy_meter(hass, "sensor.grid_import_total", grid_import)
    _set_energy_meter(hass, "sensor.grid_export_total", grid_export)
    _set_energy_meter(hass, "sensor.usage_total", usage)
    _set_energy_meter(hass, "sensor.pv_total", pv)


def _seed_tracker(tracker: HistoricalCostTracker, start: datetime) -> None:
    """Make the tracker believe slot `start` is the first one still to process."""
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


async def _setup_battery_tracker(
    hass: HomeAssistant, freezer, start: datetime
) -> tuple[MockConfigEntry, HistoricalCostTracker]:
    """Set up an hourly battery tracker with slot `start` complete and unprocessed.

    The slot used 1.0 kWh imported, 0.2 kWh exported, 1.5 kWh load and 1.0 kWh PV.
    """
    freezer.move_to(start + timedelta(hours=1, seconds=2))
    entry = _entry(
        title="Home",
        sources=_hourly_sources(),
        options=_historical_options(),
        subentries_data=[_self_consumption_battery()],
    )
    entry.add_to_hass(hass)
    _set_hourly_meters(hass, 100.0, 10.0, 200.0, 50.0)
    hass.states.async_set(
        "sensor.battery_soc",
        "1.0",
        {"unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR},
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    tracker = entry.runtime_data.historical_tracker
    assert tracker is not None
    _seed_tracker(tracker, start)
    tracker.store.update_simulation_soc({"battery_sub": 1.0})
    _set_hourly_meters(hass, 101.0, 10.2, 201.5, 51.0)
    return entry, tracker


async def test_runtime_diagnostic_sensors_disabled_by_default(
    hass: HomeAssistant,
) -> None:
    """Register noisy runtime sensors disabled while keeping last run enabled."""
    entry = _entry(title="Home", sources=_import_only_sources(), subentries_data=[])
    entry.add_to_hass(hass)

    with patch(
        "custom_components.wattplan.optimizer.optimize",
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
    assert last_run_entry is not None
    assert last_run_entry.disabled_by is None
    _assert_valid_state(hass, "sensor.home_last_run")

    for entity_id in ("sensor.home_next_run", "sensor.home_last_run_duration"):
        registered = entity_registry.async_get(entity_id)
        assert registered is not None
        assert registered.disabled_by == er.RegistryEntryDisabler.INTEGRATION
        assert registered.entity_category == EntityCategory.DIAGNOSTIC
        assert hass.states.get(entity_id) is None


async def test_full_runtime_optimize_publishes_plan_entities(
    hass: HomeAssistant,
) -> None:
    """Set up entry with one of each asset and assert runtime entities have data."""
    hass.config.language = "en-US"
    entry = _entry(
        title="Home",
        options={
            CONF_PLANNING_ENABLED: False,
            CONF_ACTION_EMISSION_ENABLED: False,
            CONF_OUTLOOK_LANGUAGES: ["da", "en", "da"],
        },
        subentries_data=_all_assets(),
    )
    entry.add_to_hass(hass)

    hass.states.async_set("sensor.battery_soc", "5.0")
    hass.states.async_set("binary_sensor.comfort_on_off", "off")

    with patch("custom_components.wattplan.optimizer.optimize", side_effect=_fake_optimize):
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

    for entity_id in (
        "sensor.home_status",
        "sensor.home_plan_outlook_da",
        "sensor.home_plan_outlook_en",
        "sensor.home_last_run",
        "sensor.home_battery_action",
        "sensor.home_comfort_action",
        "sensor.home_optional_next_start_option",
        "sensor.home_optional_option_1_start",
    ):
        _assert_valid_state(hass, entity_id)

    outlook = hass.states.get("sensor.home_plan_outlook_en")
    assert outlook is not None
    assert len(outlook.state) <= 255
    assert outlook.state == outlook.attributes["report_id"]
    assert outlook.state.count("_") >= 3
    assert outlook.attributes["text"]
    assert outlook.attributes["language"] == "en"
    assert outlook.attributes["information_value"] in {"low", "medium", "high"}
    assert outlook.attributes["topic"] in {
        "routine",
        "opportunity",
        "battery",
        "target",
        "energy_balance",
        "reliability",
    }
    assert hass.states.get("sensor.home_plan_outlook_da").attributes["language"] == "da"

    next_option = hass.states.get("sensor.home_optional_next_start_option")
    option_1 = hass.states.get("sensor.home_optional_option_1_start")
    for option in (next_option, option_1):
        start = dt_util.parse_datetime(option.state)
        end = dt_util.parse_datetime(option.attributes["end_timestamp"])
        assert start is not None
        assert end is not None
        assert end - start == timedelta(hours=1)
    assert option_1.state == next_option.state
    assert option_1.attributes["end_timestamp"] == next_option.attributes["end_timestamp"]

    battery_action = hass.states.get("sensor.home_battery_action")
    assert battery_action.attributes["friendly_name"] == "(battery) Action"
    assert battery_action.state == "grid_charge"
    assert "next_action" not in battery_action.attributes
    assert "next_action_timestamp" not in battery_action.attributes


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
    _, tracker = await _setup_battery_tracker(hass, freezer, start)

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
    for entity_id in (
        "sensor.home_historical_actual_cost_this_month",
        "sensor.home_historical_grid_only_cost_this_month",
    ):
        registered = entity_registry.async_get(entity_id)
        assert registered is not None
        assert registered.disabled


@pytest.mark.parametrize(
    "variant",
    [
        "complete",
        "missing_pv",
        "missing_price",
        "usage_reset",
        "skipped_boundary",
        "missing_simulation_state",
    ],
)
async def test_identical_self_consumption_execution_has_zero_reported_savings(
    hass: HomeAssistant, freezer, variant: str,
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
    entry = _entry(
        title="Home",
        sources=_hourly_sources(import_price=1.0, export_price=0.0),
        options=_historical_options(),
        subentries_data=[_self_consumption_battery(capacity_kwh=1.0, max_kw=1.0)],
    )
    entry.add_to_hass(hass)
    _set_hourly_meters(hass, 0.0, 0.0, 0.0, 0.0)
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
    if variant == "missing_simulation_state":
        tracker.store.update_simulation_soc({})

    original_price = tracker._async_price

    async def _price_with_gap(source_key: str, slot_start: datetime) -> float | None:
        if (
            variant == "missing_price"
            and source_key == CONF_SOURCE_IMPORT_PRICE
            and slot_start == start
        ):
            return None
        return await original_price(source_key, slot_start)

    hours = (1, 3, 4) if variant == "skipped_boundary" else (1, 2, 3)
    for hour in hours:
        now = start + timedelta(hours=hour, seconds=1)
        freezer.move_to(now)
        _set_energy_meter(hass, "sensor.grid_import_total", float(hour - 1))
        _set_energy_meter(
            hass,
            "sensor.usage_total",
            (-1.0 if hour == 1 else float(hour - 2))
            if variant == "usage_reset"
            else float(hour),
        )
        _set_energy_meter(hass, "sensor.pv_total",
                          float("nan") if variant == "missing_pv" and hour == 1 else 0.0)
        hass.states.async_set("sensor.battery_soc", "0.0",
                             {"unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR})
        with patch.object(tracker, "_async_price", side_effect=_price_with_gap):
            await tracker.async_process_completed_slot(now)
        await hass.async_block_till_done()
    sensor = hass.states.get("sensor.home_historical_savings_vs_self_consumption_today")
    assert sensor is not None
    assert sensor.attributes["slots"] == (4 if variant == "skipped_boundary" else 3)
    assert sensor.attributes["missing_slots"] == (
        2
        if variant in {"missing_pv", "skipped_boundary"}
        else 1
        if variant in {"missing_price", "missing_simulation_state", "usage_reset"}
        else 0
    )
    assert float(sensor.state) == pytest.approx(0.0)
    assert sensor.attributes["reference_continuity_valid"] is True
    if variant in {"missing_pv", "usage_reset", "missing_simulation_state"}:
        assert sensor.attributes["reference_segment_ids"] == ["segment-2"]
        assert sensor.attributes["reference_segment_reason"] == (
            "reinitialized_after_untrusted_slot"
        )
    elif variant == "skipped_boundary":
        assert sensor.attributes["reference_segment_ids"] == [
            "segment-1",
            "segment-2",
        ]
        assert sensor.attributes["reference_segment_reason"] == (
            "reinitialized_after_skipped_slots"
        )
    else:
        assert sensor.attributes["reference_segment_ids"] == ["segment-1"]

    if variant == "missing_pv":
        # A missing PV meter only voids the self-consumption reference; the
        # actual and grid-only metrics keep the slot.
        day = tracker.store.data["days"]["2026-09-01"]
        assert day["flags"][0] == FLAG_MISSING_METER | FLAG_SELF_CONSUMPTION_UNAVAILABLE
        expected = {
            "sensor.home_historical_actual_cost_today": 2.0,
            "sensor.home_historical_grid_only_cost_today": 3.0,
        }
        for entity_id, value in expected.items():
            state = hass.states.get(entity_id)
            assert state is not None
            assert float(state.state) == pytest.approx(value)
            assert state.attributes["missing_slots"] == 0


async def test_overlapping_historical_refresh_paths_process_each_slot_once(
    hass: HomeAssistant,
    freezer,
) -> None:
    """Timer, coordinator, and service refreshes should share one slot transaction."""
    start = datetime(2026, 5, 24, 12, 0, tzinfo=UTC)
    first_refresh = start + timedelta(hours=1, seconds=2)
    entry, tracker = await _setup_battery_tracker(hass, freezer, start)

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
    _set_hourly_meters(hass, 100.0, 10.0, 200.0, 50.0)

    with patch(
        "custom_components.wattplan.optimizer.optimize",
        side_effect=_fake_optimize,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await entry.runtime_data.coordinator.async_plan(trigger=CycleTrigger.SERVICE)

    tracker = entry.runtime_data.historical_tracker
    assert tracker is not None
    assert tracker.store.cached_price(start, "import") == pytest.approx(1.0)
    assert tracker.store.cached_price(start, "export") == pytest.approx(0.1)

    _seed_tracker(tracker, start)
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
    _set_hourly_meters(hass, 101.0, 10.2, 201.5, 51.0)

    await tracker.async_process_completed_slot(start + timedelta(hours=1, seconds=1))
    await hass.async_block_till_done()

    day = tracker.store.data["days"]["2026-05-24"]
    assert day["import_price"] == [1.0]
    assert day["export_price"] == [0.1]
    assert day["flags"] == [0]
    actual = hass.states.get("sensor.home_historical_actual_cost_today")
    assert actual is not None
    assert float(actual.state) == pytest.approx(0.98)




async def test_restore_snapshot_on_startup(
    hass: HomeAssistant,
    frozen_clock,
    entity_registry_enabled_by_default: None,
) -> None:
    """Restore a still-valid cached plan so sensors are available before the first plan."""
    now = datetime.now(tz=UTC)
    plan_start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    plan_end = plan_start + timedelta(hours=4)
    entry = _entry(
        title="Home",
        sources=_import_only_sources(),
        subentries_data=_all_assets(),
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
            "schema_id": snapshot_schema_id(),
            "config_fingerprint": config_fingerprint(entry),
            "planner_preset": "highs",
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
                        "planner_preset": "highs",
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

    with patch.object(
        WattPlanCoordinator, "async_plan", new_callable=AsyncMock
    ) as background_plan:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)

    # Setup did not wait on a plan, but one still ran afterwards to refresh the cache.
    background_plan.assert_awaited_once_with(trigger=CycleTrigger.SERVICE)
    _assert_valid_state(hass, "sensor.home_status")
    _assert_valid_state(hass, "sensor.home_last_run_duration")
    status = hass.states.get("sensor.home_status")
    assert status is not None
    assert status.attributes["scheduler_stale"] is False
    assert status.attributes["action_recommendations_validated"] is True
    assert status.attributes["has_usable_plan"] is True
    assert status.attributes["reason_codes"] == ["restored_plan_in_use"]
    assert hass.states.get("sensor.home_battery_action").state == "preserve"
    assert hass.states.get("sensor.home_comfort_action").state == "off"
    _assert_valid_state(hass, "sensor.home_optional_next_start_option")

    duration_state = hass.states.get("sensor.home_last_run_duration")
    assert duration_state is not None
    assert duration_state.attributes["planner_preset"] == "highs"
    assert [label for label, _ms in duration_state.attributes["timings"]] == [
        "Import price source fetch",
        "Optimizer plan calculation",
        "total",
    ]

    coordinator = entry.runtime_data.coordinator
    frozen_clock.move_to(plan_end + timedelta(minutes=1))
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    status = hass.states.get("sensor.home_status")
    assert status is not None
    assert status.attributes["reason_codes"] == ["plan_stale"]
    assert status.attributes["scheduler_stale"] is False
    assert status.attributes["has_usable_plan"] is False
    expired_outlook = hass.states.get("sensor.home_plan_outlook_en")
    assert expired_outlook is not None
    assert expired_outlook.state.startswith("plan_expired_high_")
    assert "plan_expired:plan" in expired_outlook.attributes["selected_facts"]
    _assert_valid_state(hass, "sensor.home_last_run_duration")
    frozen_clock.move_to(now)

    # Re-restoring the payload directly (no fingerprint check) is diagnostic-only.
    payload = coordinator.restore_payload()
    assert payload is not None
    assert coordinator.async_restore_payload(payload)
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

    with patch("custom_components.wattplan.optimizer.optimize", _fake_optimize):
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


_MATCHING = object()


async def _setup_with_cached_snapshot(
    hass: HomeAssistant,
    *,
    span_hours_ago: int,
    fingerprint: str | None | object,
) -> tuple[MockConfigEntry, AsyncMock, Store]:
    """Set up an entry whose cached snapshot covers a chosen window."""
    now = datetime.now(tz=UTC)
    span_end = now.replace(minute=0, second=0, microsecond=0) - timedelta(
        hours=span_hours_ago
    )
    span_start = span_end - timedelta(hours=4)
    entry = _entry(title="Home", sources=_import_only_sources(), subentries_data=[])
    entry.add_to_hass(hass)
    store = Store[dict[str, object]](
        hass, STORAGE_VERSION, f"{DOMAIN}.snapshot.{entry.entry_id}", private=True
    )
    await store.async_save(
        {
            "schema_id": snapshot_schema_id(),
            "config_fingerprint": (
                config_fingerprint(entry) if fingerprint is _MATCHING else fingerprint
            ),
            "planner_preset": "highs",
            "snapshot": {
                "created_at": span_start.isoformat(),
                "planner_status": "planned",
                "planner_message": "Restored plan",
                "diagnostics": {
                    "optimizer": {
                        "span_start": span_start.isoformat(),
                        "span_end": span_end.isoformat(),
                    },
                },
                "action_schedules": {},
            },
            "last_success_at": span_start.isoformat(),
            "last_duration_ms": 1,
            "last_run_timings": None,
        }
    )
    with patch.object(
        WattPlanCoordinator, "async_plan", new_callable=AsyncMock
    ) as plan:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    return entry, plan, store


async def test_expired_cached_snapshot_is_discarded_and_replanned(
    hass: HomeAssistant,
) -> None:
    """A snapshot whose plan no longer covers now is dropped and a plan runs."""
    entry, plan, store = await _setup_with_cached_snapshot(
        hass, span_hours_ago=1, fingerprint=_MATCHING
    )
    coordinator = entry.runtime_data.coordinator
    assert coordinator.snapshot is None
    assert coordinator.action_recommendations_validated is False
    assert await store.async_load() is None
    plan.assert_awaited_once_with(trigger=CycleTrigger.SERVICE)


@pytest.mark.parametrize("fingerprint", ["different-config", None])
async def test_cached_snapshot_from_other_config_is_discarded(
    hass: HomeAssistant, fingerprint: str | None
) -> None:
    """A snapshot from changed config, or an old one without fingerprint, is dropped."""
    entry, plan, store = await _setup_with_cached_snapshot(
        hass, span_hours_ago=-2, fingerprint=fingerprint
    )
    coordinator = entry.runtime_data.coordinator
    assert coordinator.snapshot is None
    assert coordinator.action_recommendations_validated is False
    assert await store.async_load() is None
    plan.assert_awaited_once_with(trigger=CycleTrigger.SERVICE)


async def test_successful_plan_persists_completed_last_run(
    hass: HomeAssistant,
) -> None:
    """Persist the successful run timestamp from the run that just completed."""
    entry = _entry(title="Home", sources=_import_only_sources(), subentries_data=[])
    entry.add_to_hass(hass)

    with patch(
        "custom_components.wattplan.optimizer.optimize",
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


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_failed_plan_advances_retained_actions_across_tariff_boundary(
    hass: HomeAssistant,
    frozen_clock,
) -> None:
    """A failed replan should retain the schedule, not its first actions."""
    entry = _entry(
        title="Home",
        sources=_import_only_sources("{{ [0.05, 0.4, 0.4, 0.4] }}"),
        options={
            CONF_PLANNING_ENABLED: False,
            CONF_ACTION_EMISSION_ENABLED: True,
        },
        subentries_data=[
            _battery_subentry(subentry_id="battery_sub", name="battery"),
            _comfort_subentry(subentry_id="comfort_sub", name="comfort"),
        ],
    )
    entry.add_to_hass(hass)
    hass.states.async_set("sensor.battery_soc", "5.0")
    hass.states.async_set("binary_sensor.comfort_on_off", "off")

    with patch("custom_components.wattplan.optimizer.optimize") as optimize_mock:
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
    plan_start = dt_util.parse_datetime(coordinator.snapshot.action_schedules["start_at"])
    assert plan_start is not None

    frozen_clock.move_to(plan_start + timedelta(hours=1, minutes=1))
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

    assert hass.states.get("sensor.home_battery_action").state == "preserve"
    assert hass.states.get("sensor.home_comfort_action").state == "off"
    for next_action, expected in (
        ("sensor.home_battery_next_action", "self_consume"),
        ("sensor.home_comfort_next_action", "on"),
    ):
        state = hass.states.get(next_action)
        assert state is not None
        assert state.state == expected
        assert state.attributes["timestamp"] == (
            plan_start + timedelta(hours=2)
        ).isoformat()

    # The scheduled slot-boundary run moves the retained actions on.
    later_slot = plan_start + timedelta(hours=2, minutes=1)
    frozen_clock.move_to(later_slot)
    async_fire_time_changed(hass, later_slot)
    await hass.async_block_till_done(wait_background_tasks=True)

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
    frozen_clock,
) -> None:
    """A retained previous plan should stop being usable after its coverage ends."""
    entry = _entry(
        title="Home",
        sources=_base_sources(),
        subentries_data=[_battery_subentry(subentry_id="battery_sub", name="battery")],
    )
    entry.add_to_hass(hass)
    hass.states.async_set("sensor.battery_soc", "5.0")

    with patch("custom_components.wattplan.optimizer.optimize", side_effect=_fake_optimize):
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
    planned_at = dt_util.utcnow()

    # No public driver exists for an unusable but unexpired plan, so fail the
    # health state directly.
    healthy_status = dict(coordinator._source_status._overall_status)
    coordinator._source_status._overall_status.update(
        {
            "status": "failed",
            "reason_codes": ["source_import_price_failed_critical"],
            "reason_summary": "No usable plan is available",
            "has_usable_plan": False,
        }
    )
    coordinator.async_update_listeners()
    await hass.async_block_till_done()

    unusable_outlook = hass.states.get("sensor.home_plan_outlook_en")
    assert unusable_outlook is not None
    assert unusable_outlook.state.startswith("plan_unusable_high_")
    assert "plan_unusable:plan" in unusable_outlook.attributes["selected_facts"]
    assert "expired" not in unusable_outlook.attributes["text"]
    assert "saved plan" in unusable_outlook.attributes["text"].lower()
    assert "used" in unusable_outlook.attributes["text"].lower()

    coordinator._source_status._overall_status = healthy_status
    coordinator.async_update_listeners()
    await hass.async_block_till_done()

    frozen_clock.move_to(expires_at)
    coordinator.async_update_listeners()
    await hass.async_block_till_done()

    status = hass.states.get("sensor.home_status")
    assert status is not None
    assert status.state == "failed"
    assert status.attributes["reason_codes"] == ["plan_stale"]
    assert status.attributes["is_stale"] is True
    assert status.attributes["has_usable_plan"] is False
    assert hass.states.get("sensor.home_battery_action").state == STATE_UNAVAILABLE

    frozen_clock.move_to(planned_at)
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.home_status").state == "ok"

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


async def test_plan_details_sensor_exposes_horizon_length_arrays(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,
) -> None:
    """Enable the plan details sensor and assert graph payload shape is compact."""
    entry = _entry(title="Home", subentries_data=_all_assets())
    entry.add_to_hass(hass)

    hass.states.async_set("sensor.battery_soc", "5.0")
    hass.states.async_set("binary_sensor.comfort_on_off", "off")

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    with patch("custom_components.wattplan.optimizer.optimize", side_effect=_fake_optimize):
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
    for key in (
        "grid_import_price_per_kwh",
        "grid_export_price_per_kwh",
        "usage_kwh",
        "solar_input_kwh",
        "projected_cost",
        "projected_savings_cost",
        "projected_savings_pct",
        "battery_battery_action",
        "battery_battery_level_kwh",
        "comfort_comfort_enabled",
        "optional_optional_enabled",
    ):
        assert len(state.attributes[key]) == 4, key
    assert state.attributes["grid_export_price_per_kwh"] == [0.0, 0.0, 0.0, 0.0]
    assert state.attributes["battery_battery_action"] == ["gc", "sc", "sc", "sc"]
    assert state.attributes["comfort_comfort_enabled"] == [True, False, False, True]
    assert state.attributes["optional_optional_enabled"] == [False, True, True, False]
    assert "timings" not in state.attributes

    duration_state = hass.states.get("sensor.home_last_run_duration")
    assert duration_state is not None
    assert "optimizer_reported_ms" in duration_state.attributes
    # Timings are cumulative checkpoints that end with the total.
    timings = duration_state.attributes["timings"]
    assert timings[-1][0] == "total"
    values = [value for _label, value in timings]
    assert values == sorted(values)

    hourly_state = hass.states.get("sensor.home_plan_details_hourly")
    assert hourly_state is not None
    assert "timings" not in hourly_state.attributes


@pytest.mark.usefixtures("frozen_clock")
async def test_plan_details_timings_keep_merged_source_as_single_source_entry(
    hass: HomeAssistant,
    entity_registry_enabled_by_default: None,
) -> None:
    """Merged sources expose one timing entry; unconfigured sources have none."""
    start_at = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    sources = _import_only_sources()
    sources[CONF_SOURCE_IMPORT_PRICE] = {
        CONF_SOURCE_MODE: SOURCE_MODE_ENTITY_ADAPTER,
        CONF_PROVIDERS: [
            {
                CONF_SOURCE_MODE: SOURCE_MODE_ENTITY_ADAPTER,
                "entity_id": entity_id,
                CONF_ADAPTER_TYPE: ADAPTER_TYPE_ATTRIBUTE_OBJECTS,
                CONF_NAME: "prices",
                CONF_TIME_KEY: "start",
                CONF_VALUE_KEY: "value",
            }
            for entity_id in ("sensor.prices_today", "sensor.prices_tomorrow")
        ],
    }
    entry = _entry(title="Home", sources=sources, subentries_data=[])
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

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    with patch("custom_components.wattplan.optimizer.optimize", side_effect=_fake_optimize):
        await hass.services.async_call(
            DOMAIN, SERVICE_RUN_OPTIMIZE_NOW, {}, blocking=True
        )
        await hass.async_block_till_done()

    state = hass.states.get("sensor.home_plan_details")
    assert state is not None
    assert "timings" not in state.attributes
    duration_state = hass.states.get("sensor.home_last_run_duration")
    assert duration_state is not None
    tasks = [label for label, _ms in duration_state.attributes["timings"]]
    assert tasks.count("Import price source fetch") == 1
    assert all("provider" not in task for task in tasks)
    for unconfigured in (
        "Export price source fetch",
        "Usage source fetch",
        "PV source fetch",
    ):
        assert unconfigured not in tasks


def _set_battery_target(hass: HomeAssistant, reach_at: datetime):
    """Call the set_target service for the test battery."""
    return hass.services.async_call(
        DOMAIN,
        SERVICE_SET_TARGET,
        {"battery": "battery", "soc_kwh": 8.0, "reach_at": reach_at},
        blocking=True,
    )


@pytest.mark.usefixtures("frozen_clock", "entity_registry_enabled_by_default")
async def test_battery_target_changes_plan_clears_and_expires_after_deadline(
    hass: HomeAssistant,
) -> None:
    """Targets affect planning until cleared or past their deadline."""
    entry = _entry(
        title="Home",
        sources={
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
        subentries_data=[_battery_subentry(subentry_id="battery_sub", name="battery")],
    )
    entry.add_to_hass(hass)
    hass.states.async_set("sensor.battery_soc", "5.0")

    async def optimize_and_read_actions() -> list[str]:
        await hass.services.async_call(DOMAIN, SERVICE_RUN_OPTIMIZE_NOW, {}, blocking=True)
        await hass.async_block_till_done()
        plan_details = hass.states.get("sensor.home_plan_details")
        assert plan_details is not None
        return plan_details.attributes["battery_battery_action"]

    with patch(
        "custom_components.wattplan.optimizer.optimize",
        side_effect=_fake_optimize_with_target_behavior,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert await optimize_and_read_actions() == ["sc", "sc", "sc", "sc"]

        target_at = dt_util.utcnow() + timedelta(hours=2)
        await _set_battery_target(hass, target_at)
        assert await optimize_and_read_actions() == ["gc", "gc", "sc", "sc"]
        target_sensor = hass.states.get("sensor.home_battery_target")
        assert target_sensor is not None
        assert float(target_sensor.state) == 8.0

        await hass.services.async_call(
            DOMAIN, SERVICE_CLEAR_TARGET, {"battery": "battery"}, blocking=True
        )
        await hass.async_block_till_done()
        target_sensor = hass.states.get("sensor.home_battery_target")
        assert target_sensor is not None
        assert target_sensor.state == STATE_UNKNOWN
        assert target_sensor.attributes["by"] == "not_set"
        assert await optimize_and_read_actions() == ["sc", "sc", "sc", "sc"]

        await _set_battery_target(hass, target_at)
        assert await optimize_and_read_actions() == ["gc", "gc", "sc", "sc"]

        expired_at = target_at + timedelta(minutes=1)
        async_fire_time_changed(hass, expired_at)
        await hass.async_block_till_done()

        with patch(
            "custom_components.wattplan.target_runtime.dt_util.utcnow",
            return_value=expired_at,
        ):
            assert await optimize_and_read_actions() == ["sc", "sc", "sc", "sc"]
            target_sensor = hass.states.get("sensor.home_battery_target")
            assert target_sensor is not None
            assert target_sensor.state == STATE_UNKNOWN
            assert target_sensor.attributes["by"] == "not_set"


async def test_button_entities_registered_and_pressable(hass: HomeAssistant) -> None:
    """Optimize replans; Refresh Sensors re-emits the existing plan."""
    entry = _entry(
        title="Home",
        subentries_data=[_battery_subentry(subentry_id="battery_sub", name="battery")],
    )

    with patch("custom_components.wattplan.optimizer.optimize", side_effect=_fake_optimize):
        await _setup_entry(hass, entry)

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

        snapshot_after_optimize = coordinator.snapshot
        assert snapshot_after_optimize is not None, "Snapshot should exist after pressing Run Optimize Now"
        assert snapshot_after_optimize is not snapshot_before, "Pressing button should produce a new snapshot"
        assert hass.states.get("sensor.home_battery_action").state == "grid_charge"

        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": "button.home_refresh_sensors"},
            blocking=True,
        )
        await hass.async_block_till_done()

    # Refresh re-emits the same plan (a new snapshot copy), it does not replan.
    assert coordinator.snapshot.created_at == snapshot_after_optimize.created_at
    assert hass.states.get("sensor.home_battery_action").state == "grid_charge"


async def test_button_optimize_raises_when_already_running(hass: HomeAssistant) -> None:
    """Pressing the optimize button while a plan is running raises ServiceValidationError."""
    entry = _entry(title="Home", subentries_data=[])

    with patch("custom_components.wattplan.optimizer.optimize", side_effect=_fake_optimize):
        await _setup_entry(hass, entry)

    coordinator = entry.runtime_data.coordinator

    async with coordinator._plan_lock:
        with pytest.raises(ServiceValidationError):
            await coordinator.async_plan(trigger=CycleTrigger.SERVICE)
