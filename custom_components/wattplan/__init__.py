"""The WattPlan integration."""

import logging
from pathlib import Path
import shutil

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    CONF_OPTIMIZER_LOOKAHEAD_SLOTS,
    CONF_PLANNER_PRESET,
    LEGACY_OPTIMIZER_LOOKAHEAD_SLOTS,
)
from .config_identity import config_fingerprint
from .coordinator import STORAGE_VERSION, snapshot_storage_key
from .entry_setup import async_setup_entry, async_unload_entry, async_update_listener
from .historical_cost.store import history_storage_key
from .optimizer.planners.presets import LEGACY_DEFAULT_PRESET
from .planner_history import reproduction_directory
from .runtime import BatteryTarget, WattPlanConfigEntry, WattPlanRuntimeData
from .target_persistence import targets_storage_key

_LOGGER = logging.getLogger(__name__)

CURRENT_VERSION = 1


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Preserve historical tuning and planner selection for existing entries."""
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
    if entry.version == 1 and entry.minor_version < 3:
        options = dict(entry.options)
        options.setdefault(CONF_PLANNER_PRESET, LEGACY_DEFAULT_PRESET)
        if CONF_PLANNER_PRESET not in entry.options:
            store = Store(hass, STORAGE_VERSION, snapshot_storage_key(entry.entry_id), private=True)
            cached = await store.async_load()
            if (
                isinstance(cached, dict)
                and cached.get("planner_preset") == LEGACY_DEFAULT_PRESET
                and cached.get("config_fingerprint") == config_fingerprint(entry)
            ):
                # Only the implicit selection becomes explicit; the algorithm
                # and physical inputs are unchanged. Keep matching cached plans.
                await store.async_save({
                    **cached,
                    "config_fingerprint": config_fingerprint(entry, options=options),
                })
        hass.config_entries.async_update_entry(
            entry,
            options=options,
            minor_version=3,
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
