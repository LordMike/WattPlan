"""Subentry action and target sensors for WattPlan."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy

from ..coordinator import WattPlanCoordinator
from ..target_runtime import get_active_battery_target
from .base import WattPlanCoordinatorSensor
from .common import as_datetime, entry_device_info

BATTERY_ACTION_STATES = [
    "preserve",
    "self_consume",
    "grid_charge",
]


class SubentryActionSensor(WattPlanCoordinatorSensor):
    """Base sensor for diagnostics keyed by subentry and action group."""

    _require_usable_plan = True

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: WattPlanCoordinator,
        *,
        subentry_id: str,
        group: str,
        **kwargs: Any,
    ) -> None:
        """Initialize subentry action sensor."""
        super().__init__(config_entry, coordinator, **kwargs)
        self._subentry_id = subentry_id
        self._group = group
        if group == "batteries":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = BATTERY_ACTION_STATES

    def _action_data(self) -> dict[str, Any]:
        """Return action data for this subentry from snapshot diagnostics."""
        if not self.snapshot:
            return {}
        diagnostics = self.snapshot.diagnostics or {}
        group_data = diagnostics.get(self._group, {})
        if not isinstance(group_data, dict):
            return {}
        subentry_data = group_data.get(self._subentry_id, {})
        return subentry_data if isinstance(subentry_data, dict) else {}


class ActionSensor(SubentryActionSensor):
    """Action sensor with next action timestamp attributes."""

    @property
    def native_value(self) -> str | None:
        """Return current action label."""
        return self._action_data().get("action")

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        """Return action metadata."""
        return None


class NextActionSensor(SubentryActionSensor):
    """Disabled-by-default sensor exposing the next planned action."""

    _attr_entity_registry_enabled_default = False

    @property
    def native_value(self) -> str | None:
        """Return the next action label."""
        next_action = self._action_data().get("next_action")
        return str(next_action) if isinstance(next_action, str) else None

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        """Return next-action metadata."""
        data = self._action_data()
        attrs: dict[str, str] = {}
        timestamp = as_datetime(data.get("next_action_timestamp"))
        if timestamp is not None:
            attrs["timestamp"] = timestamp.isoformat()
        return attrs or None


class BatteryTargetSocSensor(SensorEntity):
    """Sensor for a battery target set by the user."""

    _attr_should_poll = False
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        config_entry: ConfigEntry,
        runtime_data: Any,
        subentry_id: str,
        *,
        object_id: str,
        friendly_name: str,
        unique_id: str,
    ) -> None:
        """Initialize battery target sensor."""
        self._attr_object_id = object_id
        self._attr_name = friendly_name
        self.internal_integration_suggested_object_id = object_id
        self._attr_unique_id = unique_id
        self._attr_device_info = entry_device_info(config_entry)
        self._runtime_data = runtime_data
        self._subentry_id = subentry_id

    async def async_added_to_hass(self) -> None:
        """Register updates so service calls can push state immediately."""
        await super().async_added_to_hass()
        listeners = self._runtime_data.battery_target_update_listeners.setdefault(
            self._subentry_id, set()
        )
        listeners.add(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister update callback."""
        listeners = self._runtime_data.battery_target_update_listeners.get(
            self._subentry_id
        )
        if listeners is not None:
            listeners.discard(self.async_write_ha_state)
            if not listeners:
                self._runtime_data.battery_target_update_listeners.pop(
                    self._subentry_id, None
                )
        await super().async_will_remove_from_hass()

    @property
    def native_value(self) -> float | None:
        """Return target SoC, or unknown when no user intent is set."""
        if target := get_active_battery_target(self._runtime_data, self._subentry_id):
            return target.soc_kwh
        return None

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        """Return timing metadata for the current user intent."""
        if target := get_active_battery_target(self._runtime_data, self._subentry_id):
            return {"by": target.reach_at.isoformat()}
        return {"by": "not_set"}
