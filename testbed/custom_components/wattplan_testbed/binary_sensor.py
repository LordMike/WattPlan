"""Binary sensor platform for WattPlan testbed."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import SUBENTRY_BATTERY
from .runtime import TestbedRuntime, subentry_name, subentry_slug


class BatteryAvailabilitySensor(BinarySensorEntity):
    """Battery availability binary sensor."""

    _attr_has_entity_name = False
    _attr_should_poll = False

    def __init__(self, runtime: TestbedRuntime, subentry: Any) -> None:
        """Initialize availability sensor."""
        self._runtime = runtime
        self._subentry = subentry
        object_id = f"{runtime.entry_slug}_{subentry_slug(subentry)}_available"
        self._attr_object_id = object_id
        self.internal_integration_suggested_object_id = object_id
        self._attr_unique_id = f"{runtime.entry.entry_id}:{subentry.subentry_id}:available"
        self._attr_name = f"{subentry_name(subentry)} Available"
        self._attr_device_info = runtime.device_info(subentry=subentry)

    async def async_added_to_hass(self) -> None:
        """Register for runtime refreshes."""
        self._runtime.register_entity(self)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister from runtime refreshes."""
        self._runtime.unregister_entity(self)

    @property
    def available(self) -> bool:
        """Return whether availability itself is trusted."""
        return self._runtime.battery_availability_state(self._subentry.subentry_id) != "unavailable"

    @property
    def is_on(self) -> bool:
        """Return whether battery is available for planning."""
        return self._runtime.battery_availability_state(self._subentry.subentry_id) == "on"


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up availability sensors."""
    runtime: TestbedRuntime = config_entry.runtime_data
    async_add_entities(
        [
            BatteryAvailabilitySensor(runtime, subentry)
            for subentry in config_entry.subentries.values()
            if subentry.subentry_type == SUBENTRY_BATTERY
        ]
    )
