"""Shared builders for driving the WattPlan config, options and subentry flows."""

from __future__ import annotations

from typing import Any

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
    CONF_SOURCE_MODE,
    CONF_TARGET_ON_HOURS_PER_WINDOW,
    CONF_TEMPLATE,
    SOURCE_MODE_TEMPLATE,
)

SECTION_BATTERY_ADVANCED = "advanced"
CONF_ACCEPT_SOURCE_SUMMARY = "accept_source_summary"


async def walk_template_sources(
    hass: HomeAssistant, flow_id: str, template: str
) -> dict[str, Any]:
    """Configure import price and usage as templates; return the source_pv form."""
    result: dict[str, Any] = {}
    for _source in ("price", "usage"):
        for user_input in (
            {CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE},
            {CONF_TEMPLATE: template},
            {CONF_ACCEPT_SOURCE_SUMMARY: True},
        ):
            result = await hass.config_entries.flow.async_configure(
                flow_id, user_input
            )
    assert result["step_id"] == "source_pv"
    return result


async def finish_subentry_if_needed(
    hass: HomeAssistant, result: dict[str, Any]
) -> dict[str, Any]:
    """Advance through any final subentry confirmation form."""
    while result["type"] is FlowResultType.FORM and result["step_id"] in {
        "complete",
        "reconfigure_complete",
    }:
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {}
        )
    return result


def battery_input(name: str, **overrides: Any) -> dict[str, Any]:
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


def comfort_input(name: str, window_hours: float, **overrides: Any) -> dict[str, Any]:
    data = {
        CONF_NAME: name,
        CONF_ROLLING_WINDOW_HOURS: window_hours,
        CONF_TARGET_ON_HOURS_PER_WINDOW: 2,
        CONF_MIN_CONSECUTIVE_ON_MINUTES: 60,
        CONF_MIN_CONSECUTIVE_OFF_MINUTES: 60,
        CONF_MAX_CONSECUTIVE_OFF_MINUTES: 180,
        CONF_ON_OFF_SOURCE: "binary_sensor.heat_on",
        CONF_EXPECTED_POWER_KW: 1.5,
    }
    data.update(overrides)
    return data


def optional_input(name: str, **overrides: Any) -> dict[str, Any]:
    data = {
        CONF_NAME: name,
        CONF_DURATION_MINUTES: 60,
        CONF_RUN_WITHIN_HOURS: 12,
        CONF_ENERGY_KWH: 1.0,
        CONF_OPTIONS_COUNT: 1,
        CONF_MIN_OPTION_GAP_MINUTES: 0,
    }
    data.update(overrides)
    return data


async def submit(
    hass: HomeAssistant, entry_id: str, subentry_type: str, data: dict[str, Any]
) -> dict[str, Any]:
    """Open a new subentry flow, submit one form and confirm if it succeeded."""
    result = await hass.config_entries.subentries.async_init(
        (entry_id, subentry_type), context={"source": "user"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], data
    )
    return await finish_subentry_if_needed(hass, result)
