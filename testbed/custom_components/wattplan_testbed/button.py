"""Button platform for WattPlan testbed."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import SUBENTRY_BATTERY
from .runtime import TestbedRuntime, subentry_name, subentry_slug


class BatteryPresetButton(ButtonEntity):
    """Button that applies one battery preset."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = False
    _attr_should_poll = False

    def __init__(
        self,
        runtime: TestbedRuntime,
        subentry: Any,
        *,
        key: str,
        label: str,
        action: Callable[[str], None],
    ) -> None:
        """Initialize button."""
        self._runtime = runtime
        self._subentry = subentry
        self._action = action
        object_id = f"{runtime.entry_slug}_{subentry_slug(subentry)}_{key}"
        self._attr_object_id = object_id
        self.internal_integration_suggested_object_id = object_id
        self._attr_unique_id = f"{runtime.entry.entry_id}:{subentry.subentry_id}:{key}"
        self._attr_name = f"{subentry_name(subentry)} {label}"
        self._attr_device_info = runtime.device_info(subentry=subentry)

    async def async_press(self) -> None:
        """Apply the preset."""
        self._action(self._subentry.subentry_id)


def _battery_buttons(runtime: TestbedRuntime, subentry: Any) -> list[BatteryPresetButton]:
    """Return preset buttons for one battery."""
    return [
        BatteryPresetButton(
            runtime,
            subentry,
            key="set_available",
            label="Set Available",
            action=runtime.set_battery_available,
        ),
        BatteryPresetButton(
            runtime,
            subentry,
            key="set_unavailable",
            label="Set Unavailable",
            action=runtime.set_battery_unavailable,
        ),
        *[
            BatteryPresetButton(
                runtime,
                subentry,
                key=f"soc_{pct}",
                label=f"Set {pct}%",
                action=lambda subentry_id, pct=pct: runtime.set_battery_soc(subentry_id, pct),
            )
            for pct in (10, 80, 100)
        ],
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up battery preset buttons."""
    runtime: TestbedRuntime = config_entry.runtime_data
    buttons: list[BatteryPresetButton] = []
    for subentry in config_entry.subentries.values():
        if subentry.subentry_type == SUBENTRY_BATTERY:
            buttons.extend(_battery_buttons(runtime, subentry))
    async_add_entities(buttons)
