"""Config entry lifecycle for WattPlan."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.loader import async_get_integration

from .const import (
    CONF_ACTION_EMISSION_ENABLED,
    CONF_HISTORICAL_COST_TRACKING_ENABLED,
    CONF_PLANNER_REPRODUCTION_RETENTION_DAYS,
    CONF_PLANNER_PRESET,
    CONF_OUTLOOK_LANGUAGES,
    CONF_PLANNING_ENABLED,
    CONF_RECORD_PLANNER_REPRODUCTIONS,
    CONF_SLOT_MINUTES,
    DOMAIN,
    SUBENTRY_TYPE_BATTERY,
    DEFAULT_PLANNER_REPRODUCTION_RETENTION_DAYS,
    MAX_PLANNER_REPRODUCTION_RETENTION_DAYS,
)
from .config_identity import config_fingerprint
from .coordinator import CycleTrigger, PlanSupersededError, WattPlanCoordinator
from .historical_cost.tracker import HistoricalCostTracker
from .optimizer.planners.presets import DEFAULT_PRESET, get_preset
from .outlook_languages import resolve_outlook_languages
from .plan_outlook_renderer import preload_plan_outlook_catalogs
from .runtime import WattPlanConfigEntry, WattPlanRuntimeData, mark_runtime_updated
from .services import SERVICE_SPECS
from .target_persistence import BatteryTargetStore

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BUTTON]

DATA_ENTRY_COUNT = "entry_count"
DATA_SERVICE_REGISTERED = "service_registered"

_LOGGER = logging.getLogger(__name__)


async def async_try_initial_plan(entry: WattPlanConfigEntry) -> None:
    """Run one immediate planning cycle after setup or reload."""
    try:
        await entry.runtime_data.coordinator.async_plan(trigger=CycleTrigger.SERVICE)
    except PlanSupersededError as err:
        _LOGGER.info(
            "Initial planner run superseded after setup/reload (entry_id=%s): %s",
            entry.entry_id,
            err,
        )
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning(
            "Initial planner run failed after setup/reload (entry_id=%s): %s",
            entry.entry_id,
            err,
        )
    finally:
        mark_runtime_updated(entry.runtime_data, when=datetime.now(tz=UTC))


async def async_initial_plan_then_schedule(entry: WattPlanConfigEntry) -> None:
    """Start slot-boundary runs only after the initial plan settles.

    Otherwise a boundary shortly after setup supersedes the initial run.
    """
    try:
        await async_try_initial_plan(entry)
    finally:
        entry.runtime_data.coordinator.async_start_scheduler()


async def async_setup_entry(hass: HomeAssistant, entry: WattPlanConfigEntry) -> bool:
    """Set up WattPlan from a config entry."""
    configured_preset = entry.options.get(CONF_PLANNER_PRESET, DEFAULT_PRESET)
    try:
        get_preset(configured_preset)
    except ValueError:
        _LOGGER.warning(
            "Unknown planner preset %r for entry %s; using %s",
            configured_preset,
            entry.entry_id,
            DEFAULT_PRESET,
        )
        # Repair stale or manually edited options before constructing the
        # coordinator so runtime selection, cache identity and the next options
        # form all agree on one supported preset.
        hass.config_entries.async_update_entry(
            entry,
            options={**entry.options, CONF_PLANNER_PRESET: DEFAULT_PRESET},
        )

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

    outlook_languages = resolve_outlook_languages(
        entry.options.get(CONF_OUTLOOK_LANGUAGES, []),
        hass.config.language,
    )
    await hass.async_add_executor_job(
        preload_plan_outlook_catalogs, outlook_languages
    )
    record_plans = bool(entry.options.get(CONF_RECORD_PLANNER_REPRODUCTIONS, False))
    retention_days = entry.options.get(
        CONF_PLANNER_REPRODUCTION_RETENTION_DAYS,
        DEFAULT_PLANNER_REPRODUCTION_RETENTION_DAYS,
    )
    if type(retention_days) is not int or not 1 <= retention_days <= MAX_PLANNER_REPRODUCTION_RETENTION_DAYS:
        _LOGGER.warning("Invalid planner reproduction retention; using the 14-day default")
        retention_days = DEFAULT_PLANNER_REPRODUCTION_RETENTION_DAYS
    coordinator = WattPlanCoordinator(
        hass,
        entry_id=entry.entry_id,
        config_entry=entry,
        config_fingerprint=config_fingerprint(entry),
        planner_preset=entry.options.get(CONF_PLANNER_PRESET, DEFAULT_PRESET),
        update_interval=timedelta(minutes=int(entry.data[CONF_SLOT_MINUTES])),
        planning_enabled=bool(entry.options.get(CONF_PLANNING_ENABLED, True)),
        action_emission_enabled=bool(entry.options.get(CONF_ACTION_EMISSION_ENABLED, True)),
        outlook_languages=outlook_languages,
        record_planner_reproductions=record_plans,
        planner_reproduction_retention_days=retention_days,
        integration_version=(
            str((await async_get_integration(hass, DOMAIN)).version or "unknown")
            if record_plans else "unknown"
        ),
    )
    entry.runtime_data = WattPlanRuntimeData(
        coordinator=coordinator,
        last_run_at=datetime.now(tz=UTC),
        outlook_languages=outlook_languages,
    )
    if bool(entry.options.get(CONF_HISTORICAL_COST_TRACKING_ENABLED, False)):
        tracker = HistoricalCostTracker(
            hass,
            entry,
            slot_minutes=int(entry.data[CONF_SLOT_MINUTES]),
            actuals_recorder=coordinator.planner_history.async_record_actuals if record_plans else None,
        )
        entry.runtime_data.historical_tracker = tracker
        await tracker.async_start()
        coordinator.planner_history.set_actuals_provider(tracker.store.export_actuals)

        @callback
        def _async_flush_history_on_stop(_event: Event) -> None:
            hass.async_create_task(tracker.async_shutdown())

        entry.async_on_unload(
            hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STOP,
                _async_flush_history_on_stop,
            )
        )

    # Battery targets are user intent and survive reloads and restarts.
    target_store = BatteryTargetStore(hass, entry.entry_id)
    entry.runtime_data.battery_target_store = target_store
    entry.runtime_data.battery_targets = await target_store.async_load(
        {
            subentry.subentry_id
            for subentry in entry.subentries.values()
            if subentry.subentry_type == SUBENTRY_TYPE_BATTERY
        }
    )

    had_snapshot = await coordinator.async_restore_snapshot()
    entry.async_on_unload(entry.add_update_listener(async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if had_snapshot:
        # The restored plan keeps the sensors available; replace it with fresh
        # data in the background so slow sources cannot delay setup.
        entry.async_create_background_task(
            hass, async_initial_plan_then_schedule(entry), f"{DOMAIN} initial plan"
        )
    else:
        await async_initial_plan_then_schedule(entry)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: WattPlanConfigEntry) -> bool:
    """Unload a config entry."""
    if entry.runtime_data.historical_tracker is not None:
        await entry.runtime_data.historical_tracker.async_shutdown()
    await entry.runtime_data.coordinator.async_shutdown()
    if (target_store := entry.runtime_data.battery_target_store) is not None:
        await target_store.async_save(entry.runtime_data.battery_targets)
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
