"""Shared helpers for WattPlan sensor entities."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import DeviceInfo

from ..const import DOMAIN
from ..datetime_utils import parse_datetime_like

BATTERY_CHARGE_SOURCE_LABELS: dict[str, str] = {
    "n": "(N)one",
    "g": "(G)rid",
    "p": "(P)V",
    "gp": "(G)rid and (P)V",
}

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


def friendly_charge_source_label(charge_source: str) -> str:
    """Return a user-facing charge source label for compact planner codes."""
    return BATTERY_CHARGE_SOURCE_LABELS.get(charge_source, charge_source)
