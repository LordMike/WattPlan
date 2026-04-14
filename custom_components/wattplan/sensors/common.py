"""Shared helpers for WattPlan sensor entities."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import DeviceInfo

from ..const import DOMAIN
from ..datetime_utils import parse_datetime_like

MAX_EXPOSED_PROJECTED_SAVINGS_PCT = 200.0
TIMESTAMP_DEVICE_CLASS = SensorDeviceClass.TIMESTAMP


def entry_device_info(config_entry: ConfigEntry) -> DeviceInfo:
    """Return shared device info for all entry entities."""
    return DeviceInfo(
        identifiers={(DOMAIN, config_entry.entry_id)},
        name=f"WattPlan {config_entry.title}",
        manufacturer="WattPlan",
        model="Planner",
    )


def as_datetime(value: Any) -> datetime | None:
    """Convert a dynamic value to datetime when possible."""
    return parse_datetime_like(value)
