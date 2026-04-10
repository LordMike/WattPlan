"""Config entry lifecycle for WattPlan."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import partial
import logging
from typing import Any

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_ACTION_EMISSION_ENABLED, CONF_PLANNING_ENABLED, CONF_SLOT_MINUTES, DOMAIN
from .coordinator import CycleTrigger, WattPlanCoordinator
from .runtime import WattPlanConfigEntry, WattPlanRuntimeData, mark_runtime_updated
from .services import SERVICE_SPECS

PLATFORMS: list[Platform] = [Platform.SENSOR]

DATA_ENTRY_COUNT = "entry_count"
DATA_SERVICE_REGISTERED = "service_registered"

_LOGGER = logging.getLogger(__name__)


async def async_try_initial_plan(entry: WattPlanConfigEntry) -> None:
    """Run one immediate planning cycle after setup or reload."""
    try:
        await entry.runtime_data.coordinator.async_plan(trigger=CycleTrigger.SERVICE)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning(
            "Initial planner run failed after setup/reload (entry_id=%s): %s",
            entry.entry_id,
            err,
        )
    finally:
        mark_runtime_updated(entry.runtime_data, when=datetime.now(tz=UTC))


async def async_setup_entry(hass: HomeAssistant, entry: WattPlanConfigEntry) -> bool:
    """Set up WattPlan from a config entry."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if not domain_data.get(DATA_SERVICE_REGISTERED, False):
        for service, handler, schema, supports_response in SERVICE_SPECS:
            register_kwargs: dict[str, Any] = {"schema": schema}
            if supports_response is not None:
                register_kwargs["supports_response"] = supports_response
            hass.services.async_register(
                DOMAIN,
                service,
                partial(handler, hass),
                **register_kwargs,
            )
        domain_data[DATA_SERVICE_REGISTERED] = True
    domain_data[DATA_ENTRY_COUNT] = int(domain_data.get(DATA_ENTRY_COUNT, 0)) + 1

    coordinator = WattPlanCoordinator(
        hass,
        entry_id=entry.entry_id,
        update_interval=timedelta(minutes=int(entry.data[CONF_SLOT_MINUTES])),
        planning_enabled=bool(entry.options.get(CONF_PLANNING_ENABLED, True)),
        action_emission_enabled=bool(entry.options.get(CONF_ACTION_EMISSION_ENABLED, True)),
    )
    entry.runtime_data = WattPlanRuntimeData(
        coordinator=coordinator,
        last_run_at=datetime.now(tz=UTC),
    )
    await coordinator.async_restore_snapshot()
    entry.async_on_unload(entry.add_update_listener(async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await async_try_initial_plan(entry)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: WattPlanConfigEntry) -> bool:
    """Unload a config entry."""
    await entry.runtime_data.coordinator.async_shutdown()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    domain_data = hass.data.setdefault(DOMAIN, {})
    domain_data[DATA_ENTRY_COUNT] = max(int(domain_data.get(DATA_ENTRY_COUNT, 1)) - 1, 0)
    if (
        int(domain_data[DATA_ENTRY_COUNT]) == 0
        and domain_data.get(DATA_SERVICE_REGISTERED, False)
    ):
        for service, _handler, _schema, _supports_response in SERVICE_SPECS:
            hass.services.async_remove(DOMAIN, service)
        domain_data[DATA_SERVICE_REGISTERED] = False
    return True


async def async_update_listener(hass: HomeAssistant, entry: WattPlanConfigEntry) -> None:
    """Reload entry when config, options, or subentries change."""
    await hass.config_entries.async_reload(entry.entry_id)
