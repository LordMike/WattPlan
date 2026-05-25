"""Number controls for WattPlan testbed generators."""

from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    CONF_BASE_OFFSET,
    CONF_CLOUD_FACTOR,
    CONF_FACTOR,
    CONF_NOISE,
    CONF_PEAK_KWH,
    SUBENTRY_LOAD,
    SUBENTRY_PRICE,
    SUBENTRY_PV,
)
from .runtime import TestbedRuntime, subentry_name, subentry_slug


class GeneratorNumber(NumberEntity):
    """Runtime number control for one generator parameter."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = False
    _attr_mode = NumberMode.BOX
    _attr_should_poll = False

    def __init__(
        self,
        runtime: TestbedRuntime,
        subentry: Any,
        *,
        field: str,
        label: str,
        min_value: float,
        max_value: float,
        step: float,
    ) -> None:
        """Initialize number control."""
        self._runtime = runtime
        self._subentry = subentry
        self._field = field
        object_id = f"{runtime.entry_slug}_{subentry_slug(subentry)}_{field}"
        self._attr_object_id = object_id
        self.internal_integration_suggested_object_id = object_id
        self._attr_unique_id = f"{runtime.entry.entry_id}:{subentry.subentry_id}:{field}"
        self._attr_name = f"{subentry_name(subentry)} {label}"
        self._attr_native_min_value = min_value
        self._attr_native_max_value = max_value
        self._attr_native_step = step
        self._attr_device_info = runtime.device_info(subentry=subentry)

    @property
    def native_value(self) -> float:
        """Return current value."""
        return self._runtime.override_value(self._subentry, self._field)

    async def async_set_native_value(self, value: float) -> None:
        """Update generator override."""
        self._runtime.set_override(self._subentry.subentry_id, self._field, value)
        self.async_write_ha_state()


def _numbers_for_subentry(runtime: TestbedRuntime, subentry: Any) -> list[GeneratorNumber]:
    """Return number controls for one source subentry."""
    if subentry.subentry_type == SUBENTRY_PRICE:
        return [
            GeneratorNumber(runtime, subentry, field=CONF_BASE_OFFSET, label="Base Offset", min_value=-5, max_value=5, step=0.01),
            GeneratorNumber(runtime, subentry, field=CONF_FACTOR, label="Factor", min_value=-10, max_value=10, step=0.05),
            GeneratorNumber(runtime, subentry, field=CONF_NOISE, label="Noise", min_value=0, max_value=5, step=0.01),
        ]
    if subentry.subentry_type == SUBENTRY_PV:
        return [
            GeneratorNumber(runtime, subentry, field=CONF_PEAK_KWH, label="Peak kW", min_value=0, max_value=100, step=0.1),
            GeneratorNumber(runtime, subentry, field=CONF_CLOUD_FACTOR, label="Cloud Factor", min_value=0, max_value=1, step=0.05),
            GeneratorNumber(runtime, subentry, field=CONF_FACTOR, label="Factor", min_value=0, max_value=10, step=0.05),
        ]
    if subentry.subentry_type == SUBENTRY_LOAD:
        return [
            GeneratorNumber(runtime, subentry, field=CONF_FACTOR, label="Factor", min_value=0, max_value=10, step=0.05),
            GeneratorNumber(runtime, subentry, field=CONF_NOISE, label="Noise", min_value=0, max_value=5, step=0.01),
        ]
    return []


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up generator number controls."""
    runtime: TestbedRuntime = config_entry.runtime_data
    numbers: list[GeneratorNumber] = []
    for subentry in config_entry.subentries.values():
        numbers.extend(_numbers_for_subentry(runtime, subentry))
    async_add_entities(numbers)
