"""The WattPlan integration."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_OPTIMIZER_LOOKAHEAD_SLOTS, LEGACY_OPTIMIZER_LOOKAHEAD_SLOTS
from .entry_setup import async_setup_entry, async_unload_entry, async_update_listener
from .runtime import BatteryTarget, WattPlanConfigEntry, WattPlanRuntimeData


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Persist the historical 22-slot lookahead for existing entries."""
    if entry.version == 1 and entry.minor_version < 2:
        options = dict(entry.options)
        options.setdefault(
            CONF_OPTIMIZER_LOOKAHEAD_SLOTS,
            LEGACY_OPTIMIZER_LOOKAHEAD_SLOTS,
        )
        hass.config_entries.async_update_entry(
            entry,
            options=options,
            minor_version=2,
        )
    return True

__all__ = [
    "BatteryTarget",
    "WattPlanConfigEntry",
    "WattPlanRuntimeData",
    "async_migrate_entry",
    "async_setup_entry",
    "async_unload_entry",
    "async_update_listener",
]
