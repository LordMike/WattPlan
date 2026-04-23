"""Button platform for WattPlan."""

from __future__ import annotations

from datetime import UTC, datetime

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .coordinator import CycleTrigger, WattPlanCoordinator
from .runtime import mark_runtime_updated
from .sensors.common import entry_device_info


class WattPlanButton(CoordinatorEntity[WattPlanCoordinator], ButtonEntity):
    """Base WattPlan button backed by coordinator."""

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: WattPlanCoordinator,
        *,
        object_id: str,
        friendly_name: str,
        unique_id: str,
    ) -> None:
        """Initialize coordinator-backed button."""
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_object_id = object_id
        self._attr_name = friendly_name
        self.internal_integration_suggested_object_id = object_id
        self._attr_unique_id = unique_id
        self._attr_device_info = entry_device_info(config_entry)


class RunOptimizeNowButton(WattPlanButton):
    """Button that triggers an immediate planning (optimize) cycle."""

    _attr_icon = "mdi:refresh"

    async def async_press(self) -> None:
        """Run the planning stage immediately."""
        await self.coordinator.async_plan(trigger=CycleTrigger.SERVICE)
        mark_runtime_updated(self._config_entry.runtime_data, when=datetime.now(tz=UTC))


class RefreshSensorsButton(WattPlanButton):
    """Button that refreshes HA sensor entities from the current plan."""

    _attr_icon = "mdi:lightning-bolt"

    async def async_press(self) -> None:
        """Run the emission stage immediately."""
        await self.coordinator.async_emit(trigger=CycleTrigger.SERVICE)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up WattPlan buttons for one config entry."""
    entry_slug = slugify(config_entry.title) or "entry"
    coordinator: WattPlanCoordinator = config_entry.runtime_data.coordinator

    async_add_entities(
        [
            RunOptimizeNowButton(
                config_entry,
                coordinator,
                object_id=f"{entry_slug}_run_optimize_now",
                friendly_name="Run Optimize Now",
                unique_id=f"{config_entry.entry_id}:entry:run_optimize_now",
            ),
            RefreshSensorsButton(
                config_entry,
                coordinator,
                object_id=f"{entry_slug}_refresh_sensors",
                friendly_name="Refresh Sensors",
                unique_id=f"{config_entry.entry_id}:entry:refresh_sensors",
            ),
        ]
    )
