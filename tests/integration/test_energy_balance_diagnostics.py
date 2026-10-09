"""Independent energy balance diagnostics; no planner solves are needed."""

from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfPower
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.wattplan.const import (
    CONF_HISTORICAL_BATTERY_CHARGE_SENSOR,
    CONF_HISTORICAL_BATTERY_DISCHARGE_SENSOR,
    CONF_HISTORICAL_GRID_IMPORT_SENSOR,
    CONF_HISTORICAL_SIMULATE_SELF_CONSUMPTION,
    CONF_HISTORICAL_USAGE_SENSOR,
    DOMAIN,
    SUBENTRY_TYPE_BATTERY,
)
from custom_components.wattplan.historical_cost.energy_balance import (
    ENERGY_BALANCE_KEYS,
    energy_balance_delta,
)
from custom_components.wattplan.historical_cost.models import (
    FLAG_MISSING_IMPORT_PRICE,
    HistoricalMetric,
    PERIOD_LAST_SLOT,
    PERIOD_TODAY,
    SCENARIO_ACTUAL,
    SlotRecord,
)
from custom_components.wattplan.historical_cost.tracker import HistoricalCostTracker
from custom_components.wattplan.sensors.historical import build_historical_sensors

START = datetime(2026, 10, 9, 10, tzinfo=UTC)


def _tracker(hass, *, battery=True, meters=True):
    options = {
        CONF_HISTORICAL_GRID_IMPORT_SENSOR: "sensor.grid",
        CONF_HISTORICAL_USAGE_SENSOR: "sensor.load",
        CONF_HISTORICAL_SIMULATE_SELF_CONSUMPTION: False,
    }
    if meters:
        options.update({
            CONF_HISTORICAL_BATTERY_CHARGE_SENSOR: "sensor.charge",
            CONF_HISTORICAL_BATTERY_DISCHARGE_SENSOR: "sensor.discharge",
        })
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        options=options,
        subentries_data=[
            ConfigSubentryData(
                subentry_id="battery", subentry_type=SUBENTRY_TYPE_BATTERY,
                title="Battery", unique_id="battery", data={},
            )
        ] if battery else [],
    )
    return HistoricalCostTracker(hass, entry, slot_minutes=15)


def _battery(hass, charge=100, discharge=200, *, unit="kWh"):
    attributes = {
        "device_class": "energy",
        "unit_of_measurement": unit,
        "state_class": "total_increasing",
    }
    hass.states.async_set("sensor.charge", str(charge), attributes)
    hass.states.async_set("sensor.discharge", str(discharge), attributes)


def _core(grid=10, load=20):
    return {"grid_import": grid, "usage": load, "pv": 0, "grid_export": 0}


@pytest.mark.parametrize(
    "flows",
    [
        {"grid_import": .52, "usage": .5},
        {"pv": .72, "usage": .5, "grid_export": .2},
        {"battery_discharge": .52, "usage": .5},
        {"grid_import": 1.52, "usage": .5, "battery_charge": 1},
        # A household spike seen in both meters cancels.
        {"grid_import": .62, "usage": .6},
    ],
)
def test_balance_across_supply_modes_and_household_spikes(flows):
    previous = dict.fromkeys(ENERGY_BALANCE_KEYS, 100.0)
    current = {key: value + flows.get(key, 0) for key, value in previous.items()}
    assert energy_balance_delta(previous, current) == pytest.approx(.02)


def test_signed_timing_errors_cancel_instead_of_being_clipped():
    zero = dict.fromkeys(ENERGY_BALANCE_KEYS, 0.0)
    first = {**zero, "grid_import": .12}
    second = {**zero, "grid_import": .14, "usage": .1}
    positive = energy_balance_delta(zero, first)
    negative = energy_balance_delta(first, second)
    assert negative == pytest.approx(-.08)
    assert positive + negative == pytest.approx(.04)


@pytest.mark.parametrize("invalid", [None, float("nan"), float("inf"), "invalid", -1])
def test_invalid_or_reset_counter_is_not_zero_discrepancy(invalid):
    previous = dict.fromkeys(ENERGY_BALANCE_KEYS, 0.0)
    current = {**previous, "battery_charge": invalid}
    assert energy_balance_delta(previous, current) is None


def test_partial_seed_gap_and_meter_recovery_do_not_mix_windows(hass):
    tracker = _tracker(hass)
    _battery(hass)
    tracker._sample_energy_balance(START + timedelta(minutes=3), _core())
    assert tracker._sample_energy_balance(
        START + timedelta(minutes=15, seconds=5), _core(10.52, 20.5),
        completed_slot=START,
    ) is None
    assert tracker._sample_energy_balance(
        START + timedelta(minutes=30, seconds=5), _core(11.04, 21),
        completed_slot=START + timedelta(minutes=15),
    ) == pytest.approx(.02)

    # Missing readings and their recovery are both excluded from the diagnostic.
    _battery(hass, charge="unavailable")
    assert tracker._sample_energy_balance(
        START + timedelta(minutes=45, seconds=5), _core(11.56, 21.5),
        completed_slot=START + timedelta(minutes=30),
    ) is None
    _battery(hass)
    assert tracker._sample_energy_balance(
        START + timedelta(minutes=60, seconds=5), _core(12.08, 22),
        completed_slot=START + timedelta(minutes=45),
    ) is None
    assert tracker._sample_energy_balance(
        START + timedelta(minutes=75, seconds=5), _core(12.6, 22.5),
        completed_slot=START + timedelta(minutes=60),
    ) == pytest.approx(.02)
    assert tracker._sample_energy_balance(
        START + timedelta(minutes=105, seconds=5), _core(13.64, 23.5),
        completed_slot=START + timedelta(minutes=90),
    ) is None


def test_battery_counter_reset_discards_one_window_then_recovers(hass):
    tracker = _tracker(hass)
    _battery(hass)
    tracker._sample_energy_balance(START, _core())
    _battery(hass, charge=1)
    assert tracker._sample_energy_balance(
        START + timedelta(minutes=15), _core(10.52, 20.5),
        completed_slot=START,
    ) is None
    assert tracker._sample_energy_balance(
        START + timedelta(minutes=30), _core(11.04, 21),
        completed_slot=START + timedelta(minutes=15),
    ) == pytest.approx(.02)


def test_battery_meters_are_required_without_using_soc_as_inactivity(hass):
    tracker = _tracker(hass, meters=False)
    assert tracker.energy_balance_attributes()["configuration_status"] == "battery_meters_missing"
    tracker._sample_energy_balance(START, _core())
    assert tracker._sample_energy_balance(
        START + timedelta(minutes=15), _core(10.52, 20.5),
        completed_slot=START,
    ) is None
    sensor = build_historical_sensors(tracker.entry, tracker, entry_slug="home")[0]
    assert not sensor.available
    assert sensor.native_value is None
    assert sensor.extra_state_attributes["missing_battery_meters"] == [
        "battery_charge", "battery_discharge",
    ]


def test_no_battery_and_no_battery_meters_can_report(hass):
    tracker = _tracker(hass, battery=False, meters=False)
    tracker._sample_energy_balance(START, _core())
    assert tracker._sample_energy_balance(
        START + timedelta(minutes=15), _core(10.52, 20.5),
        completed_slot=START,
    ) == pytest.approx(.02)


def _record(start=START, **changes):
    values = dict(
        start=start, import_price=2, export_price=0,
        grid_import=.52, grid_export=0, usage=.5, pv=0,
        energy_balance_discrepancy=.02,
    )
    values.update(changes)
    return SlotRecord(**values)


async def test_sensor_units_signed_totals_and_coverage(hass, freezer):
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to(START + timedelta(minutes=45, seconds=5))
    tracker = _tracker(hass, battery=False, meters=False)
    tracker.store.append_slots([
        _record(flags=FLAG_MISSING_IMPORT_PRICE, import_price=None),
        _record(START + timedelta(minutes=15), energy_balance_discrepancy=None),
        _record(START + timedelta(minutes=30), energy_balance_discrepancy=-.01),
    ])
    sensors = {
        sensor._description.key: sensor
        for sensor in build_historical_sensors(tracker.entry, tracker, entry_slug="home")
    }
    power = sensors["energy_balance_discrepancy"]
    energy = sensors["energy_balance_discrepancy_today"]
    assert power.available and energy.available
    assert power.native_value == pytest.approx(-40)
    assert energy.native_value == pytest.approx(10)
    assert power.native_unit_of_measurement == UnitOfPower.WATT
    assert energy.native_unit_of_measurement == UnitOfEnergy.WATT_HOUR
    assert power.device_class == SensorDeviceClass.POWER
    assert energy.device_class == SensorDeviceClass.ENERGY
    assert power.state_class == SensorStateClass.MEASUREMENT
    assert energy.state_class == SensorStateClass.TOTAL
    assert power.entity_category == EntityCategory.DIAGNOSTIC
    assert energy.last_reset == START.replace(hour=0)
    attributes = energy.extra_state_attributes
    assert attributes["valid_slots"] == 2
    assert attributes["missing_slots"] == 1
    assert attributes["covered_hours"] == .5
    assert attributes["average_discrepancy_w"] == pytest.approx(20)
    # A missing latest slot is unavailable, not the last good reading or zero.
    tracker.store.append_slot(
        _record(START + timedelta(minutes=45), energy_balance_discrepancy=None)
    )
    freezer.move_to(START + timedelta(minutes=60, seconds=5))
    assert not power.available
    assert power.native_value is None
    assert energy.native_value == pytest.approx(10)


async def test_daily_reset_preserves_previous_day_and_local_timezone(hass, freezer):
    await hass.config.async_set_time_zone("Europe/Copenhagen")
    # Midnight local time is 22:00 UTC in October.
    start = datetime(2026, 10, 9, 21, 45, tzinfo=UTC)
    freezer.move_to(start + timedelta(minutes=15, seconds=5))
    tracker = _tracker(hass, battery=False, meters=False)
    tracker.store.append_slot(_record(start))
    energy = next(
        sensor for sensor in build_historical_sensors(tracker.entry, tracker, entry_slug="home")
        if sensor._description.key == "energy_balance_discrepancy_today"
    )
    assert not energy.available  # No completed slot in the new local day.
    assert energy.last_reset is None
    tracker.store.append_slot(_record(start + timedelta(minutes=15), energy_balance_discrepancy=-.01))
    freezer.move_to(start + timedelta(minutes=30, seconds=5))
    assert energy.native_value == pytest.approx(-10)
    assert energy.last_reset == start + timedelta(minutes=15)


async def test_legacy_days_do_not_shift_new_discrepancies_into_old_slots(hass, freezer):
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to(START + timedelta(minutes=30, seconds=5))
    tracker = _tracker(hass, battery=False, meters=False)
    tracker.store.append_slot(_record())
    day = tracker.store.data["days"]["2026-10-09"]
    day.pop("energy_balance_discrepancy")
    tracker.store.data = tracker.store._migrate(tracker.store.data)
    tracker.store.append_slot(
        _record(START + timedelta(minutes=15), energy_balance_discrepancy=-.01)
    )
    day = tracker.store.data["days"]["2026-10-09"]
    assert day["energy_balance_discrepancy"] == [None, -.01]
    summary = tracker.store.summary(
        metric=HistoricalMetric.ENERGY_BALANCE_DISCREPANCY,
        period=PERIOD_TODAY, scenario=None,
    )
    assert summary.value == pytest.approx(-.01)
    assert summary.missing_slots == 1
    actual = tracker.store.summary(
        metric=HistoricalMetric.COST, period=PERIOD_TODAY, scenario=SCENARIO_ACTUAL,
    )
    assert actual.value == pytest.approx(2.08)


async def test_missing_battery_meter_does_not_change_actual_cost(
    hass, freezer,
):
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to(START)
    tracker = _tracker(hass)
    await tracker.store.async_load()
    _battery(hass)
    hass.states.async_set("sensor.grid", "10")
    hass.states.async_set("sensor.load", "20")
    tracker.remember_price_series(
        start_at=START, slot_minutes=15,
        import_prices=[2] * 4, export_prices=[0] * 4,
    )
    await tracker._async_seed(START)
    freezer.move_to(START + timedelta(minutes=15, seconds=5))
    _battery(hass, charge="unavailable")
    hass.states.async_set("sensor.grid", "10.52")
    hass.states.async_set("sensor.load", "20.5")
    await tracker.async_process_completed_slot()
    actual = tracker.summary(
        metric=HistoricalMetric.COST, period=PERIOD_TODAY, scenario=SCENARIO_ACTUAL,
    )
    diagnostic = tracker.summary(
        metric=HistoricalMetric.ENERGY_BALANCE_DISCREPANCY,
        period=PERIOD_LAST_SLOT, scenario=None,
    )
    assert actual.value == pytest.approx(1.04)
    assert diagnostic.value is None
    assert diagnostic.missing_slots == 1

    # Diagnostic configuration changes reseed only its own cursor.
    core_cursor = tracker.store.last_meter_values()
    config = tracker._energy_balance_config()
    tracker.store.data["energy_balance_cursor"]["config"] = {**config, "battery_charge": "old"}
    _battery(hass)
    assert tracker._sample_energy_balance(
        START + timedelta(minutes=30, seconds=5), _core(11.04, 21),
        completed_slot=START + timedelta(minutes=15),
    ) is None
    assert tracker.store.last_meter_values() == core_cursor
    await tracker.async_shutdown()


def test_changed_battery_units_cannot_produce_a_large_false_discrepancy(hass):
    tracker = _tracker(hass)
    _battery(hass)
    tracker._sample_energy_balance(START, _core())
    _battery(hass, charge=100000, discharge=200000, unit="Wh")
    assert tracker._sample_energy_balance(
        START + timedelta(minutes=15), _core(10.52, 20.5),
        completed_slot=START,
    ) is None
    # Restoring the units does not compare kWh counters with the rejected Wh reading.
    _battery(hass)
    assert tracker._sample_energy_balance(
        START + timedelta(minutes=30), _core(11.04, 21),
        completed_slot=START + timedelta(minutes=15),
    ) is None
    assert tracker._sample_energy_balance(
        START + timedelta(minutes=45), _core(11.56, 21.5),
        completed_slot=START + timedelta(minutes=30),
    ) == pytest.approx(.02)
