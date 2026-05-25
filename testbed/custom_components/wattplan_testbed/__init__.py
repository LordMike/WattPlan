"""WattPlan live testbed integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PLATFORMS
from .config_flow import normalize_entry_data
from .runtime import TestbedRuntime


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old root slot/horizon fields to the update interval field."""
    normalized = normalize_entry_data(dict(entry.data))
    if normalized != dict(entry.data) or entry.title != normalized["name"]:
        hass.config_entries.async_update_entry(
            entry,
            title=str(normalized["name"]),
            data=normalized,
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one WattPlan testbed entry."""
    runtime = TestbedRuntime(hass, entry)
    entry.runtime_data = runtime
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    runtime.start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload one WattPlan testbed entry."""
    runtime: TestbedRuntime = entry.runtime_data
    runtime.stop()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok
