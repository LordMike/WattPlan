"""The WattPlan integration."""

import logging
from pathlib import Path
import shutil

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import CONF_OPTIMIZER_LOOKAHEAD_SLOTS, LEGACY_OPTIMIZER_LOOKAHEAD_SLOTS
from .coordinator import snapshot_storage_key
from .entry_setup import async_setup_entry, async_unload_entry, async_update_listener
from .historical_cost.store import history_storage_key
from .planner_history import reproduction_directory
from .runtime import BatteryTarget, WattPlanConfigEntry, WattPlanRuntimeData
from .target_persistence import targets_storage_key

_LOGGER = logging.getLogger(__name__)

CURRENT_VERSION = 1


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Persist the historical 22-slot lookahead for existing entries."""
    if entry.version > CURRENT_VERSION:
        # Created by a newer WattPlan; downgrading must not touch its data.
        _LOGGER.error(
            "Cannot migrate WattPlan entry %s: version %s is newer than supported",
            entry.entry_id,
            entry.version,
        )
        return False
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


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete everything WattPlan persisted for a removed config entry."""
    for key in (
        snapshot_storage_key(entry.entry_id),
        history_storage_key(entry.entry_id),
        targets_storage_key(entry.entry_id),
    ):
        # The version is irrelevant when removing a store.
        await Store(hass, 1, key, private=True).async_remove()
    await hass.async_add_executor_job(
        _remove_reproductions, reproduction_directory(hass, entry.entry_id)
    )


def _remove_reproductions(directory: Path) -> None:
    """Delete the recorded planner reproductions of one entry."""
    try:
        shutil.rmtree(directory)
    except FileNotFoundError:
        pass
    except OSError as err:
        _LOGGER.warning("Could not remove planner reproductions %s: %s", directory, err)


__all__ = [
    "BatteryTarget",
    "WattPlanConfigEntry",
    "WattPlanRuntimeData",
    "async_migrate_entry",
    "async_remove_entry",
    "async_setup_entry",
    "async_unload_entry",
    "async_update_listener",
]
