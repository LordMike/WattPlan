"""Validation behaviour of the battery, comfort and optional subentry flows."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.wattplan.const import (
    CONF_CAN_CHARGE_FROM_GRID,
    CONF_CAN_CHARGE_FROM_PV,
    CONF_CAPACITY_KWH,
    CONF_CHARGE_EFFICIENCY,
    CONF_DISCHARGE_EFFICIENCY,
    CONF_DURATION_MINUTES,
    CONF_ENERGY_KWH,
    CONF_EXPECTED_POWER_KW,
    CONF_MAX_CHARGE_KW,
    CONF_MAX_CONSECUTIVE_OFF_MINUTES,
    CONF_MAX_DISCHARGE_KW,
    CONF_MIN_CONSECUTIVE_OFF_MINUTES,
    CONF_MIN_CONSECUTIVE_ON_MINUTES,
    CONF_MIN_OPTION_GAP_MINUTES,
    CONF_MINIMUM_KWH,
    CONF_ON_OFF_SOURCE,
    CONF_OPTIONS_COUNT,
    CONF_ROLLING_WINDOW_HOURS,
    CONF_RUN_WITHIN_HOURS,
    CONF_SOC_SOURCE,
    CONF_TARGET_ON_HOURS_PER_WINDOW,
    SUBENTRY_TYPE_BATTERY,
    SUBENTRY_TYPE_COMFORT,
    SUBENTRY_TYPE_OPTIONAL,
)

from .test_config_flow import (
    SECTION_BATTERY_ADVANCED,
    _create_basic_entry,
    _finish_subentry_if_needed,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def _battery_input(name: str, **overrides: Any) -> dict[str, Any]:
    data = {
        CONF_NAME: name,
        CONF_SOC_SOURCE: "sensor.soc",
        CONF_CAPACITY_KWH: 20,
        CONF_MINIMUM_KWH: 5,
        CONF_MAX_CHARGE_KW: 7,
        CONF_MAX_DISCHARGE_KW: 7,
        SECTION_BATTERY_ADVANCED: {
            CONF_CHARGE_EFFICIENCY: 0.9,
            CONF_DISCHARGE_EFFICIENCY: 0.9,
        },
        CONF_CAN_CHARGE_FROM_GRID: False,
        CONF_CAN_CHARGE_FROM_PV: True,
    }
    data.update(overrides)
    return data


def _comfort_input(name: str, window_hours: float) -> dict[str, Any]:
    return {
        CONF_NAME: name,
        CONF_ROLLING_WINDOW_HOURS: window_hours,
        CONF_TARGET_ON_HOURS_PER_WINDOW: 2,
        CONF_MIN_CONSECUTIVE_ON_MINUTES: 60,
        CONF_MIN_CONSECUTIVE_OFF_MINUTES: 60,
        CONF_MAX_CONSECUTIVE_OFF_MINUTES: 180,
        CONF_ON_OFF_SOURCE: "binary_sensor.heat_on",
        CONF_EXPECTED_POWER_KW: 1.5,
    }


def _optional_input(name: str) -> dict[str, Any]:
    return {
        CONF_NAME: name,
        CONF_DURATION_MINUTES: 60,
        CONF_RUN_WITHIN_HOURS: 12,
        CONF_ENERGY_KWH: 1.0,
        CONF_OPTIONS_COUNT: 1,
        CONF_MIN_OPTION_GAP_MINUTES: 0,
    }


async def _submit(
    hass: HomeAssistant, entry_id: str, subentry_type: str, data: dict[str, Any]
) -> dict[str, Any]:
    result = await hass.config_entries.subentries.async_init(
        (entry_id, subentry_type), context={"source": "user"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], data
    )
    return await _finish_subentry_if_needed(hass, result)


def _suggested(schema, field: str) -> Any:
    marker = next(key for key in schema.schema if key.schema == field)
    return (marker.description or {}).get("suggested_value")


async def test_battery_form_error_keeps_entered_efficiencies(
    hass: HomeAssistant, mock_setup_entry: AsyncMock
) -> None:
    """COR-30: a rejected battery form must not reset the efficiencies."""
    entry = await _create_basic_entry(hass)
    result = await _submit(
        hass,
        entry.entry_id,
        SUBENTRY_TYPE_BATTERY,
        _battery_input(
            "Home battery",
            **{
                CONF_MINIMUM_KWH: 25,
                SECTION_BATTERY_ADVANCED: {
                    CONF_CHARGE_EFFICIENCY: 0.8,
                    CONF_DISCHARGE_EFFICIENCY: 0.7,
                },
            },
        ),
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_MINIMUM_KWH: "battery_minimum_exceeds_capacity"}
    advanced = next(
        value
        for key, value in result["data_schema"].schema.items()
        if key.schema == SECTION_BATTERY_ADVANCED
    )
    assert _suggested(advanced.schema, CONF_CHARGE_EFFICIENCY) == 0.8
    assert _suggested(advanced.schema, CONF_DISCHARGE_EFFICIENCY) == 0.7


async def test_names_that_collapse_to_the_same_unique_id_are_rejected(
    hass: HomeAssistant, mock_setup_entry: AsyncMock
) -> None:
    """COR-31: the name check follows the normalisation used for unique_id."""
    entry = await _create_basic_entry(hass)
    result = await _submit(
        hass, entry.entry_id, SUBENTRY_TYPE_OPTIONAL, _optional_input("Boiler 1")
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    result = await _submit(
        hass, entry.entry_id, SUBENTRY_TYPE_OPTIONAL, _optional_input("boiler 1")
    )
    assert result["errors"] == {"base": "name_not_unique"}

    result = await _submit(
        hass, entry.entry_id, SUBENTRY_TYPE_OPTIONAL, _optional_input("Boiler-1")
    )
    assert result["errors"] == {"base": "name_too_similar"}

    # Non-ASCII letters stay distinct instead of both collapsing to "item".
    for name in ("Ål", "Øl"):
        result = await _submit(
            hass, entry.entry_id, SUBENTRY_TYPE_OPTIONAL, _optional_input(name)
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY

    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated is not None
    unique_ids = [subentry.unique_id for subentry in updated.subentries.values()]
    assert len(unique_ids) == len(set(unique_ids)) == 3


async def test_comfort_loads_must_share_one_rolling_window(
    hass: HomeAssistant, mock_setup_entry: AsyncMock
) -> None:
    """COR-26: a different rolling window would make every plan fail."""
    entry = await _create_basic_entry(hass)
    result = await _submit(
        hass, entry.entry_id, SUBENTRY_TYPE_COMFORT, _comfort_input("Heat", 24)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    result = await _submit(
        hass, entry.entry_id, SUBENTRY_TYPE_COMFORT, _comfort_input("Water", 12)
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {
        CONF_ROLLING_WINDOW_HOURS: "comfort_rolling_window_mismatch"
    }

    result = await _submit(
        hass, entry.entry_id, SUBENTRY_TYPE_COMFORT, _comfort_input("Water", 24)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    # Reconfiguring the only differing load is judged against the others, not itself.
    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated is not None
    heat = next(s for s in updated.subentries.values() if s.data[CONF_NAME] == "Heat")
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_COMFORT),
        context={"source": "reconfigure", "subentry_id": heat.subentry_id},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], _comfort_input("Heat", 12)
    )
    assert result["errors"] == {
        CONF_ROLLING_WINDOW_HOURS: "comfort_rolling_window_mismatch"
    }
