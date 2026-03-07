"""Binary sensor platform for WattPlan."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import (
    CONF_SOURCE_MODE,
    CONF_SOURCE_PV,
    CONF_SOURCES,
    DOMAIN,
    SOURCE_MODE_NOT_USED,
)
from .coordinator import StageErrorKind, WattPlanCoordinator


def _entry_slug(config_entry: ConfigEntry) -> str:
    """Return slug for config entry naming."""
    return slugify(config_entry.title) or "entry"


def _entry_device_info(config_entry: ConfigEntry) -> DeviceInfo:
    """Return shared device info for all entry entities."""
    return DeviceInfo(
        identifiers={(DOMAIN, config_entry.entry_id)},
        name=f"WattPlan {config_entry.title}",
        manufacturer="WattPlan",
        model="Planner",
    )


class WattPlanBinarySensor(
    CoordinatorEntity[WattPlanCoordinator], BinarySensorEntity
):
    """Base WattPlan binary sensor with one shared device."""

    _attr_should_poll = False

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: WattPlanCoordinator,
        *,
        object_id: str,
        unique_id: str,
        enabled_default: bool = True,
    ) -> None:
        """Initialize binary sensor."""
        super().__init__(coordinator)
        self._attr_object_id = object_id
        self._attr_name = object_id
        self._attr_unique_id = unique_id
        self._attr_device_info = _entry_device_info(config_entry)
        self._attr_entity_registry_enabled_default = enabled_default


class ErrorBinarySensor(WattPlanBinarySensor):
    """Error binary sensor with coordinator diagnostics."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: WattPlanCoordinator,
        *,
        scope: str,
        enabled_default: bool,
        object_id: str,
        unique_id: str,
    ) -> None:
        """Initialize error sensor."""
        super().__init__(
            config_entry,
            coordinator,
            object_id=object_id,
            unique_id=unique_id,
            enabled_default=enabled_default,
        )
        self._scope = scope

    @property
    def is_on(self) -> bool:
        """Return if this error scope is active."""
        if self._scope == "setup":
            return self.coordinator.has_error

        attrs = self.coordinator.error_attributes()
        plan_kind = attrs.get("plan_error_kind")
        plan_source = attrs.get("plan_error_source")

        source_error_kinds = {
            StageErrorKind.SOURCE_FETCH,
            StageErrorKind.SOURCE_PARSE,
            StageErrorKind.SOURCE_VALIDATION,
        }
        optimize_error_kinds = {
            StageErrorKind.PLANNER_INPUT,
            StageErrorKind.PLANNER_EXECUTION,
            StageErrorKind.INTERNAL,
            StageErrorKind.LOCKED,
        }

        if self._scope in {"source_price", "source_usage", "source_pv"}:
            expected_source = self._scope.removeprefix("source_")
            return plan_kind in source_error_kinds and plan_source == expected_source

        if self._scope == "optimize":
            return plan_kind in optimize_error_kinds

        return False

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return diagnostic attributes for this scope."""
        attrs = self.coordinator.error_attributes()
        attrs["scope"] = self._scope
        return attrs


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up WattPlan binary sensors for one config entry."""
    entry_slug = _entry_slug(config_entry)
    coordinator = config_entry.runtime_data.coordinator

    entities: list[BinarySensorEntity] = [
        ErrorBinarySensor(
            config_entry,
            coordinator,
            scope="setup",
            enabled_default=True,
            object_id=f"{entry_slug}_has_error",
            unique_id=f"{config_entry.entry_id}:entry:has_error",
        ),
        ErrorBinarySensor(
            config_entry,
            coordinator,
            scope="source_price",
            enabled_default=False,
            object_id=f"{entry_slug}_source_price_error",
            unique_id=f"{config_entry.entry_id}:entry:source_price_error",
        ),
        ErrorBinarySensor(
            config_entry,
            coordinator,
            scope="source_usage",
            enabled_default=False,
            object_id=f"{entry_slug}_source_usage_error",
            unique_id=f"{config_entry.entry_id}:entry:source_usage_error",
        ),
        ErrorBinarySensor(
            config_entry,
            coordinator,
            scope="optimize",
            enabled_default=False,
            object_id=f"{entry_slug}_optimize_error",
            unique_id=f"{config_entry.entry_id}:entry:optimize_error",
        ),
    ]
    pv_source = config_entry.data.get(CONF_SOURCES, {}).get(CONF_SOURCE_PV, {})
    if pv_source.get(CONF_SOURCE_MODE) != SOURCE_MODE_NOT_USED:
        entities.append(
            ErrorBinarySensor(
                config_entry,
                coordinator,
                scope="source_pv",
                enabled_default=False,
                object_id=f"{entry_slug}_source_pv_error",
                unique_id=f"{config_entry.entry_id}:entry:source_pv_error",
            )
        )

    async_add_entities(entities)
