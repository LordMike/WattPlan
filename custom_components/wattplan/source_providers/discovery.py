"""Discovery and auto-detect helpers for source providers."""

from __future__ import annotations

from contextlib import suppress
from json import JSONDecodeError
import json
from typing import Any

from homeassistant.components.energy.types import GetSolarForecastType
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.integration_platform import (
    async_process_integration_platforms,
)

from ..adapter_auto import (
    AdapterAutoDetectResult,
    auto_detect_mapping,
    summarize_auto_detect_candidates,
)
from ..source_types import SourceProviderError
from .config import CONF_WATTPLAN_ENTITY_ID


def _decoded_state_root(state: Any) -> dict[str, Any]:
    """Return state attributes with decoded JSON state when available."""
    root = dict(state.attributes)
    with suppress(JSONDecodeError):
        root["state_json"] = json.loads(state.state)
    return root


def _summarized_candidates(root: Any) -> list[dict[str, Any]]:
    """Return auto-detect candidate summaries for diagnostics."""
    return [
        {
            "path": summary.path or "<root>",
            "row_count": summary.row_count,
            "sample_type": summary.sample_type,
            "timestamp_keys": list(summary.timestamp_keys),
            "numeric_keys": list(summary.numeric_keys),
            "compatible": summary.compatible,
            "reason": summary.reason,
        }
        for summary in summarize_auto_detect_candidates(root)
    ]


async def async_get_energy_solar_forecast_platforms(
    hass: HomeAssistant,
) -> dict[str, GetSolarForecastType]:
    """Return domains that provide Energy solar forecasts."""
    platforms: dict[str, GetSolarForecastType] = {}

    def _process_platform(
        hass: HomeAssistant,
        domain: str,
        platform: Any,
    ) -> None:
        """Collect integrations exposing Energy solar forecasts."""
        callback = getattr(platform, "async_get_solar_forecast", None)
        if callback is None:
            return
        platforms[domain] = callback

    await async_process_integration_platforms(
        hass,
        "energy",
        _process_platform,
        wait_for_platforms=True,
    )
    return platforms


async def async_get_energy_solar_forecast_entries(
    hass: HomeAssistant,
) -> list[ConfigEntry]:
    """Return loaded config entries that can provide Energy solar forecasts."""
    forecast_platforms = await async_get_energy_solar_forecast_platforms(hass)
    if not forecast_platforms:
        return []

    return [
        entry
        for entry in hass.config_entries.async_entries()
        if entry.domain in forecast_platforms and entry.state == ConfigEntryState.LOADED
    ]


async def async_auto_detect_entity_adapter(
    hass: HomeAssistant,
    entity_ids: list[str],
) -> list[AdapterAutoDetectResult]:
    """Return one detected mapping for each selected entity."""
    detected_mappings: list[tuple[str, AdapterAutoDetectResult]] = []
    entity_candidates: dict[str, list[dict[str, Any]]] = {}
    for entity_id in entity_ids:
        state = hass.states.get(entity_id)
        if state is None:
            raise SourceProviderError(
                "source_fetch",
                f"Source entity `{entity_id}` was not found",
                details={"entity_id": entity_id},
            )

        root = _decoded_state_root(state)
        entity_candidates[entity_id] = _summarized_candidates(root)

        detected = auto_detect_mapping(root)
        if detected is not None:
            detected_mappings.append((entity_id, detected))

    if not detected_mappings or len(detected_mappings) != len(entity_ids):
        raise SourceProviderError(
            "source_validation",
            "One or more selected entities returned no compatible forecast list",
            details={
                "entity_ids": entity_ids,
                "diagnostic_kind": "auto_detect_no_match",
                "entity_candidates": entity_candidates,
                "detected_mappings": [
                    {
                        "entity_id": entity_id,
                        "root_key": detected.root_key,
                        "time_key": detected.time_key,
                        "value_key": detected.value_key,
                    }
                    for entity_id, detected in detected_mappings
                ],
            },
        )

    return [detected for _, detected in detected_mappings]


async def async_auto_detect_service_adapter(
    hass: HomeAssistant,
    service_name: str,
) -> AdapterAutoDetectResult:
    """Return mapping inferred from a no-argument service response."""
    from .payloads import async_service_response

    try:
        response = await async_service_response(hass, service_name)
    except SourceProviderError as err:
        raise SourceProviderError(
            "source_validation",
            f"Service `{service_name}` is invalid",
            details={"service": service_name},
        ) from err
    candidates = _summarized_candidates(response)
    detected = auto_detect_mapping(response)
    if detected is None:
        raise SourceProviderError(
            "source_validation",
            f"Service `{service_name}` returned no compatible forecast list",
            details={
                "service": service_name,
                "diagnostic_kind": "auto_detect_no_match",
                "candidates": candidates,
            },
        )
    return detected


__all__ = [
    "CONF_WATTPLAN_ENTITY_ID",
    "async_auto_detect_entity_adapter",
    "async_auto_detect_service_adapter",
    "async_get_energy_solar_forecast_entries",
    "async_get_energy_solar_forecast_platforms",
]
