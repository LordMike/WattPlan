"""The WattPlan integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
import json
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import ATTR_DEVICE_ID, ATTR_ENTITY_ID, CONF_NAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_BATTERY,
    ATTR_ENTRY_ID,
    ATTR_REACH_AT,
    ATTR_SOC_KWH,
    CONF_ACTION_EMISSION_ENABLED,
    CONF_PLANNING_ENABLED,
    CONF_SLOT_MINUTES,
    CONF_SOURCE_MODE,
    CONF_SOURCE_USAGE,
    CONF_SOURCES,
    DOMAIN,
    SERVICE_EXPORT_PLANNER_INPUT,
    SERVICE_EXPORT_USAGE_FORECAST_DEBUG,
    SERVICE_RUN_OPTIMIZE_NOW,
    SERVICE_RUN_PLAN_NOW,
    SERVICE_SET_TARGET,
    SOURCE_MODE_BUILT_IN,
    SUBENTRY_TYPE_BATTERY,
)
from .coordinator import CycleTrigger, WattPlanCoordinator
from .forecast_provider import ForecastProvider
from .source_pipeline import build_source_base_provider

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]

DATA_ENTRY_COUNT = "entry_count"
DATA_SERVICE_REGISTERED = "service_registered"


@dataclass
class BatteryTarget:
    """Battery target requested by the user."""

    soc_kwh: float
    reach_at: datetime


@dataclass
class WattPlanRuntimeData:
    """Runtime data for the WattPlan integration."""

    coordinator: WattPlanCoordinator
    last_run_at: datetime
    optimizer_state: str | None = None
    runtime_update_listeners: set[Callable[[], None]] = field(default_factory=set)
    battery_targets: dict[str, BatteryTarget] = field(default_factory=dict)
    battery_target_update_listeners: dict[str, set[Callable[[], None]]] = field(
        default_factory=dict
    )


type WattPlanConfigEntry = ConfigEntry[WattPlanRuntimeData]

SET_TARGET_SERVICE_SCHEMA = vol.Schema(
    vol.All(
        {
            vol.Optional(ATTR_BATTERY): cv.string,
            vol.Optional(ATTR_ENTITY_ID): cv.comp_entity_ids,
            vol.Optional(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string]),
            vol.Required(ATTR_SOC_KWH): vol.All(vol.Coerce(float), vol.Range(min=0)),
            vol.Required(ATTR_REACH_AT): cv.datetime,
            vol.Optional(ATTR_ENTRY_ID): cv.string,
        },
        cv.has_at_least_one_key(ATTR_BATTERY, ATTR_ENTITY_ID, ATTR_DEVICE_ID),
    )
)

RUN_NOW_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTRY_ID): cv.string,
        vol.Optional(CONF_NAME): cv.string,
    }
)

EXPORT_PLANNER_INPUT_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTRY_ID): cv.string,
        vol.Optional(CONF_NAME): cv.string,
        vol.Optional("as_json", default=False): cv.boolean,
    }
)

EXPORT_USAGE_FORECAST_DEBUG_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTRY_ID): cv.string,
        vol.Optional(CONF_NAME): cv.string,
        vol.Optional("as_json", default=False): cv.boolean,
    }
)


def _subentry_name(subentry: Any) -> str:
    """Return the semantic name of a subentry."""
    return str(subentry.data.get(CONF_NAME, subentry.title))


def _subentry_id_from_entity_unique_id(unique_id: str) -> str | None:
    """Extract WattPlan subentry ID from a WattPlan entity unique ID."""
    parts = unique_id.split(":")
    if len(parts) < 3:
        return None
    if parts[1] == "entry":
        return None
    return parts[1]


def _loaded_entries(
    hass: HomeAssistant, entry_id_filter: str | None
) -> list[WattPlanConfigEntry]:
    """Return loaded WattPlan entries matching optional filter."""
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
        and (not entry_id_filter or entry.entry_id == entry_id_filter)
    ]


def _resolve_run_entries(hass: HomeAssistant, call: ServiceCall) -> list[WattPlanConfigEntry]:
    """Resolve run-service targets using optional name/entry filters."""
    entry_id_filter = call.data.get(ATTR_ENTRY_ID)
    name_filter = call.data.get(CONF_NAME)
    if name_filter is not None:
        name_filter = str(name_filter).strip()
        if not name_filter:
            raise ServiceValidationError("`name` must not be empty")

    loaded = _loaded_entries(hass, entry_id_filter)
    if name_filter:
        loaded = [entry for entry in loaded if entry.title.casefold() == name_filter.casefold()]

    if entry_id_filter or name_filter:
        if not loaded:
            raise ServiceValidationError("No loaded WattPlan entries matched the filters")
        return loaded

    if not loaded:
        raise ServiceValidationError("No loaded WattPlan entries found")
    if len(loaded) > 1:
        raise ServiceValidationError(
            "Multiple WattPlan entries are loaded; provide `name` or `entry_id`"
        )
    return loaded


async def _async_handle_set_target_service(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    """Store a user target for one or more batteries."""
    battery_name = call.data.get(ATTR_BATTERY, "").strip()
    if ATTR_BATTERY in call.data and not battery_name:
        raise ServiceValidationError("`battery` must not be empty")

    entry_id_filter = call.data.get(ATTR_ENTRY_ID)
    matched_entries = _loaded_entries(hass, entry_id_filter)
    if not matched_entries:
        raise ServiceValidationError("No loaded WattPlan entries matched the filters")
    loaded_entries: dict[str, WattPlanConfigEntry] = {
        entry.entry_id: entry for entry in matched_entries
    }

    name_matches: set[tuple[str, str]] = set()
    entity_matches: set[tuple[str, str]] = set()
    device_matches: set[tuple[str, str]] = set()

    if battery_name:
        for entry in loaded_entries.values():
            for subentry in entry.subentries.values():
                if subentry.subentry_type != SUBENTRY_TYPE_BATTERY:
                    continue
                if _subentry_name(subentry).casefold() != battery_name.casefold():
                    continue
                name_matches.add((entry.entry_id, subentry.subentry_id))

    entity_registry = er.async_get(hass)
    for entity_id in call.data.get(ATTR_ENTITY_ID, []):
        entity_entry = entity_registry.async_get(entity_id)
        if entity_entry is None:
            raise ServiceValidationError(f"Entity `{entity_id}` was not found")
        entry = loaded_entries.get(entity_entry.config_entry_id)
        if entry is None:
            continue
        subentry_id = _subentry_id_from_entity_unique_id(entity_entry.unique_id)
        if subentry_id is None:
            continue
        subentry = entry.subentries.get(subentry_id)
        if subentry is None or subentry.subentry_type != SUBENTRY_TYPE_BATTERY:
            continue
        entity_matches.add((entry.entry_id, subentry_id))

    for device_id in call.data.get(ATTR_DEVICE_ID, []):
        for entity_entry in er.async_entries_for_device(
            entity_registry, device_id, include_disabled_entities=True
        ):
            entry = loaded_entries.get(entity_entry.config_entry_id)
            if entry is None:
                continue
            subentry_id = _subentry_id_from_entity_unique_id(entity_entry.unique_id)
            if subentry_id is None:
                continue
            subentry = entry.subentries.get(subentry_id)
            if subentry is None or subentry.subentry_type != SUBENTRY_TYPE_BATTERY:
                continue
            device_matches.add((entry.entry_id, subentry_id))

    matches: set[tuple[str, str]] = set()
    matches.update(name_matches)
    matches.update(entity_matches)
    matches.update(device_matches)

    if not matches:
        raise ServiceValidationError("No battery targets matched the provided selectors")

    reach_at = dt_util.as_utc(call.data[ATTR_REACH_AT])
    target_soc = float(call.data[ATTR_SOC_KWH])
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id not in loaded_entries:
            continue
        runtime_data = entry.runtime_data
        for match_entry_id, subentry_id in matches:
            if match_entry_id != entry.entry_id:
                continue
            runtime_data.battery_targets[subentry_id] = BatteryTarget(
                soc_kwh=target_soc,
                reach_at=reach_at,
            )
            for listener in list(
                runtime_data.battery_target_update_listeners.get(subentry_id, ())
            ):
                listener()


async def _async_handle_run_optimize_now_service(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    """Request an immediate planning run."""
    matched_entries = _resolve_run_entries(hass, call)

    for entry in matched_entries:
        await entry.runtime_data.coordinator.async_plan(trigger=CycleTrigger.SERVICE)
        _async_mark_runtime_updated(entry.runtime_data)


async def _async_handle_run_plan_now_service(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    """Request immediate action emission from current plan."""
    matched_entries = _resolve_run_entries(hass, call)

    for entry in matched_entries:
        await entry.runtime_data.coordinator.async_emit(trigger=CycleTrigger.SERVICE)
        _async_mark_runtime_updated(entry.runtime_data)


async def _async_handle_export_planner_input_service(
    hass: HomeAssistant, call: ServiceCall
) -> dict[str, Any]:
    """Rebuild and return the current planner model payload."""
    matched_entries = _resolve_run_entries(hass, call)
    if len(matched_entries) != 1:
        raise ServiceValidationError(
            "Planner input export requires exactly one matching WattPlan entry"
        )

    entry = matched_entries[0]
    request = await entry.runtime_data.coordinator.async_build_planner_input_export()
    model = request["optimizer_params"]
    if call.data["as_json"]:
        return {
            "model_json": json.dumps(model, separators=(",", ":"), sort_keys=True)
        }
    return {"model": model}


async def _async_handle_export_usage_forecast_debug_service(
    hass: HomeAssistant, call: ServiceCall
) -> dict[str, Any]:
    """Return raw built-in usage forecast debug data for one setup."""
    matched_entries = _resolve_run_entries(hass, call)
    if len(matched_entries) != 1:
        raise ServiceValidationError(
            "Usage forecast debug export requires exactly one matching WattPlan entry"
        )

    entry = matched_entries[0]
    sources = entry.data.get(CONF_SOURCES, {})
    if not isinstance(sources, dict):
        raise ServiceValidationError("Source configuration is missing or invalid")
    usage_source = sources.get(CONF_SOURCE_USAGE, {})
    if not isinstance(usage_source, dict):
        raise ServiceValidationError("Usage source configuration is missing or invalid")
    if usage_source.get(CONF_SOURCE_MODE) != SOURCE_MODE_BUILT_IN:
        raise ServiceValidationError("Usage source is not configured as built in")

    provider = build_source_base_provider(
        hass,
        source_key=CONF_SOURCE_USAGE,
        source_config=usage_source,
    )
    if not isinstance(provider, ForecastProvider):
        raise ServiceValidationError("Usage source is not configured as built in")
    request = await entry.runtime_data.coordinator.async_build_planner_input_export()
    debug_payload = await provider.async_debug_payload(request["window"])
    if call.data["as_json"]:
        return {
            "debug_json": json.dumps(debug_payload, separators=(",", ":"), sort_keys=True)
        }
    return {"debug": debug_payload}


def _async_mark_runtime_updated(runtime_data: WattPlanRuntimeData) -> None:
    """Update runtime timestamp and notify listeners."""
    runtime_data.last_run_at = datetime.now(tz=UTC)
    for listener in list(runtime_data.runtime_update_listeners):
        listener()


async def async_setup_entry(hass: HomeAssistant, entry: WattPlanConfigEntry) -> bool:
    """Set up WattPlan from a config entry."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if not domain_data.get(DATA_SERVICE_REGISTERED, False):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_TARGET,
            partial(_async_handle_set_target_service, hass),
            schema=SET_TARGET_SERVICE_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_RUN_OPTIMIZE_NOW,
            partial(_async_handle_run_optimize_now_service, hass),
            schema=RUN_NOW_SERVICE_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_RUN_PLAN_NOW,
            partial(_async_handle_run_plan_now_service, hass),
            schema=RUN_NOW_SERVICE_SCHEMA,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_EXPORT_PLANNER_INPUT,
            partial(_async_handle_export_planner_input_service, hass),
            schema=EXPORT_PLANNER_INPUT_SERVICE_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )
        hass.services.async_register(
            DOMAIN,
            SERVICE_EXPORT_USAGE_FORECAST_DEBUG,
            partial(_async_handle_export_usage_forecast_debug_service, hass),
            schema=EXPORT_USAGE_FORECAST_DEBUG_SERVICE_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )
        domain_data[DATA_SERVICE_REGISTERED] = True
    domain_data[DATA_ENTRY_COUNT] = int(domain_data.get(DATA_ENTRY_COUNT, 0)) + 1

    coordinator = WattPlanCoordinator(
        hass,
        entry_id=entry.entry_id,
        update_interval=timedelta(minutes=int(entry.data[CONF_SLOT_MINUTES])),
        planning_enabled=bool(entry.options.get(CONF_PLANNING_ENABLED, True)),
        action_emission_enabled=bool(
            entry.options.get(CONF_ACTION_EMISSION_ENABLED, True)
        ),
    )
    entry.runtime_data = WattPlanRuntimeData(
        coordinator=coordinator,
        last_run_at=datetime.now(tz=UTC),
    )
    await coordinator.async_restore_snapshot()
    _async_mark_runtime_updated(entry.runtime_data)
    entry.async_on_unload(entry.add_update_listener(async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
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
        hass.services.async_remove(DOMAIN, SERVICE_SET_TARGET)
        hass.services.async_remove(DOMAIN, SERVICE_RUN_OPTIMIZE_NOW)
        hass.services.async_remove(DOMAIN, SERVICE_RUN_PLAN_NOW)
        hass.services.async_remove(DOMAIN, SERVICE_EXPORT_PLANNER_INPUT)
        hass.services.async_remove(DOMAIN, SERVICE_EXPORT_USAGE_FORECAST_DEBUG)
        domain_data[DATA_SERVICE_REGISTERED] = False
    return True


async def async_update_listener(hass: HomeAssistant, entry: WattPlanConfigEntry) -> None:
    """Reload entry when config, options, or subentries change."""
    await hass.config_entries.async_reload(entry.entry_id)
