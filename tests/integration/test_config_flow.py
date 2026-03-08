"""Test the WattPlan config flow."""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

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
    CONF_SOURCES,
    CONF_TARGET_ON_HOURS_PER_WINDOW,
    CONF_TEMPLATE,
    DOMAIN,
    SOURCE_MODE_NOT_USED,
    SOURCE_MODE_TEMPLATE,
    SUBENTRY_TYPE_BATTERY,
    SUBENTRY_TYPE_COMFORT,
    SUBENTRY_TYPE_OPTIONAL,
)
import pytest

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

SECTION_BATTERY_ADVANCED = "advanced"
CONF_ACCEPT_SOURCE_SUMMARY = "accept_source_summary"


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
    template = _series_template(24)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_TEMPLATE: template},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_ACCEPT_SOURCE_SUMMARY: True},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_TEMPLATE: template},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_ACCEPT_SOURCE_SUMMARY: True},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry is not None
    return entry


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
    assert _default_source_mode(result) == "energy_provider"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Home"
    assert len(mock_setup_entry.mock_calls) == 1


async def test_multiple_setups_allowed(
    hass: HomeAssistant, mock_setup_entry: AsyncMock
) -> None:
    """Test that multiple config entries can be created."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Home 1",
            CONF_SLOT_MINUTES: "60",
            CONF_HOURS_TO_PLAN: "12",
        },
    )
    template = _series_template(12)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_TEMPLATE: template},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_ACCEPT_SOURCE_SUMMARY: True},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE},
    )
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

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(mock_setup_entry.mock_calls) == 1

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "requirements"


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
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_TEMPLATE: template},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_ACCEPT_SOURCE_SUMMARY: True},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_TEMPLATE: template},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_ACCEPT_SOURCE_SUMMARY: True},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_SOURCE_MODE: SOURCE_MODE_NOT_USED},
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY

    entry = hass.config_entries.async_entries(DOMAIN)[0]

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "planner_timers"}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_PLANNING_ENABLED: True,
            CONF_ACTION_EMISSION_ENABLED: False,
        },
    )
    assert result["type"] is FlowResultType.MENU

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

    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated is not None
    assert updated.options[CONF_ACTION_EMISSION_ENABLED] is False
    assert CONF_SOURCES in updated.data

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_BATTERY), context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Car battery",
            CONF_SOC_SOURCE: "sensor.car_soc",
            CONF_CAPACITY_KWH: 70,
            CONF_MINIMUM_KWH: 10,
            CONF_MAX_CHARGE_KW: 11,
            CONF_MAX_DISCHARGE_KW: 11,
            SECTION_BATTERY_ADVANCED: {
                CONF_CHARGE_EFFICIENCY: 0.9,
                CONF_DISCHARGE_EFFICIENCY: 0.9,
            },
            CONF_CAN_CHARGE_FROM_GRID: False,
            CONF_CAN_CHARGE_FROM_PV: True,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_COMFORT), context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "House heat",
            CONF_ROLLING_WINDOW_HOURS: 24,
            CONF_TARGET_ON_HOURS_PER_WINDOW: 8,
            CONF_MIN_CONSECUTIVE_ON_MINUTES: 60,
            CONF_MIN_CONSECUTIVE_OFF_MINUTES: 60,
            CONF_MAX_CONSECUTIVE_OFF_MINUTES: 180,
            CONF_ON_OFF_SOURCE: "binary_sensor.house_heat_on",
            CONF_EXPECTED_POWER_KW: 1.5,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_OPTIONAL), context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Dishwasher",
            CONF_DURATION_MINUTES: 120,
            CONF_RUN_WITHIN_HOURS: 24,
            CONF_ENERGY_KWH: 2.2,
            CONF_OPTIONS_COUNT: 3,
            CONF_MIN_OPTION_GAP_MINUTES: 0,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    await hass.async_block_till_done()
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated is not None
    assert len(updated.subentries) == 3
    assert any(
        subentry.subentry_type == SUBENTRY_TYPE_BATTERY
        and subentry.title == "Car battery (70 kWh, min 10 kWh)"
        for subentry in updated.subentries.values()
    )


async def test_subentry_validation_errors(
    hass: HomeAssistant, mock_setup_entry: AsyncMock
) -> None:
    """Test relational and required validation for subentries."""
    entry = await _create_basic_entry(hass)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_BATTERY), context={"source": "user"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Home battery",
            CONF_SOC_SOURCE: "sensor.home_soc",
            CONF_CAPACITY_KWH: 20,
            CONF_MINIMUM_KWH: 25,
            CONF_MAX_CHARGE_KW: 7,
            CONF_MAX_DISCHARGE_KW: 7,
            SECTION_BATTERY_ADVANCED: {
                CONF_CHARGE_EFFICIENCY: 0.9,
                CONF_DISCHARGE_EFFICIENCY: 0.9,
            },
            CONF_CAN_CHARGE_FROM_GRID: False,
            CONF_CAN_CHARGE_FROM_PV: True,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_MINIMUM_KWH: "battery_minimum_exceeds_capacity"}

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_OPTIONAL), context={"source": "user"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_NAME: " ",
            CONF_DURATION_MINUTES: 120,
            CONF_RUN_WITHIN_HOURS: 1,
            CONF_ENERGY_KWH: 0,
            CONF_OPTIONS_COUNT: 2,
            CONF_MIN_OPTION_GAP_MINUTES: 15,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {
        CONF_NAME: "text_required",
        CONF_ENERGY_KWH: "optional_energy_must_be_positive",
        CONF_DURATION_MINUTES: "optional_duration_exceeds_window",
    }

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Dishwasher",
            CONF_DURATION_MINUTES: 60,
            CONF_RUN_WITHIN_HOURS: 3,
            CONF_ENERGY_KWH: 2.2,
            CONF_OPTIONS_COUNT: 3,
            CONF_MIN_OPTION_GAP_MINUTES: 600,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_OPTIONS_COUNT: "optional_options_exceed_window"}
