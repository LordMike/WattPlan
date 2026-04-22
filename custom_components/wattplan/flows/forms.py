"""Shared subentry form helpers for WattPlan flows."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME

from .common import _subentry_name
from ..const import (
    CONF_CAPACITY_KWH,
    CONF_CHARGE_EFFICIENCY,
    CONF_DISCHARGE_EFFICIENCY,
    CONF_DURATION_MINUTES,
    CONF_ENERGY_KWH,
    CONF_EXPECTED_POWER_KW,
    CONF_MAX_CONSECUTIVE_OFF_MINUTES,
    CONF_MIN_CONSECUTIVE_OFF_MINUTES,
    CONF_MIN_CONSECUTIVE_ON_MINUTES,
    CONF_MIN_OPTION_GAP_MINUTES,
    CONF_MINIMUM_KWH,
    CONF_OPTIONS_COUNT,
    CONF_PREFER_PV_SURPLUS_CHARGING,
    CONF_ROLLING_WINDOW_HOURS,
    CONF_RUN_WITHIN_HOURS,
    CONF_TARGET_ON_HOURS_PER_WINDOW,
)
from .source_shared import (
    MAX_NAME_LENGTH,
    SECTION_BATTERY_ADVANCED,
    _optional_max_distinct_options,
    _validate_text_field,
)


def _subentry_name_in_use(entry: ConfigEntry, name: str) -> bool:
    """Return True if the name is already used by a subentry."""
    wanted = name.casefold()
    return any(
        _subentry_name(subentry).casefold() == wanted
        for subentry in entry.subentries.values()
    )


def _subentry_name_in_use_excluding(
    entry: ConfigEntry, name: str, exclude_subentry_id: str
) -> bool:
    """Return True if the name is used by another subentry."""
    wanted = name.casefold()
    return any(
        subentry.subentry_id != exclude_subentry_id
        and _subentry_name(subentry).casefold() == wanted
        for subentry in entry.subentries.values()
    )


def _validate_battery_data(data: dict[str, Any]) -> dict[str, str]:
    """Validate battery values for better UX."""
    errors: dict[str, str] = {}
    _validate_text_field(
        str(data.get(CONF_NAME, "")), CONF_NAME, errors, max_length=MAX_NAME_LENGTH
    )
    if float(data[CONF_MINIMUM_KWH]) > float(data[CONF_CAPACITY_KWH]):
        errors[CONF_MINIMUM_KWH] = "battery_minimum_exceeds_capacity"
    for field in (CONF_CHARGE_EFFICIENCY, CONF_DISCHARGE_EFFICIENCY):
        if not 0 < float(data[field]) <= 1:
            errors[field] = "battery_efficiency_invalid"
    return errors


def _normalize_battery_input(user_input: dict[str, Any]) -> dict[str, Any]:
    """Flatten battery advanced settings into subentry data."""
    data = dict(user_input)
    data.update(data.pop(SECTION_BATTERY_ADVANCED, {}))
    data.setdefault(CONF_CHARGE_EFFICIENCY, 0.9)
    data.setdefault(CONF_DISCHARGE_EFFICIENCY, 0.9)
    data.setdefault(CONF_PREFER_PV_SURPLUS_CHARGING, False)
    return data


def _battery_form_defaults(data: dict[str, Any]) -> dict[str, Any]:
    """Return battery defaults shaped for the form schema."""
    defaults = dict(data)
    defaults[SECTION_BATTERY_ADVANCED] = {
        CONF_CHARGE_EFFICIENCY: defaults.get(CONF_CHARGE_EFFICIENCY, 0.9),
        CONF_DISCHARGE_EFFICIENCY: defaults.get(CONF_DISCHARGE_EFFICIENCY, 0.9),
    }
    return defaults


def _validate_comfort_data(data: dict[str, Any]) -> dict[str, str]:
    """Validate comfort values for better UX."""
    errors: dict[str, str] = {}
    _validate_text_field(
        str(data.get(CONF_NAME, "")), CONF_NAME, errors, max_length=MAX_NAME_LENGTH
    )
    rolling_window_hours = float(data[CONF_ROLLING_WINDOW_HOURS])
    rolling_window_minutes = int(rolling_window_hours * 60)
    if float(data[CONF_TARGET_ON_HOURS_PER_WINDOW]) > rolling_window_hours:
        errors[CONF_TARGET_ON_HOURS_PER_WINDOW] = "comfort_target_on_hours_invalid"
    if int(data[CONF_MIN_CONSECUTIVE_ON_MINUTES]) > rolling_window_minutes:
        errors[CONF_MIN_CONSECUTIVE_ON_MINUTES] = "comfort_duration_exceeds_window"
    if int(data[CONF_MIN_CONSECUTIVE_OFF_MINUTES]) > rolling_window_minutes:
        errors[CONF_MIN_CONSECUTIVE_OFF_MINUTES] = "comfort_duration_exceeds_window"
    if int(data[CONF_MAX_CONSECUTIVE_OFF_MINUTES]) > rolling_window_minutes:
        errors[CONF_MAX_CONSECUTIVE_OFF_MINUTES] = "comfort_duration_exceeds_window"
    if float(data[CONF_EXPECTED_POWER_KW]) <= 0:
        errors[CONF_EXPECTED_POWER_KW] = "comfort_expected_power_invalid"
    return errors


def _validate_optional_data(data: dict[str, Any]) -> dict[str, str]:
    """Validate optional load values for better UX."""
    errors: dict[str, str] = {}
    _validate_text_field(
        str(data.get(CONF_NAME, "")), CONF_NAME, errors, max_length=MAX_NAME_LENGTH
    )
    energy_kwh = data.get(CONF_ENERGY_KWH)
    if energy_kwh is None:
        errors[CONF_ENERGY_KWH] = "energy_kwh_required"
    elif float(energy_kwh) <= 0:
        errors[CONF_ENERGY_KWH] = "optional_energy_must_be_positive"

    duration_minutes = int(data[CONF_DURATION_MINUTES])
    run_within_minutes = int(data[CONF_RUN_WITHIN_HOURS] * 60)
    min_gap_minutes = int(data[CONF_MIN_OPTION_GAP_MINUTES])
    options_count = int(data[CONF_OPTIONS_COUNT])

    if duration_minutes > run_within_minutes:
        errors[CONF_DURATION_MINUTES] = "optional_duration_exceeds_window"
        return errors

    max_options = _optional_max_distinct_options(
        run_within_minutes, duration_minutes, min_gap_minutes
    )
    if options_count > max_options:
        errors[CONF_OPTIONS_COUNT] = "optional_options_exceed_window"

    return errors
