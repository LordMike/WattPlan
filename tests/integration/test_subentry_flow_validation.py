"""Validation behaviour of the battery, comfort and optional subentry flows."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.wattplan.const import (
    CONF_AVAILABILITY_SOURCE,
    CONF_DURATION_MINUTES,
    CONF_ENERGY_KWH,
    CONF_MIN_OPTION_GAP_MINUTES,
    CONF_OPTIONS_COUNT,
    CONF_ROLLING_WINDOW_HOURS,
    CONF_RUN_WITHIN_HOURS,
    CONF_CHARGE_EFFICIENCY,
    CONF_DISCHARGE_EFFICIENCY,
    CONF_MINIMUM_KWH,
    SUBENTRY_TYPE_BATTERY,
    SUBENTRY_TYPE_COMFORT,
    SUBENTRY_TYPE_OPTIONAL,
)

from .flow_helpers import (
    SECTION_BATTERY_ADVANCED,
    battery_input,
    comfort_input,
    finish_subentry_if_needed,
    optional_input,
    submit,
)
from .test_config_flow import _create_basic_entry

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def _suggested(schema, field: str) -> Any:
    marker = next(key for key in schema.schema if key.schema == field)
    return (marker.description or {}).get("suggested_value")


async def test_battery_form_error_keeps_entered_efficiencies(
    hass: HomeAssistant, mock_setup_entry: AsyncMock
) -> None:
    """COR-30: a rejected battery form must not reset the efficiencies."""
    entry = await _create_basic_entry(hass)
    result = await submit(
        hass,
        entry.entry_id,
        SUBENTRY_TYPE_BATTERY,
        battery_input(
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
    result = await submit(
        hass, entry.entry_id, SUBENTRY_TYPE_OPTIONAL, optional_input("Boiler 1")
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    result = await submit(
        hass, entry.entry_id, SUBENTRY_TYPE_OPTIONAL, optional_input("boiler 1")
    )
    assert result["errors"] == {"base": "name_not_unique"}

    result = await submit(
        hass, entry.entry_id, SUBENTRY_TYPE_OPTIONAL, optional_input("Boiler-1")
    )
    assert result["errors"] == {"base": "name_too_similar"}

    # Non-ASCII letters stay distinct instead of both collapsing to "item".
    for name in ("Ål", "Øl"):
        result = await submit(
            hass, entry.entry_id, SUBENTRY_TYPE_OPTIONAL, optional_input(name)
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
    result = await submit(
        hass, entry.entry_id, SUBENTRY_TYPE_COMFORT, comfort_input("Heat", 24)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    result = await submit(
        hass, entry.entry_id, SUBENTRY_TYPE_COMFORT, comfort_input("Water", 12)
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {
        CONF_ROLLING_WINDOW_HOURS: "comfort_rolling_window_mismatch"
    }
    assert result["description_placeholders"]["window_hours"] == "24"

    result = await submit(
        hass, entry.entry_id, SUBENTRY_TYPE_COMFORT, comfort_input("Water", 24)
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
        result["flow_id"], comfort_input("Heat", 12)
    )
    assert result["errors"] == {
        CONF_ROLLING_WINDOW_HOURS: "comfort_rolling_window_mismatch"
    }


async def test_battery_without_availability_source_is_saved_without_the_key(
    hass: HomeAssistant, mock_setup_entry: AsyncMock
) -> None:
    entry = await _create_basic_entry(hass)
    result = await submit(
        hass, entry.entry_id, SUBENTRY_TYPE_BATTERY, battery_input("Garage battery")
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated is not None
    battery = next(iter(updated.subentries.values()))
    assert CONF_AVAILABILITY_SOURCE not in battery.data


async def test_optional_load_form_errors(
    hass: HomeAssistant, mock_setup_entry: AsyncMock
) -> None:
    """Each optional-load validation error is reported on its own field."""
    entry = await _create_basic_entry(hass)
    result = await submit(
        hass,
        entry.entry_id,
        SUBENTRY_TYPE_OPTIONAL,
        optional_input(
            " ",
            **{
                CONF_DURATION_MINUTES: 120,
                CONF_RUN_WITHIN_HOURS: 1,
                CONF_ENERGY_KWH: 0,
                CONF_OPTIONS_COUNT: 2,
                CONF_MIN_OPTION_GAP_MINUTES: 15,
            },
        ),
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {
        CONF_NAME: "text_required",
        CONF_ENERGY_KWH: "optional_energy_must_be_positive",
        CONF_DURATION_MINUTES: "optional_duration_exceeds_window",
    }

    result = await submit(
        hass,
        entry.entry_id,
        SUBENTRY_TYPE_OPTIONAL,
        optional_input(
            "Dishwasher",
            **{
                CONF_RUN_WITHIN_HOURS: 3,
                CONF_OPTIONS_COUNT: 3,
                CONF_MIN_OPTION_GAP_MINUTES: 600,
            },
        ),
    )
    assert result["errors"] == {CONF_OPTIONS_COUNT: "optional_options_exceed_window"}


async def test_reconfigure_saves_changes_and_keeps_own_name(
    hass: HomeAssistant, mock_setup_entry: AsyncMock
) -> None:
    """Reconfiguring a load may keep its name and updates the stored data."""
    entry = await _create_basic_entry(hass)
    result = await submit(
        hass, entry.entry_id, SUBENTRY_TYPE_OPTIONAL, optional_input("Boiler 1")
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    original = next(iter(hass.config_entries.async_get_entry(entry.entry_id).subentries.values()))

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_OPTIONAL),
        context={"source": "reconfigure", "subentry_id": original.subentry_id},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], optional_input("Boiler 1", **{CONF_ENERGY_KWH: 2.5})
    )
    result = await finish_subentry_if_needed(hass, result)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"

    updated = hass.config_entries.async_get_entry(entry.entry_id)
    assert updated is not None
    assert len(updated.subentries) == 1
    saved = updated.subentries[original.subentry_id]
    assert saved.data[CONF_ENERGY_KWH] == 2.5
    assert saved.unique_id == original.unique_id
