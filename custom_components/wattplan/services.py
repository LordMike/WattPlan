"""Service schemas and handlers for WattPlan."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
import json
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_DEVICE_ID, ATTR_ENTITY_ID, CONF_NAME
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.util import dt as dt_util

from .const import (
    ATTR_BATTERY,
    ATTR_ENTRY_ID,
    ATTR_REACH_AT,
    ATTR_RUN_OPTIMIZE,
    ATTR_SOC_KWH,
    CONF_SOURCE_MODE,
    CONF_SOURCE_USAGE,
    CONF_SOURCES,
    DOMAIN,
    SERVICE_CLEAR_TARGET,
    SERVICE_EXPORT_PLANNER_INPUT,
    SERVICE_EXPORT_USAGE_FORECAST_DEBUG,
    SERVICE_RUN_OPTIMIZE_NOW,
    SERVICE_RUN_PLAN_NOW,
    SERVICE_SET_TARGET,
    SOURCE_MODE_BUILT_IN,
    SUBENTRY_TYPE_BATTERY,
)
from .runtime import BatteryTarget, WattPlanConfigEntry, WattPlanRuntimeData, mark_runtime_updated
from .source_pipeline import build_source_base_provider
from .target_runtime import clear_battery_target, set_battery_target
from .coordinator import CycleTrigger
from .forecast_provider import ForecastProvider

SET_TARGET_SERVICE_SCHEMA = vol.Schema(
    vol.All(
        {
            vol.Optional(ATTR_BATTERY): cv.string,
            vol.Optional(ATTR_ENTITY_ID): cv.comp_entity_ids,
            vol.Optional(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string]),
            vol.Required(ATTR_SOC_KWH): vol.All(vol.Coerce(float), vol.Range(min=0)),
            vol.Required(ATTR_REACH_AT): cv.datetime,
            vol.Optional(ATTR_ENTRY_ID): cv.string,
            vol.Optional(ATTR_RUN_OPTIMIZE, default=True): cv.boolean,
        },
        cv.has_at_least_one_key(ATTR_BATTERY, ATTR_ENTITY_ID, ATTR_DEVICE_ID),
    )
)

CLEAR_TARGET_SERVICE_SCHEMA = vol.Schema(
    vol.All(
        {
            vol.Optional(ATTR_BATTERY): cv.string,
            vol.Optional(ATTR_ENTITY_ID): cv.comp_entity_ids,
            vol.Optional(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string]),
            vol.Optional(ATTR_ENTRY_ID): cv.string,
            vol.Optional(ATTR_RUN_OPTIMIZE, default=True): cv.boolean,
        },
        cv.has_at_least_one_key(ATTR_BATTERY, ATTR_ENTITY_ID, ATTR_DEVICE_ID),
    )
)

RUN_NOW_SERVICE_SCHEMA = vol.Schema(
    {vol.Optional(ATTR_ENTRY_ID): cv.string, vol.Optional(CONF_NAME): cv.string}
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


def subentry_name(subentry: Any) -> str:
    """Return the semantic name of a subentry."""
    return str(subentry.data.get(CONF_NAME, subentry.title))


def subentry_id_from_entity_unique_id(unique_id: str) -> str | None:
    """Extract WattPlan subentry ID from a WattPlan entity unique ID."""
    parts = unique_id.split(":")
    if len(parts) < 3 or parts[1] == "entry":
        return None
    return parts[1]


def loaded_entries(
    hass: HomeAssistant, entry_id_filter: str | None
) -> list[WattPlanConfigEntry]:
    """Return loaded WattPlan entries matching optional filter."""
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
        and (not entry_id_filter or entry.entry_id == entry_id_filter)
    ]


def resolve_run_entries(hass: HomeAssistant, call: ServiceCall) -> list[WattPlanConfigEntry]:
    """Resolve run-service targets using optional name or entry filters."""
    entry_id_filter = call.data.get(ATTR_ENTRY_ID)
    name_filter = call.data.get(CONF_NAME)
    if name_filter is not None:
        name_filter = str(name_filter).strip()
        if not name_filter:
            raise ServiceValidationError("`name` must not be empty")

    matched = loaded_entries(hass, entry_id_filter)
    if name_filter:
        matched = [entry for entry in matched if entry.title.casefold() == name_filter.casefold()]

    if entry_id_filter or name_filter:
        if not matched:
            raise ServiceValidationError("No loaded WattPlan entries matched the filters")
        return matched

    if not matched:
        raise ServiceValidationError("No loaded WattPlan entries found")
    if len(matched) > 1:
        raise ServiceValidationError(
            "Multiple WattPlan entries are loaded; provide `name` or `entry_id`"
        )
    return matched


def resolve_single_run_entry(
    hass: HomeAssistant,
    call: ServiceCall,
    *,
    label: str,
) -> WattPlanConfigEntry:
    """Resolve a single matching entry for handlers that require one entry."""
    matched = resolve_run_entries(hass, call)
    if len(matched) != 1:
        raise ServiceValidationError(f"{label} requires exactly one matching WattPlan entry")
    return matched[0]


def _battery_name_filter(call: ServiceCall) -> str:
    battery_name = str(call.data.get(ATTR_BATTERY, "")).strip()
    if ATTR_BATTERY in call.data and not battery_name:
        raise ServiceValidationError("`battery` must not be empty")
    return battery_name


def _matched_loaded_entries(
    hass: HomeAssistant, entry_id_filter: str | None
) -> dict[str, WattPlanConfigEntry]:
    matched = loaded_entries(hass, entry_id_filter)
    if not matched:
        raise ServiceValidationError("No loaded WattPlan entries matched the filters")
    return {entry.entry_id: entry for entry in matched}


def _matching_battery_targets(
    hass: HomeAssistant,
    call: ServiceCall,
    *,
    loaded: dict[str, WattPlanConfigEntry],
) -> set[tuple[str, str]]:
    matches: set[tuple[str, str]] = set()
    battery_name = _battery_name_filter(call)

    if battery_name:
        for entry in loaded.values():
            for subentry in entry.subentries.values():
                if subentry.subentry_type != SUBENTRY_TYPE_BATTERY:
                    continue
                if subentry_name(subentry).casefold() == battery_name.casefold():
                    matches.add((entry.entry_id, subentry.subentry_id))

    entity_registry = er.async_get(hass)
    for entity_id in call.data.get(ATTR_ENTITY_ID, []):
        entity_entry = entity_registry.async_get(entity_id)
        if entity_entry is None:
            raise ServiceValidationError(f"Entity `{entity_id}` was not found")
        entry = loaded.get(entity_entry.config_entry_id)
        if entry is None:
            continue
        subentry_id = subentry_id_from_entity_unique_id(entity_entry.unique_id)
        if subentry_id is None:
            continue
        subentry = entry.subentries.get(subentry_id)
        if subentry is not None and subentry.subentry_type == SUBENTRY_TYPE_BATTERY:
            matches.add((entry.entry_id, subentry_id))

    for device_id in call.data.get(ATTR_DEVICE_ID, []):
        for entity_entry in er.async_entries_for_device(
            entity_registry, device_id, include_disabled_entities=True
        ):
            entry = loaded.get(entity_entry.config_entry_id)
            if entry is None:
                continue
            subentry_id = subentry_id_from_entity_unique_id(entity_entry.unique_id)
            if subentry_id is None:
                continue
            subentry = entry.subentries.get(subentry_id)
            if subentry is not None and subentry.subentry_type == SUBENTRY_TYPE_BATTERY:
                matches.add((entry.entry_id, subentry_id))

    if not matches:
        raise ServiceValidationError("No battery targets matched the provided selectors")
    return matches


def _iter_target_runtime_data(
    loaded: dict[str, WattPlanConfigEntry], matches: Iterable[tuple[str, str]]
) -> list[tuple[WattPlanRuntimeData, str]]:
    return [
        (entry.runtime_data, subentry_id)
        for entry_id, subentry_id in matches
        if (entry := loaded.get(entry_id)) is not None
    ]


def _format_service_export(
    *, payload_key: str, payload: Any, as_json_key: str, as_json: bool
) -> dict[str, Any]:
    if as_json:
        return {as_json_key: json.dumps(payload, separators=(",", ":"), sort_keys=True)}
    return {payload_key: payload}


async def async_handle_set_target_service(hass: HomeAssistant, call: ServiceCall) -> None:
    loaded = _matched_loaded_entries(hass, call.data.get(ATTR_ENTRY_ID))
    matches = _matching_battery_targets(hass, call, loaded=loaded)
    target = BatteryTarget(
        soc_kwh=float(call.data[ATTR_SOC_KWH]),
        reach_at=dt_util.as_utc(call.data[ATTR_REACH_AT]),
    )
    for runtime_data, subentry_id in _iter_target_runtime_data(loaded, matches):
        set_battery_target(runtime_data, subentry_id, target)
    if call.data[ATTR_RUN_OPTIMIZE]:
        for entry_id in {eid for eid, _ in matches}:
            entry = loaded[entry_id]
            await entry.runtime_data.coordinator.async_plan(trigger=CycleTrigger.SERVICE)
            mark_runtime_updated(entry.runtime_data, when=datetime.now(tz=UTC))


async def async_handle_clear_target_service(hass: HomeAssistant, call: ServiceCall) -> None:
    loaded = _matched_loaded_entries(hass, call.data.get(ATTR_ENTRY_ID))
    matches = _matching_battery_targets(hass, call, loaded=loaded)
    cleared_any = False
    for runtime_data, subentry_id in _iter_target_runtime_data(loaded, matches):
        cleared_any = clear_battery_target(runtime_data, subentry_id) or cleared_any
    if not cleared_any:
        raise ServiceValidationError("No active battery targets matched the provided selectors")
    if call.data[ATTR_RUN_OPTIMIZE]:
        for entry_id in {eid for eid, _ in matches}:
            entry = loaded[entry_id]
            await entry.runtime_data.coordinator.async_plan(trigger=CycleTrigger.SERVICE)
            mark_runtime_updated(entry.runtime_data, when=datetime.now(tz=UTC))


async def async_handle_run_optimize_now_service(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    for entry in resolve_run_entries(hass, call):
        await entry.runtime_data.coordinator.async_plan(trigger=CycleTrigger.SERVICE)
        mark_runtime_updated(entry.runtime_data, when=datetime.now(tz=UTC))


async def async_handle_run_plan_now_service(hass: HomeAssistant, call: ServiceCall) -> None:
    for entry in resolve_run_entries(hass, call):
        await entry.runtime_data.coordinator.async_emit(trigger=CycleTrigger.SERVICE)
        mark_runtime_updated(entry.runtime_data, when=datetime.now(tz=UTC))


async def async_handle_export_planner_input_service(
    hass: HomeAssistant, call: ServiceCall
) -> dict[str, Any]:
    entry = resolve_single_run_entry(hass, call, label="Planner input export")
    request = await entry.runtime_data.coordinator.async_build_planner_input_export()
    return _format_service_export(
        payload_key="model",
        payload=request["optimizer_params"],
        as_json_key="model_json",
        as_json=call.data["as_json"],
    )


async def async_handle_export_usage_forecast_debug_service(
    hass: HomeAssistant, call: ServiceCall
) -> dict[str, Any]:
    entry = resolve_single_run_entry(hass, call, label="Usage forecast debug export")
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
    return _format_service_export(
        payload_key="debug",
        payload=debug_payload,
        as_json_key="debug_json",
        as_json=call.data["as_json"],
    )


SERVICE_SPECS = (
    (SERVICE_SET_TARGET, async_handle_set_target_service, SET_TARGET_SERVICE_SCHEMA, None),
    (SERVICE_CLEAR_TARGET, async_handle_clear_target_service, CLEAR_TARGET_SERVICE_SCHEMA, None),
    (SERVICE_RUN_OPTIMIZE_NOW, async_handle_run_optimize_now_service, RUN_NOW_SERVICE_SCHEMA, None),
    (SERVICE_RUN_PLAN_NOW, async_handle_run_plan_now_service, RUN_NOW_SERVICE_SCHEMA, None),
    (
        SERVICE_EXPORT_PLANNER_INPUT,
        async_handle_export_planner_input_service,
        EXPORT_PLANNER_INPUT_SERVICE_SCHEMA,
        SupportsResponse.ONLY,
    ),
    (
        SERVICE_EXPORT_USAGE_FORECAST_DEBUG,
        async_handle_export_usage_forecast_debug_service,
        EXPORT_USAGE_FORECAST_DEBUG_SERVICE_SCHEMA,
        SupportsResponse.ONLY,
    ),
)
