"""Test the WattPlan config flow."""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
import voluptuous_serialize
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.wattplan.const import (
    CONF_ACTION_EMISSION_ENABLED,
    CONF_AVAILABILITY_SOURCE,
    CONF_CAPACITY_KWH,
    CONF_CONFIG_ENTRY_ID,
    CONF_HISTORICAL_BATTERY_CHARGE_SENSOR,
    CONF_HISTORICAL_BATTERY_DISCHARGE_SENSOR,
    CONF_HISTORICAL_COST_TRACKING_ENABLED,
    CONF_HISTORICAL_GRID_EXPORT_SENSOR,
    CONF_HISTORICAL_GRID_IMPORT_SENSOR,
    CONF_HISTORICAL_PV_SENSOR,
    CONF_HISTORICAL_SIMULATE_SELF_CONSUMPTION,
    CONF_HISTORICAL_USAGE_SENSOR,
    CONF_HOURS_TO_PLAN,
    CONF_MAX_CONSECUTIVE_OFF_MINUTES,
    CONF_MIN_CONSECUTIVE_ON_MINUTES,
    CONF_MINIMUM_KWH,
    CONF_ON_OFF_SOURCE,
    CONF_OPTIMIZER_LOOKAHEAD_SLOTS,
    CONF_OUTLOOK_LANGUAGES,
    CONF_PLANNER_REPRODUCTION_RETENTION_DAYS,
    CONF_PLANNING_ENABLED,
    CONF_PLANNER_PRESET,
    CONF_RECORD_PLANNER_REPRODUCTIONS,
    CONF_SHOW_EXPERIMENTAL_PLANNERS,
    CONF_SLOT_MINUTES,
    CONF_SOC_SOURCE,
    CONF_SOURCE_MODE,
    CONF_SOURCE_PV,
    CONF_SOURCE_USAGE,
    CONF_SOURCES,
    CONF_TARGET_ON_HOURS_PER_WINDOW,
    CONF_TEMPLATE,
    DOMAIN,
    SOURCE_MODE_BUILT_IN,
    SOURCE_MODE_ENERGY_PROVIDER,
    SOURCE_MODE_ENTITY_ADAPTER,
    SOURCE_MODE_NOT_USED,
    SOURCE_MODE_TEMPLATE,
    SUBENTRY_TYPE_BATTERY,
    SUBENTRY_TYPE_COMFORT,
    SUBENTRY_TYPE_OPTIONAL,
)
from custom_components.wattplan.source_providers import CONF_WATTPLAN_ENTITY_ID
from tests.common import MockConfigEntry

from .flow_helpers import (
    CONF_ACCEPT_SOURCE_SUMMARY,
    battery_input,
    comfort_input,
    optional_input,
    submit,
    walk_template_sources,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "frozen_clock")

CONF_ACCEPT_MANUAL_SCHEDULING = "accept_manual_scheduling"


def _series_template(hours: int) -> str:
    """Create a static template that renders canonical source objects."""
    start = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
    payload = [
        {"start": (start + timedelta(hours=idx)).isoformat(), "value": float(idx)}
        for idx in range(hours)
    ]
    return f"{{{{ {payload!r} }}}}"


def _default_source_mode(result: dict[str, Any]) -> str:
    """Extract the default source-mode value from a flow form schema."""
    schema = result["data_schema"].schema
    marker = next(key for key in schema if getattr(key, "schema", None) == CONF_SOURCE_MODE)
    return marker.default()


def _schema_default(result: dict[str, Any], field: str) -> Any:
    """Extract a default value from a flow form schema."""
    schema = result["data_schema"].schema
    marker = next(key for key in schema if getattr(key, "schema", None) == field)
    if not callable(marker.default):
        return None
    return marker.default()


def _schema_suggested(result: dict[str, Any], field: str) -> Any:
    """Extract a suggested value from a flow form schema."""
    schema = result["data_schema"].schema
    marker = next(key for key in schema if getattr(key, "schema", None) == field)
    return (marker.description or {}).get("suggested_value")


def _serialized_schema_field(result: dict[str, Any], field: str) -> dict[str, Any]:
    """Return a serialized schema field from a flow form."""
    return next(
        item
        for item in voluptuous_serialize.convert(
            result["data_schema"], custom_serializer=cv.custom_serializer
        )
        if item.get("name") == field
    )


def _set_energy_sensor(hass: HomeAssistant, entity_id: str, value: str = "1.0") -> None:
    """Set a cumulative kWh sensor state."""
    hass.states.async_set(
        entity_id,
        value,
        {
            "device_class": "energy",
            "unit_of_measurement": "kWh",
            "state_class": "total_increasing",
        },
    )


def _register_sensor_on_device(
    hass: HomeAssistant,
    config_entry: config_entries.ConfigEntry,
    *,
    device_id: str,
    entity_id: str,
    device_class: str,
    unit: str,
) -> None:
    """Register a test sensor on a device and set its current state."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("test", device_id)},
    )
    entry = er.async_get(hass).async_get_or_create(
        "sensor",
        "test",
        entity_id,
        config_entry=config_entry,
        device_id=device.id,
        suggested_object_id=entity_id.removeprefix("sensor."),
        original_device_class=device_class,
        unit_of_measurement=unit,
    )
    attributes = {
        "device_class": device_class,
        "unit_of_measurement": unit,
    }
    if device_class == "energy":
        attributes["state_class"] = "total_increasing"
    hass.states.async_set(entry.entity_id, "1.0", attributes)


async def _finish_setup_if_needed(
    hass: HomeAssistant, result: dict[str, Any]
) -> dict[str, Any]:
    """Advance through any final setup forms before entry creation."""
    while result["type"] is FlowResultType.FORM:
        if result["step_id"] == "source_export_price":
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED},
            )
            continue
        if result["step_id"] == "setup_complete":
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {}
            )
            continue
        break
    return result


async def _create_basic_entry(hass: HomeAssistant) -> config_entries.ConfigEntry:
    """Create a basic WattPlan config entry for subentry tests."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Home",
            CONF_SLOT_MINUTES: "60",
            CONF_HOURS_TO_PLAN: "24",
        },
    )
    result = await walk_template_sources(hass, result["flow_id"], _series_template(24))
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED},
    )
    result = await _finish_setup_if_needed(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry is not None
    return entry


async def _open_historical_settings(
    hass: HomeAssistant, entry: config_entries.ConfigEntry
) -> dict[str, Any]:
    """Open the historical settings form by enabling tracking in the intro step."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "historical_costs"}
    )
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_HISTORICAL_COST_TRACKING_ENABLED: True}
    )


async def _open_planner_timers_options(
    hass: HomeAssistant, entry: config_entries.ConfigEntry
) -> dict[str, Any]:
    """Open the scheduler settings options step."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "planner_timers"}
    )


async def test_form(hass: HomeAssistant, mock_setup_entry: AsyncMock) -> None:
    """Test we can create a config entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "requirements"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "planner_setup"
    assert _schema_default(result, CONF_PLANNER_PRESET) == "local-search-3"
    schema_fields = {getattr(field, "schema", None) for field in result["data_schema"].schema}
    assert "optimizer_lookahead_hours" not in schema_fields
    assert "optimizer_profile" not in schema_fields
    assert all(
        getattr(field, "schema", None) != CONF_OUTLOOK_LANGUAGES
        for field in result["data_schema"].schema
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Home",
            CONF_SLOT_MINUTES: "60",
            CONF_HOURS_TO_PLAN: "12",
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "source_price"
    assert _default_source_mode(result) == "entity_adapter"

    template = _series_template(12)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "source_price_template"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_TEMPLATE: template},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "source_review"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_ACCEPT_SOURCE_SUMMARY: True},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "source_usage"
    assert _default_source_mode(result) == "built_in"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "source_usage_template"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_TEMPLATE: template},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_ACCEPT_SOURCE_SUMMARY: True},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "source_pv"
    assert _default_source_mode(result) == "not_used"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED},
    )
    result = await _finish_setup_if_needed(hass, result)
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Home"
    assert len(mock_setup_entry.mock_calls) == 1
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.options[CONF_PLANNER_PRESET] == "local-search-3"
    assert CONF_OPTIMIZER_LOOKAHEAD_SLOTS not in entry.options


async def test_multiple_setups_allowed(
    hass: HomeAssistant, mock_setup_entry: AsyncMock
) -> None:
    """Test that multiple config entries can be created."""
    await _create_basic_entry(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "requirements"


async def test_export_price_step_is_shown_only_when_pv_is_configured(
    hass: HomeAssistant, mock_setup_entry: AsyncMock
) -> None:
    """Export price should only be offered when PV is configured."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Home",
            CONF_SLOT_MINUTES: "60",
            CONF_HOURS_TO_PLAN: "12",
        },
    )
    template = _series_template(12)
    result = await walk_template_sources(hass, result["flow_id"], template)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TEMPLATE: template}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_ACCEPT_SOURCE_SUMMARY: True}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "source_export_price"
    assert _default_source_mode(result) == "not_used"


async def test_source_review_reports_template_that_renders_text(
    hass: HomeAssistant, mock_setup_entry: AsyncMock
) -> None:
    """A template that is not a list of values is rejected on its own step."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: "Home", CONF_SLOT_MINUTES: "60", CONF_HOURS_TO_PLAN: "12"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TEMPLATE: "{{ 'not a series' }}"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "source_review"
    assert result["errors"] == {"base": "template_invalid_structure"}


async def test_source_review_reports_missing_built_in_entity(
    hass: HomeAssistant, mock_setup_entry: AsyncMock
) -> None:
    """The built-in usage source must point at an existing entity."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: "Home", CONF_SLOT_MINUTES: "60", CONF_HOURS_TO_PLAN: "12"},
    )
    template = _series_template(12)
    for user_input in (
        {CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE},
        {CONF_TEMPLATE: template},
        {CONF_ACCEPT_SOURCE_SUMMARY: True},
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input
        )
    assert result["step_id"] == "source_usage"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_SOURCE_MODE: SOURCE_MODE_BUILT_IN}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_WATTPLAN_ENTITY_ID: "sensor.missing_usage_total"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "source_review"
    assert result["errors"] == {"base": "entity_not_found"}


async def test_options_flow_add_core_and_one_of_each_asset(
    hass: HomeAssistant, mock_setup_entry: AsyncMock
) -> None:
    """Test creating core config and adding one battery, comfort, and optional subentry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Home",
            CONF_SLOT_MINUTES: "60",
            CONF_HOURS_TO_PLAN: "24",
        },
    )
    template = _series_template(24)
    result = await walk_template_sources(hass, result["flow_id"], template)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED},
    )
    result = await _finish_setup_if_needed(hass, result)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY

    entry = hass.config_entries.async_entries(DOMAIN)[0]

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert "source_export_price" in result["menu_options"]
    assert "historical_costs" in result["menu_options"]
    assert "outlook_languages" in result["menu_options"]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "source_price"}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE},
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_TEMPLATE: template},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "source_review"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_ACCEPT_SOURCE_SUMMARY: True},
    )
    assert result["type"] is FlowResultType.MENU

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "historical_costs"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "historical_costs"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_HISTORICAL_COST_TRACKING_ENABLED: True,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "historical_costs_settings"
    serialized_fields = {
        item.get("name")
        for item in voluptuous_serialize.convert(
            result["data_schema"], custom_serializer=cv.custom_serializer
        )
    }
    assert "historical_simulate_no_battery" not in serialized_fields
    assert CONF_HISTORICAL_SIMULATE_SELF_CONSUMPTION in serialized_fields
    assert CONF_HISTORICAL_BATTERY_CHARGE_SENSOR in serialized_fields
    assert CONF_HISTORICAL_BATTERY_DISCHARGE_SENSOR in serialized_fields
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_HISTORICAL_GRID_IMPORT_SENSOR: "sensor.grid_import_total",
            CONF_HISTORICAL_GRID_EXPORT_SENSOR: "sensor.grid_export_total",
            CONF_HISTORICAL_USAGE_SENSOR: "sensor.usage_total",
            CONF_HISTORICAL_PV_SENSOR: "sensor.pv_total",
            CONF_HISTORICAL_BATTERY_CHARGE_SENSOR: "sensor.battery_charge_total",
            CONF_HISTORICAL_BATTERY_DISCHARGE_SENSOR: "sensor.battery_discharge_total",
            CONF_HISTORICAL_SIMULATE_SELF_CONSUMPTION: False,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated is not None
    assert updated.options[CONF_HISTORICAL_COST_TRACKING_ENABLED] is True
    assert (
        updated.options[CONF_HISTORICAL_GRID_IMPORT_SENSOR]
        == "sensor.grid_import_total"
    )
    assert "historical_simulate_no_battery" not in updated.options
    assert updated.options[CONF_HISTORICAL_SIMULATE_SELF_CONSUMPTION] is False
    assert updated.options[CONF_HISTORICAL_BATTERY_CHARGE_SENSOR] == "sensor.battery_charge_total"
    assert updated.options[CONF_HISTORICAL_BATTERY_DISCHARGE_SENSOR] == "sensor.battery_discharge_total"
    assert CONF_SOURCES in updated.data

    for subentry_type, data in (
        (
            SUBENTRY_TYPE_BATTERY,
            battery_input(
                "Car battery",
                **{
                    CONF_SOC_SOURCE: "sensor.car_soc",
                    CONF_AVAILABILITY_SOURCE: "binary_sensor.car_available",
                    CONF_CAPACITY_KWH: 70,
                    CONF_MINIMUM_KWH: 10,
                },
            ),
        ),
        (SUBENTRY_TYPE_COMFORT, comfort_input("House heat", 24)),
        (SUBENTRY_TYPE_OPTIONAL, optional_input("Dishwasher")),
    ):
        result = await submit(hass, entry.entry_id, subentry_type, data)
        assert result["type"] is FlowResultType.CREATE_ENTRY

    await hass.async_block_till_done()
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated is not None
    assert len(updated.subentries) == 3
    assert any(
        subentry.subentry_type == SUBENTRY_TYPE_BATTERY
        and subentry.title == "Car battery (70 kWh, min 10 kWh)"
        and subentry.data[CONF_AVAILABILITY_SOURCE] == "binary_sensor.car_available"
        for subentry in updated.subentries.values()
    )


async def test_historical_costs_disabled_intro_closes_flow(
    hass: HomeAssistant, mock_setup_entry: AsyncMock
) -> None:
    """Historical cost intro should save disabled and close without details."""
    entry = await _create_basic_entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "historical_costs"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "historical_costs"
    assert _schema_default(result, CONF_HISTORICAL_COST_TRACKING_ENABLED) is False

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_HISTORICAL_COST_TRACKING_ENABLED: False},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated is not None
    assert updated.options[CONF_HISTORICAL_COST_TRACKING_ENABLED] is False


async def test_historical_costs_prefills_discovered_source_meters(
    hass: HomeAssistant, mock_setup_entry: AsyncMock
) -> None:
    """First-time historical enablement should suggest unambiguous meters."""
    entry = await _create_basic_entry(hass)
    source_entry = MockConfigEntry(domain="test", entry_id="source-entry")
    source_entry.add_to_hass(hass)
    _set_energy_sensor(hass, "sensor.house_usage_total")
    _register_sensor_on_device(
        hass,
        source_entry,
        device_id="pv-inverter",
        entity_id="sensor.pv_power",
        device_class="power",
        unit="W",
    )
    _register_sensor_on_device(
        hass,
        source_entry,
        device_id="pv-inverter",
        entity_id="sensor.pv_energy_total",
        device_class="energy",
        unit="kWh",
    )
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_SOURCES: {
                **entry.data[CONF_SOURCES],
                CONF_SOURCE_USAGE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_BUILT_IN,
                    CONF_WATTPLAN_ENTITY_ID: "sensor.house_usage_total",
                },
                CONF_SOURCE_PV: {
                    CONF_SOURCE_MODE: SOURCE_MODE_ENTITY_ADAPTER,
                    CONF_WATTPLAN_ENTITY_ID: "sensor.pv_power",
                },
            },
        },
    )

    result = await _open_historical_settings(hass, entry)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "historical_costs_settings"
    assert (
        _schema_suggested(result, CONF_HISTORICAL_USAGE_SENSOR)
        == "sensor.house_usage_total"
    )
    assert _schema_suggested(result, CONF_HISTORICAL_PV_SENSOR) == "sensor.pv_energy_total"
    assert _schema_suggested(result, CONF_HISTORICAL_GRID_IMPORT_SENSOR) is None
    assert _schema_suggested(result, CONF_HISTORICAL_GRID_EXPORT_SENSOR) is None
    assert "default" not in _serialized_schema_field(
        result, CONF_HISTORICAL_GRID_IMPORT_SENSOR
    )
    assert "default" not in _serialized_schema_field(
        result, CONF_HISTORICAL_GRID_EXPORT_SENSOR
    )


async def test_historical_costs_prefills_energy_provider_owned_meter(
    hass: HomeAssistant, mock_setup_entry: AsyncMock
) -> None:
    """Energy-provider sources should suggest one owned cumulative meter."""
    entry = await _create_basic_entry(hass)
    solar_entry = MockConfigEntry(domain="forecast_solar", entry_id="solar-entry")
    solar_entry.add_to_hass(hass)
    _register_sensor_on_device(
        hass,
        solar_entry,
        device_id="solar-system",
        entity_id="sensor.solar_power",
        device_class="power",
        unit="W",
    )
    _register_sensor_on_device(
        hass,
        solar_entry,
        device_id="solar-system",
        entity_id="sensor.solar_energy_total",
        device_class="energy",
        unit="kWh",
    )
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_SOURCES: {
                **entry.data[CONF_SOURCES],
                CONF_SOURCE_PV: {
                    CONF_SOURCE_MODE: SOURCE_MODE_ENERGY_PROVIDER,
                    CONF_CONFIG_ENTRY_ID: solar_entry.entry_id,
                },
            },
        },
    )

    result = await _open_historical_settings(hass, entry)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "historical_costs_settings"
    assert (
        _schema_suggested(result, CONF_HISTORICAL_PV_SENSOR)
        == "sensor.solar_energy_total"
    )


async def test_historical_costs_does_not_prefill_existing_blank_option(
    hass: HomeAssistant, mock_setup_entry: AsyncMock
) -> None:
    """Already-enabled historical settings should keep intentionally blank fields."""
    entry = await _create_basic_entry(hass)
    _set_energy_sensor(hass, "sensor.house_usage_total")
    _set_energy_sensor(hass, "sensor.pv_energy_total")
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_SOURCES: {
                **entry.data[CONF_SOURCES],
                CONF_SOURCE_USAGE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_BUILT_IN,
                    CONF_WATTPLAN_ENTITY_ID: "sensor.house_usage_total",
                },
                CONF_SOURCE_PV: {
                    CONF_SOURCE_MODE: SOURCE_MODE_ENTITY_ADAPTER,
                    CONF_WATTPLAN_ENTITY_ID: "sensor.pv_energy_total",
                },
            },
        },
        options={
            **entry.options,
            CONF_HISTORICAL_COST_TRACKING_ENABLED: True,
            CONF_HISTORICAL_GRID_IMPORT_SENSOR: "sensor.grid_import_total",
            CONF_HISTORICAL_USAGE_SENSOR: "sensor.house_usage_total",
            CONF_HISTORICAL_PV_SENSOR: None,
        },
    )

    result = await _open_historical_settings(hass, entry)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "historical_costs_settings"
    assert _schema_suggested(result, CONF_HISTORICAL_PV_SENSOR) is None


async def test_historical_costs_optional_sensors_can_be_cleared(
    hass: HomeAssistant, mock_setup_entry: AsyncMock
) -> None:
    """Emptying an optional historical sensor must remove the stored value."""
    entry = await _create_basic_entry(hass)
    for entity_id in (
        "sensor.grid_import_total",
        "sensor.usage_total",
        "sensor.grid_export_total",
        "sensor.pv_total",
    ):
        _set_energy_sensor(hass, entity_id)
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_HISTORICAL_COST_TRACKING_ENABLED: True,
            CONF_HISTORICAL_GRID_IMPORT_SENSOR: "sensor.grid_import_total",
            CONF_HISTORICAL_GRID_EXPORT_SENSOR: "sensor.grid_export_total",
            CONF_HISTORICAL_USAGE_SENSOR: "sensor.usage_total",
            CONF_HISTORICAL_PV_SENSOR: "sensor.pv_total",
        },
    )

    result = await _open_historical_settings(hass, entry)
    assert _schema_suggested(result, CONF_HISTORICAL_PV_SENSOR) == "sensor.pv_total"
    assert _schema_default(result, CONF_HISTORICAL_PV_SENSOR) is None

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_HISTORICAL_GRID_IMPORT_SENSOR: "sensor.grid_import_total",
            CONF_HISTORICAL_USAGE_SENSOR: "sensor.usage_total",
            CONF_HISTORICAL_SIMULATE_SELF_CONSUMPTION: True,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated is not None
    assert updated.options[CONF_HISTORICAL_GRID_EXPORT_SENSOR] is None
    assert updated.options[CONF_HISTORICAL_PV_SENSOR] is None


async def test_historical_costs_rejects_non_energy_sensor(
    hass: HomeAssistant, mock_setup_entry: AsyncMock
) -> None:
    """A sensor that is not a cumulative energy meter cannot be saved."""
    entry = await _create_basic_entry(hass)
    _set_energy_sensor(hass, "sensor.grid_import_total")
    hass.states.async_set(
        "sensor.house_power", "1.0", {"device_class": "power", "unit_of_measurement": "W"}
    )
    result = await _open_historical_settings(hass, entry)

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_HISTORICAL_GRID_IMPORT_SENSOR: "sensor.grid_import_total",
            CONF_HISTORICAL_USAGE_SENSOR: "sensor.house_power",
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_HISTORICAL_USAGE_SENSOR: "invalid_energy_sensor"}


@pytest.mark.parametrize(
    ("planning", "emission", "warning_step"),
    [
        (True, True, None),
        (True, False, "planner_timers_warning_action_emission"),
        (False, False, "planner_timers_warning_both"),
    ],
)
async def test_options_planner_timers_save_with_matching_warning(
    hass: HomeAssistant,
    mock_setup_entry: AsyncMock,
    planning: bool,
    emission: bool,
    warning_step: str | None,
) -> None:
    """Disabled scheduler flags route through their targeted acknowledgement."""
    entry = await _create_basic_entry(hass)

    result = await _open_planner_timers_options(hass, entry)
    assert result["step_id"] == "planner_timers"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_PLANNING_ENABLED: planning, CONF_ACTION_EMISSION_ENABLED: emission},
    )
    if warning_step is not None:
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == warning_step
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_ACCEPT_MANUAL_SCHEDULING: True}
        )

    assert result["type"] is FlowResultType.MENU
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated is not None
    assert updated.options[CONF_PLANNING_ENABLED] is planning
    assert updated.options[CONF_ACTION_EMISSION_ENABLED] is emission


async def test_options_outlook_languages_are_advanced_and_optional(
    hass: HomeAssistant, mock_setup_entry: AsyncMock
) -> None:
    """Additional outlook languages should only appear in the options flow."""
    entry = await _create_basic_entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "outlook_languages"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "outlook_languages"
    assert _schema_default(result, CONF_OUTLOOK_LANGUAGES) == []
    field = _serialized_schema_field(result, CONF_OUTLOOK_LANGUAGES)
    assert field["selector"]["select"]["multiple"] is True
    assert field["selector"]["select"]["mode"] == "list"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_OUTLOOK_LANGUAGES: ["da", "en"]}
    )
    assert result["type"] is FlowResultType.MENU
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated is not None
    assert updated.options[CONF_OUTLOOK_LANGUAGES] == ["da", "en"]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "outlook_languages"}
    )
    assert _schema_default(result, CONF_OUTLOOK_LANGUAGES) == ["da", "en"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_OUTLOOK_LANGUAGES: []}
    )
    assert result["type"] is FlowResultType.MENU
    assert updated.options[CONF_OUTLOOK_LANGUAGES] == []


async def test_troubleshooting_recording_defaults_and_retention_bounds(
    hass: HomeAssistant, mock_setup_entry: AsyncMock
) -> None:
    """Recording is opt-in and retention remains within one to 730 days."""
    entry = await _create_basic_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "troubleshooting" in result["menu_options"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "troubleshooting"}
    )
    assert result["type"] is FlowResultType.FORM
    assert _schema_default(result, CONF_RECORD_PLANNER_REPRODUCTIONS) is False
    assert _schema_default(result, CONF_SHOW_EXPERIMENTAL_PLANNERS) is False
    assert _schema_default(result, CONF_PLANNER_REPRODUCTION_RETENTION_DAYS) == 14
    retention_field = _serialized_schema_field(
        result, CONF_PLANNER_REPRODUCTION_RETENTION_DAYS
    )
    assert retention_field["selector"]["number"]["min"] == 1
    assert retention_field["selector"]["number"]["max"] == 730

    fractional = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_RECORD_PLANNER_REPRODUCTIONS: True,
            CONF_SHOW_EXPERIMENTAL_PLANNERS: False,
            CONF_PLANNER_REPRODUCTION_RETENTION_DAYS: 1.5,
        },
    )
    assert fractional["type"] is FlowResultType.FORM
    assert fractional["errors"][CONF_PLANNER_REPRODUCTION_RETENTION_DAYS] == (
        "planner_reproduction_whole_days"
    )

    saved = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_RECORD_PLANNER_REPRODUCTIONS: True,
            CONF_SHOW_EXPERIMENTAL_PLANNERS: True,
            CONF_PLANNER_REPRODUCTION_RETENTION_DAYS: 730,
        },
    )
    assert saved["type"] is FlowResultType.MENU
    assert entry.options[CONF_RECORD_PLANNER_REPRODUCTIONS] is True
    assert entry.options[CONF_SHOW_EXPERIMENTAL_PLANNERS] is True
    assert entry.options[CONF_PLANNER_REPRODUCTION_RETENTION_DAYS] == 730


async def test_options_planner_core_uses_preset_owned_tuning(
    hass: HomeAssistant, mock_setup_entry: AsyncMock
) -> None:
    """Legacy profile and lookahead values stay out of the planner form."""
    entry = await _create_basic_entry(hass)
    stale_slots = 5
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "optimizer_profile": "aggressive",
            CONF_OPTIMIZER_LOOKAHEAD_SLOTS: stale_slots,
        },
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "planner_core"}
    )
    assert result["type"] is FlowResultType.FORM
    schema_fields = {getattr(field, "schema", None) for field in result["data_schema"].schema}
    assert "optimizer_lookahead_hours" not in schema_fields
    assert "optimizer_profile" not in schema_fields

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_PLANNER_PRESET: "self-consumption",
            CONF_SLOT_MINUTES: "60",
            CONF_HOURS_TO_PLAN: "24",
        },
    )
    assert result["type"] is FlowResultType.MENU
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated is not None
    assert updated.options[CONF_PLANNER_PRESET] == "self-consumption"
    assert updated.options[CONF_OPTIMIZER_LOOKAHEAD_SLOTS] == stale_slots
    assert updated.options["optimizer_profile"] == "aggressive"


def _long_cycle_heat(cycle_minutes: int) -> dict[str, Any]:
    """Comfort input whose minimum on and maximum off time are one long cycle."""
    return comfort_input(
        "Long cycle heat",
        24,
        **{
            CONF_TARGET_ON_HOURS_PER_WINDOW: 8,
            CONF_MIN_CONSECUTIVE_ON_MINUTES: cycle_minutes,
            CONF_MAX_CONSECUTIVE_OFF_MINUTES: cycle_minutes,
            CONF_ON_OFF_SOURCE: "binary_sensor.long_cycle_heat",
        },
    )


async def test_options_rejects_preset_that_shortens_existing_comfort_horizon(
    hass: HomeAssistant, mock_setup_entry: AsyncMock
) -> None:
    """Core edits should not save a resolution that breaks comfort validation."""
    entry = await _create_basic_entry(hass)
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            CONF_PLANNER_PRESET: "highs",
            CONF_OPTIMIZER_LOOKAHEAD_SLOTS: 22,
        },
    )
    result = await submit(
        hass,
        entry.entry_id,
        SUBENTRY_TYPE_COMFORT,
        _long_cycle_heat(12 * 60),
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "planner_core"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SLOT_MINUTES: "15",
            CONF_HOURS_TO_PLAN: "24",
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {
        CONF_PLANNER_PRESET: "optimizer_lookahead_too_short_for_comfort"
    }
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated is not None
    assert updated.options[CONF_OPTIMIZER_LOOKAHEAD_SLOTS] == 22


async def test_comfort_flow_rejects_minimum_equal_to_effective_lookahead(
    hass: HomeAssistant, mock_setup_entry: AsyncMock
) -> None:
    """Comfort edits should fail before they can break later planning."""
    entry = await _create_basic_entry(hass)
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_PLANNER_PRESET: "highs"}
    )
    result = await submit(
        hass,
        entry.entry_id,
        SUBENTRY_TYPE_COMFORT,
        _long_cycle_heat(22 * 60),
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"][CONF_MIN_CONSECUTIVE_ON_MINUTES] == (
        "comfort_duration_exceeds_lookahead"
    )


async def test_options_planner_timers_planning_disabled_requires_acknowledgement(
    hass: HomeAssistant, mock_setup_entry: AsyncMock
) -> None:
    """Test disabled scheduled planning routes through targeted acknowledgement."""
    entry = await _create_basic_entry(hass)

    result = await _open_planner_timers_options(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_PLANNING_ENABLED: False,
            CONF_ACTION_EMISSION_ENABLED: True,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "planner_timers_warning_planning"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_ACCEPT_MANUAL_SCHEDULING: False},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "planner_timers"
    assert _schema_default(result, CONF_PLANNING_ENABLED) is False
    assert _schema_default(result, CONF_ACTION_EMISSION_ENABLED) is True
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated is not None
    assert updated.options[CONF_PLANNING_ENABLED] is True

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_PLANNING_ENABLED: False,
            CONF_ACTION_EMISSION_ENABLED: True,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "planner_timers_warning_planning"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_ACCEPT_MANUAL_SCHEDULING: True},
    )
    assert result["type"] is FlowResultType.MENU
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated is not None
    assert updated.options[CONF_PLANNING_ENABLED] is False
    assert updated.options[CONF_ACTION_EMISSION_ENABLED] is True
