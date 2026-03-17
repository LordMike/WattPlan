"""Base sensor entities for WattPlan."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..coordinator import CoordinatorSnapshot, WattPlanCoordinator
from .common import entry_device_info


class WattPlanCoordinatorSensor(CoordinatorEntity[WattPlanCoordinator], SensorEntity):
    """Base WattPlan sensor backed by coordinator snapshots."""

    _attr_should_poll = False
    _require_snapshot = True
    _require_usable_plan = False

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: WattPlanCoordinator,
        *,
        object_id: str,
        friendly_name: str,
        unique_id: str,
        device_class: SensorDeviceClass | None = None,
    ) -> None:
        """Initialize coordinator-backed sensor."""
        super().__init__(coordinator)
        self._attr_object_id = object_id
        self._attr_name = friendly_name
        self.internal_integration_suggested_object_id = object_id
        self._attr_unique_id = unique_id
        self._attr_device_info = entry_device_info(config_entry)
        if device_class is not None:
            self._attr_device_class = device_class

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        if not super().available:
            return False
        if self._require_usable_plan and not self.coordinator.has_usable_plan:
            return False
        if self._require_snapshot and self.coordinator.snapshot is None:
            return False
        return True

    @property
    def snapshot(self) -> CoordinatorSnapshot | None:
        """Return current immutable coordinator snapshot."""
        return self.coordinator.snapshot


class StaticValueSensor(WattPlanCoordinatorSensor):
    """Simple sensor with static native value."""

    _require_snapshot = False

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: WattPlanCoordinator,
        *,
        native_value: Any,
        native_unit_of_measurement: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize static sensor."""
        super().__init__(config_entry, coordinator, **kwargs)
        self._attr_native_value = native_value
        if native_unit_of_measurement is not None:
            self._attr_native_unit_of_measurement = native_unit_of_measurement
