"""Shared subentry form helpers for WattPlan flows."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME

from .common import _subentry_name
from ..const import (
    CONF_AVAILABILITY_SOURCE,
    CONF_CAPACITY_KWH,
    CONF_CHARGE_EFFICIENCY,
    CONF_DISCHARGE_EFFICIENCY,
    CONF_DURATION_MINUTES,
    CONF_ENERGY_KWH,
    CONF_EXPECTED_POWER_KW,
    CONF_HOURS_TO_PLAN,
    CONF_MAX_CONSECUTIVE_OFF_MINUTES,
    CONF_MIN_CONSECUTIVE_OFF_MINUTES,
    CONF_MIN_CONSECUTIVE_ON_MINUTES,
    CONF_MIN_OPTION_GAP_MINUTES,
    CONF_MINIMUM_KWH,
    CONF_OPTIONS_COUNT,
    CONF_OPTIMIZER_LOOKAHEAD_HOURS,
    CONF_OPTIMIZER_LOOKAHEAD_SLOTS,
    CONF_PREFER_PV_SURPLUS_CHARGING,
    CONF_ROLLING_WINDOW_HOURS,
    CONF_RUN_WITHIN_HOURS,
    CONF_SLOT_MINUTES,
    CONF_TARGET_ON_HOURS_PER_WINDOW,
    LEGACY_OPTIMIZER_LOOKAHEAD_SLOTS,
    SUBENTRY_TYPE_COMFORT,
)
from .source_shared import (
    MAX_NAME_LENGTH,
    SECTION_BATTERY_ADVANCED,
    _lookahead_slots_from_hours,
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
    if not data.get(CONF_AVAILABILITY_SOURCE):
        data.pop(CONF_AVAILABILITY_SOURCE, None)
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


def _comfort_duration_slots(minutes: int, slot_minutes: int) -> int:
    """Convert a comfort minimum duration to planner slots."""
    return max(1, int(round(minutes / slot_minutes)))


def _validate_comfort_data(
    data: dict[str, Any], *, entry: ConfigEntry | None = None
) -> dict[str, str]:
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
    if entry is not None:
        slot_minutes = int(entry.data[CONF_SLOT_MINUTES])
        plan_slots = int(entry.data[CONF_HOURS_TO_PLAN]) * 60 // slot_minutes
        lookahead_slots = int(
            entry.options.get(
                CONF_OPTIMIZER_LOOKAHEAD_SLOTS,
                LEGACY_OPTIMIZER_LOOKAHEAD_SLOTS,
            )
        )
        solve_horizon = min(plan_slots, lookahead_slots)
        for field in (
            CONF_MIN_CONSECUTIVE_ON_MINUTES,
            CONF_MIN_CONSECUTIVE_OFF_MINUTES,
        ):
            if _comfort_duration_slots(int(data[field]), slot_minutes) >= solve_horizon:
                errors[field] = "comfort_duration_exceeds_lookahead"
    return errors


def _validate_core_lookahead_for_comforts(
    entry: ConfigEntry, data: dict[str, Any]
) -> dict[str, str]:
    """Reject planner changes that make existing comfort loads invalid."""
    slot_minutes = int(data[CONF_SLOT_MINUTES])
    plan_slots = int(data[CONF_HOURS_TO_PLAN]) * 60 // slot_minutes
    lookahead_slots = _lookahead_slots_from_hours(
        float(data[CONF_OPTIMIZER_LOOKAHEAD_HOURS]), slot_minutes
    )
    solve_horizon = min(plan_slots, lookahead_slots)
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_COMFORT:
            continue
        if any(
            _comfort_duration_slots(int(subentry.data[field]), slot_minutes)
            >= solve_horizon
            for field in (
                CONF_MIN_CONSECUTIVE_ON_MINUTES,
                CONF_MIN_CONSECUTIVE_OFF_MINUTES,
            )
        ):
            return {
                CONF_OPTIMIZER_LOOKAHEAD_HOURS: (
                    "optimizer_lookahead_too_short_for_comfort"
                )
            }
    return {}


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
