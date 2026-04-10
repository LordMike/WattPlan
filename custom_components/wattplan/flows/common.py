"""Common flow helpers shared across WattPlan flow modules."""

from __future__ import annotations

import re
from typing import Any

from homeassistant.const import CONF_NAME

from ..const import (
    CONF_CAPACITY_KWH,
    CONF_DURATION_MINUTES,
    CONF_HOURS_TO_PLAN,
    CONF_MAX_CONSECUTIVE_OFF_MINUTES,
    CONF_MINIMUM_KWH,
    CONF_ROLLING_WINDOW_HOURS,
    CONF_RUN_WITHIN_HOURS,
    CONF_SLOT_MINUTES,
    CONF_TARGET_ON_HOURS_PER_WINDOW,
    SUBENTRY_TYPE_BATTERY,
    SUBENTRY_TYPE_COMFORT,
    SUBENTRY_TYPE_OPTIONAL,
)


def _normalize_name(value: str) -> str:
    """Create a stable id from a name."""
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_") or "item"


def _format_number(value: float) -> str:
    """Format a float-like value for compact display."""
    return f"{float(value):g}"


def _subentry_display_title(subentry_type: str, data: dict[str, Any]) -> str:
    """Build a concise display title for a subentry."""
    name = data[CONF_NAME]
    if subentry_type == SUBENTRY_TYPE_BATTERY:
        return (
            f"{name} ("
            f"{_format_number(data[CONF_CAPACITY_KWH])} kWh, "
            f"min {_format_number(data[CONF_MINIMUM_KWH])} kWh)"
        )
    if subentry_type == SUBENTRY_TYPE_COMFORT:
        return (
            f"{name} ("
            f"{_format_number(data[CONF_TARGET_ON_HOURS_PER_WINDOW])}h/"
            f"{_format_number(data[CONF_ROLLING_WINDOW_HOURS])}h, "
            f"max off {_format_number(data[CONF_MAX_CONSECUTIVE_OFF_MINUTES])} min)"
        )
    if subentry_type == SUBENTRY_TYPE_OPTIONAL:
        return (
            f"{name} ("
            f"{_format_number(data[CONF_DURATION_MINUTES])} min / "
            f"{_format_number(data[CONF_RUN_WITHIN_HOURS])}h)"
        )
    return name


def _subentry_name(subentry: Any) -> str:
    """Return the semantic name of a subentry."""
    return str(subentry.data.get(CONF_NAME, subentry.title))


def _expected_slots(config: dict[str, Any]) -> int:
    """Calculate expected slots for the configured horizon."""
    return int(config[CONF_HOURS_TO_PLAN] * 60 / config[CONF_SLOT_MINUTES])
