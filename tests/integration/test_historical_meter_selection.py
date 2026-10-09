"""Historical meter selectors and validation must agree on cumulative kWh."""

import pytest

from custom_components.wattplan.const import (
    CONF_HISTORICAL_BATTERY_CHARGE_SENSOR,
    CONF_HISTORICAL_BATTERY_DISCHARGE_SENSOR,
    CONF_HISTORICAL_GRID_EXPORT_SENSOR,
    CONF_HISTORICAL_GRID_IMPORT_SENSOR,
    CONF_HISTORICAL_PV_SENSOR,
    CONF_HISTORICAL_USAGE_SENSOR,
)
from custom_components.wattplan.flows.main import _historical_costs_settings_schema
from custom_components.wattplan.historical_cost.tracker import validate_energy_sensor


@pytest.mark.parametrize(
    ("unit", "state_class", "device_class", "valid"),
    [
        ("kWh", "total", "energy", True),
        ("kWh", "total_increasing", "energy", True),
        ("Wh", "total_increasing", "energy", False),
        ("MWh", "total_increasing", "energy", False),
        (None, "total_increasing", "energy", False),
        ("kWh", "measurement", "energy", False),
        ("kWh", None, "energy", False),
        ("kW", "measurement", "power", False),
        ("%", "measurement", "battery", False),
        ("kWh", "total", None, False),
    ],
)
def test_meter_validation_checks_units_class_and_cumulative_semantics(
    hass, unit, state_class, device_class, valid,
):
    hass.states.async_set("sensor.candidate", "1", {
        "device_class": device_class,
        "unit_of_measurement": unit,
        "state_class": state_class,
    })
    assert validate_energy_sensor(hass, "sensor.candidate") is valid


def test_all_historical_selectors_hide_incompatible_loaded_energy_sensors(hass):
    valid = {"device_class": "energy", "unit_of_measurement": "kWh", "state_class": "total_increasing"}
    hass.states.async_set("sensor.running_energy", "10", valid)
    hass.states.async_set("sensor.offline_energy", "unavailable", valid)
    hass.states.async_set("sensor.wh_total", "10000", {**valid, "unit_of_measurement": "Wh"})
    hass.states.async_set("sensor.remaining_capacity", "5", {**valid, "state_class": "measurement"})
    hass.states.async_set("sensor.missing_state_class", "10", {**valid, "state_class": None})
    schema = _historical_costs_settings_schema({}, hass).schema
    fields = {
        CONF_HISTORICAL_GRID_IMPORT_SENSOR,
        CONF_HISTORICAL_GRID_EXPORT_SENSOR,
        CONF_HISTORICAL_USAGE_SENSOR,
        CONF_HISTORICAL_PV_SENSOR,
        CONF_HISTORICAL_BATTERY_CHARGE_SENSOR,
        CONF_HISTORICAL_BATTERY_DISCHARGE_SENSOR,
    }
    for marker, energy_selector in schema.items():
        if marker.schema not in fields:
            continue
        assert energy_selector.config["domain"] == ["sensor"]
        assert energy_selector.config["device_class"] == ["energy"]
        excluded = energy_selector.config["exclude_entities"]
        assert "sensor.wh_total" in excluded
        assert "sensor.remaining_capacity" in excluded
        assert "sensor.missing_state_class" in excluded
        assert "sensor.running_energy" not in excluded
        assert "sensor.offline_energy" not in excluded
        assert energy_selector("sensor.running_energy") == "sensor.running_energy"
        assert energy_selector("sensor.offline_energy") == "sensor.offline_energy"


def test_unloaded_entity_validation_is_deferred(hass):
    assert validate_energy_sensor(hass, "sensor.not_loaded_yet")
    assert not validate_energy_sensor(hass, "number.charge_limit")
    assert not validate_energy_sensor(hass, None)
