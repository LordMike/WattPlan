"""Source providers for WattPlan forecast inputs."""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from itertools import pairwise
import json
import logging
from typing import Any

from homeassistant.components.energy.types import GetSolarForecastType
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.integration_platform import (
    async_process_integration_platforms,
)
from homeassistant.helpers.template import Template

from .adapter_auto import (
    AdapterAutoDetectResult,
    auto_detect_mapping,
    resolve_nested_value,
    summarize_auto_detect_candidates,
)
from .const import (
    ADAPTER_TYPE_ATTRIBUTE_OBJECTS,
    ADAPTER_TYPE_ATTRIBUTE_VALUES,
    ADAPTER_TYPE_SERVICE_RESPONSE,
    AGGREGATION_MODE_FIRST,
    AGGREGATION_MODE_LAST,
    AGGREGATION_MODE_MAX,
    AGGREGATION_MODE_MEAN,
    AGGREGATION_MODE_MIN,
    CLAMP_MODE_NEAREST,
    CLAMP_MODE_NONE,
    CONF_ADAPTER_TYPE,
    CONF_AGGREGATION_MODE,
    CONF_CLAMP_MODE,
    CONF_CONFIG_ENTRY_ID,
    CONF_EDGE_FILL_MODE,
    CONF_HISTORY_DAYS,
    CONF_PROVIDERS,
    CONF_RESAMPLE_MODE,
    CONF_SERVICE,
    CONF_SOURCE_MODE,
    CONF_TEMPLATE,
    CONF_TIME_KEY,
    CONF_VALUE_KEY,
    EDGE_FILL_MODE_HOLD,
    EDGE_FILL_MODE_NONE,
    RESAMPLE_MODE_FORWARD_FILL,
    RESAMPLE_MODE_LINEAR,
    RESAMPLE_MODE_NONE,
    SOURCE_MODE_BUILT_IN,
    SOURCE_MODE_ENERGY_PROVIDER,
    SOURCE_MODE_ENTITY_ADAPTER,
    SOURCE_MODE_SERVICE_ADAPTER,
    SOURCE_MODE_TEMPLATE,
)
from .forecast_provider import ForecastProvider
from .source_types import SourceProvider, SourceProviderError, SourceWindow

CONF_WATTPLAN_ENTITY_ID = "entity_id"
_LOGGER = logging.getLogger(__name__)
VALID_AGGREGATION_MODES = {
    AGGREGATION_MODE_FIRST,
    AGGREGATION_MODE_LAST,
    AGGREGATION_MODE_MEAN,
    AGGREGATION_MODE_MIN,
    AGGREGATION_MODE_MAX,
}
VALID_CLAMP_MODES = {CLAMP_MODE_NONE, CLAMP_MODE_NEAREST}
VALID_RESAMPLE_MODES = {
    RESAMPLE_MODE_NONE,
    RESAMPLE_MODE_FORWARD_FILL,
    RESAMPLE_MODE_LINEAR,
}
VALID_EDGE_FILL_MODES = {EDGE_FILL_MODE_NONE, EDGE_FILL_MODE_HOLD}


def source_mode(source_config: dict[str, Any]) -> str:
    """Return the configured source/provider mode."""
    mode = source_config.get(CONF_SOURCE_MODE)
    if isinstance(mode, str) and mode:
        return mode
    providers = source_config.get(CONF_PROVIDERS)
    if isinstance(providers, list) and providers:
        provider_mode = providers[0].get(CONF_SOURCE_MODE)
        if isinstance(provider_mode, str):
            return provider_mode
    return ""


def source_providers(source_config: dict[str, Any]) -> list[dict[str, Any]]:
    """Return provider configs for a source."""
    providers = source_config.get(CONF_PROVIDERS)
    if isinstance(providers, list) and providers:
        return [provider for provider in providers if isinstance(provider, dict)]

    if source_mode(source_config) == SOURCE_MODE_ENTITY_ADAPTER:
        entity_ids = source_config.get(CONF_WATTPLAN_ENTITY_ID)
        if isinstance(entity_ids, list) and entity_ids:
            return [
                {
                    **source_config,
                    CONF_WATTPLAN_ENTITY_ID: str(entity_id),
                }
                for entity_id in entity_ids
            ]

    return [source_config]


def primary_provider_config(source_config: dict[str, Any]) -> dict[str, Any]:
    """Return the first provider config for summaries and defaults."""
    providers = source_providers(source_config)
    return providers[0] if providers else {}


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


class BasePayloadProvider(ABC):
    """Base provider for raw payload acquisition."""

    def __init__(
        self,
        hass: HomeAssistant,
        source_name: str,
        source_config: dict[str, Any],
    ) -> None:
        """Initialize payload provider."""
        self._hass = hass
        self._source_name = source_name
        self._source_config = source_config

    @abstractmethod
    async def async_fetch_payload(self) -> Any:
        """Fetch source payload before normalization."""


class TemplatePayloadProvider(BasePayloadProvider):
    """Resolve payload from a Jinja template."""

    async def async_fetch_payload(self) -> Any:
        """Render template and return parsed native value."""
        template_value = self._source_config.get(CONF_TEMPLATE)
        if not template_value:
            raise SourceProviderError(
                "source_validation",
                f"{self._source_name} template is not configured",
                details={"source": self._source_name},
            )

        try:
            rendered = Template(str(template_value), self._hass).async_render(
                parse_result=True
            )
        except Exception as err:
            raise SourceProviderError(
                "source_fetch",
                f"{self._source_name} template failed to render: {err}",
                details={"source": self._source_name},
            ) from err

        if isinstance(rendered, str):
            raise SourceProviderError(
                "source_parse",
                (
                    f"{self._source_name} template rendered a string; "
                    "return a native list of values or point objects instead"
                ),
                details={"source": self._source_name},
            )

        return rendered


class EntityAdapterPayloadProvider(BasePayloadProvider):
    """Resolve payload from entity attributes."""

    async def async_fetch_payload(self) -> Any:
        """Load payload from configured entity adapter."""
        entity_id = self._source_config.get(CONF_WATTPLAN_ENTITY_ID)
        adapter_type = self._source_config.get(CONF_ADAPTER_TYPE)
        root_key = self._source_config.get(CONF_NAME)

        if not entity_id or not adapter_type or root_key is None:
            raise SourceProviderError(
                "source_validation",
                f"{self._source_name} entity adapter configuration is incomplete",
                details={"source": self._source_name},
            )

        if adapter_type not in {
            ADAPTER_TYPE_ATTRIBUTE_OBJECTS,
            ADAPTER_TYPE_ATTRIBUTE_VALUES,
        }:
            raise SourceProviderError(
                "source_validation",
                f"{self._source_name} adapter type `{adapter_type}` is not supported",
                details={"source": self._source_name, "adapter_type": adapter_type},
            )

        state = self._hass.states.get(entity_id)
        if state is None:
            raise SourceProviderError(
                "source_fetch",
                f"{self._source_name} source entity `{entity_id}` was not found",
                details={"source": self._source_name, "entity_id": entity_id},
            )

        root = dict(state.attributes)
        with suppress(json.JSONDecodeError):
            root["state_json"] = json.loads(state.state)

        payload = resolve_nested_value(root, str(root_key))
        if payload is None:
            raise SourceProviderError(
                "source_fetch",
                (
                    f"{self._source_name} attribute `{root_key}` was not found "
                    f"on `{entity_id}`"
                ),
                details={
                    "source": self._source_name,
                    "entity_id": entity_id,
                    "attribute": str(root_key),
                },
            )
        return payload


class ServiceResponsePayloadProvider(BasePayloadProvider):
    """Resolve payload from a no-argument service response."""

    async def async_fetch_payload(self) -> Any:
        """Call service and return configured root payload."""
        service_name = self._source_config.get(CONF_SERVICE)
        root_key = self._source_config.get(CONF_NAME, "")
        adapter_type = self._source_config.get(CONF_ADAPTER_TYPE)

        if not service_name or not adapter_type or root_key is None:
            raise SourceProviderError(
                "source_validation",
                f"{self._source_name} service adapter configuration is incomplete",
                details={"source": self._source_name},
            )

        if adapter_type != ADAPTER_TYPE_SERVICE_RESPONSE:
            raise SourceProviderError(
                "source_validation",
                f"{self._source_name} service adapter type `{adapter_type}` is not supported",
                details={"source": self._source_name, "adapter_type": adapter_type},
            )

        try:
            domain, service = str(service_name).split(".", 1)
        except ValueError as err:
            raise SourceProviderError(
                "source_validation",
                f"{self._source_name} service `{service_name}` is invalid",
                details={"source": self._source_name, "service": str(service_name)},
            ) from err

        response = await self._hass.services.async_call(
            domain,
            service,
            {},
            blocking=True,
            return_response=True,
        )
        payload = resolve_nested_value(response, str(root_key))
        if payload is None:
            raise SourceProviderError(
                "source_fetch",
                (
                    f"{self._source_name} root key `{root_key}` was not found "
                    f"in service `{service_name}` response"
                ),
                details={
                    "source": self._source_name,
                    "service": str(service_name),
                    "attribute": str(root_key),
                },
            )
        return payload


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

        root = dict(state.attributes)
        with suppress(json.JSONDecodeError):
            root["state_json"] = json.loads(state.state)
        entity_candidates[entity_id] = [
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
    try:
        domain, service = service_name.split(".", 1)
    except ValueError as err:
        raise SourceProviderError(
            "source_validation",
            f"Service `{service_name}` is invalid",
            details={"service": service_name},
        ) from err

    response = await hass.services.async_call(
        domain,
        service,
        {},
        blocking=True,
        return_response=True,
    )
    candidates = [
        {
            "path": summary.path or "<root>",
            "row_count": summary.row_count,
            "sample_type": summary.sample_type,
            "timestamp_keys": list(summary.timestamp_keys),
            "numeric_keys": list(summary.numeric_keys),
            "compatible": summary.compatible,
            "reason": summary.reason,
        }
        for summary in summarize_auto_detect_candidates(response)
    ]
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


class EnergySolarForecastPayloadProvider(BasePayloadProvider):
    """Resolve payload from an Energy solar forecast provider."""

    async def async_fetch_payload(self) -> list[dict[str, Any]]:
        """Fetch and normalize Energy solar forecast data."""
        config_entry_id = self._source_config.get(CONF_CONFIG_ENTRY_ID)
        if not config_entry_id:
            raise SourceProviderError(
                "source_validation",
                f"{self._source_name} Energy provider is not configured",
                details={"source": self._source_name},
            )

        platforms = await async_get_energy_solar_forecast_platforms(self._hass)
        entry = self._hass.config_entries.async_get_entry(str(config_entry_id))
        if (
            entry is None
            or entry.state != ConfigEntryState.LOADED
            or entry.domain not in platforms
        ):
            raise SourceProviderError(
                "source_fetch",
                f"{self._source_name} Energy provider is not available",
                details={
                    "source": self._source_name,
                    "config_entry_id": str(config_entry_id),
                    "provider_reason": "unavailable",
                },
            )

        forecast = await platforms[entry.domain](self._hass, entry.entry_id)
        if forecast is None:
            raise SourceProviderError(
                "source_fetch",
                f"{self._source_name} Energy provider returned no solar forecast",
                details={
                    "source": self._source_name,
                    "config_entry_id": entry.entry_id,
                    "provider_reason": "no_forecast",
                },
            )

        wh_hours = forecast.get("wh_hours")
        if not isinstance(wh_hours, dict):
            raise SourceProviderError(
                "source_parse",
                f"{self._source_name} Energy provider returned invalid forecast data",
                details={
                    "source": self._source_name,
                    "config_entry_id": entry.entry_id,
                    "provider_reason": "invalid_forecast",
                },
            )

        points: list[dict[str, Any]] = []
        for timestamp, value in sorted(wh_hours.items()):
            try:
                numeric_value = float(value) / 1000.0
            except (TypeError, ValueError) as err:
                raise SourceProviderError(
                    "source_parse",
                    (
                        f"{self._source_name} Energy provider returned invalid "
                        f"numeric value `{value}`"
                    ),
                    details={
                        "source": self._source_name,
                        "config_entry_id": entry.entry_id,
                        "provider_reason": "invalid_forecast",
                    },
                ) from err
            points.append({"start": str(timestamp), "value": numeric_value})
        return points


class TemplateAdapterSourceProvider(SourceProvider):
    """Configured source provider that returns exactly N numeric values."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        source_name: str,
        source_config: dict[str, Any],
    ) -> None:
        """Initialize one configured source provider instance."""
        self._source_name = source_name
        self._source_config = source_config
        self._aggregation_mode = self._aggregation_mode(source_config)
        self._clamp_mode = self._clamp_mode(source_config)
        self._resample_mode = self._resample_mode(source_config)
        self._edge_fill_mode = self._edge_fill_mode(source_config)

        mode = source_mode(source_config)
        if mode == SOURCE_MODE_TEMPLATE:
            self._payload_provider: BasePayloadProvider = TemplatePayloadProvider(
                hass, source_name, source_config
            )
        elif mode == SOURCE_MODE_ENTITY_ADAPTER:
            self._payload_provider = EntityAdapterPayloadProvider(
                hass, source_name, source_config
            )
        elif mode == SOURCE_MODE_SERVICE_ADAPTER:
            self._payload_provider = ServiceResponsePayloadProvider(
                hass, source_name, source_config
            )
        elif mode == SOURCE_MODE_ENERGY_PROVIDER:
            self._payload_provider = EnergySolarForecastPayloadProvider(
                hass, source_name, source_config
            )
        else:
            raise SourceProviderError(
                "source_validation",
                f"{source_name} source mode `{mode}` is not supported",
                details={"source": source_name, "mode": mode},
            )

    async def async_values(self, window: SourceWindow) -> list[float]:
        """Return exactly `window.slots` values or raise error."""
        points = await self.async_points(window)
        return self._points_to_values(points, window)

    async def async_fetch_payload(self) -> Any:
        """Return raw payload before fixup for review and debug paths."""

        return await self._payload_provider.async_fetch_payload()

    async def async_points(self, window: SourceWindow) -> list[dict[str, Any]]:
        """Return point objects for this provider."""
        payload = await self.async_fetch_payload()
        return self._payload_to_points(payload, window, strict=True)

    def _payload_to_points(
        self,
        payload: Any,
        window: SourceWindow,
        *,
        strict: bool,
    ) -> list[dict[str, Any]]:
        """Convert one provider payload into timestamp/value points."""
        if not isinstance(payload, list):
            raise SourceProviderError(
                "source_parse",
                f"{self._source_name} source did not render to a list",
                details={
                    "source": self._source_name,
                    "payload_type": type(payload).__name__,
                },
            )

        if not payload:
            return []

        if isinstance(payload[0], dict):
            return self._object_payload_to_points(payload, strict=strict)
        return self._numeric_payload_to_points(payload, window)

    def _object_payload_to_points(
        self,
        payload: list[Any],
        *,
        strict: bool,
    ) -> list[dict[str, Any]]:
        """Convert an object payload into point objects."""
        time_key = str(self._source_config.get(CONF_TIME_KEY, "start"))
        value_key = str(self._source_config.get(CONF_VALUE_KEY, "value"))
        points: list[dict[str, Any]] = []
        for index, point in enumerate(payload):
            if not isinstance(point, dict):
                if strict:
                    raise SourceProviderError(
                        "source_parse",
                        f"{self._source_name} point {index + 1} is not an object",
                        details={"source": self._source_name, "index": index},
                    )
                continue

            start_value = point.get(time_key)
            numeric_value = point.get(value_key)
            if not isinstance(start_value, str):
                if strict:
                    raise SourceProviderError(
                        "source_parse",
                        f"{self._source_name} point {index + 1} missing `{time_key}`",
                        details={"source": self._source_name, "index": index, "key": time_key},
                    )
                continue

            try:
                start_dt = datetime.fromisoformat(start_value)
                value = float(numeric_value)
            except (TypeError, ValueError) as err:
                if strict:
                    field_name = time_key if not isinstance(start_value, str) else value_key
                    raise SourceProviderError(
                        "source_parse",
                        (
                            f"{self._source_name} point {index + 1} has invalid "
                            f"`{field_name}` value"
                        ),
                        details={"source": self._source_name, "index": index},
                    ) from err
                continue

            points.append(
                {
                    "start": self._as_utc(start_dt).isoformat(),
                    "value": value,
                }
            )
        return points

    def _numeric_payload_to_points(
        self,
        payload: list[Any],
        window: SourceWindow,
    ) -> list[dict[str, Any]]:
        """Convert one numeric payload into timestamp/value points."""
        points: list[dict[str, Any]] = []
        start_at = self._as_utc(window.start_at)
        values_per_slot = 1
        if len(payload) > window.slots:
            if len(payload) % window.slots != 0:
                raise SourceProviderError(
                    "source_validation",
                    (
                        f"{self._source_name} source returned {len(payload)} values, "
                        f"which cannot be evenly aggregated into {window.slots} slots"
                    ),
                    details={
                        "source": self._source_name,
                        "available_count": len(payload),
                        "required_count": window.slots,
                    },
                )
            values_per_slot = len(payload) // window.slots
        for index, value in enumerate(payload):
            try:
                numeric = float(value)
            except (TypeError, ValueError) as err:
                raise SourceProviderError(
                    "source_parse",
                    (
                        f"{self._source_name} point {index + 1} has invalid "
                        f"numeric value `{value}`"
                    ),
                    details={"source": self._source_name, "index": index, "value": value},
                ) from err
            points.append(
                {
                    "start": (
                        start_at + timedelta(minutes=window.slot_minutes * (index // values_per_slot))
                    ).isoformat(),
                    "value": numeric,
                }
            )
        return points

    def _points_to_values(
        self,
        payload: list[dict[str, Any]],
        window: SourceWindow,
    ) -> list[float]:
        """Resolve point payload into one value per requested slot."""
        return self._object_values(payload, window, time_key="start", value_key="value")

    def _object_values(
        self,
        payload: list[Any],
        window: SourceWindow,
        *,
        time_key: str | None = None,
        value_key: str | None = None,
    ) -> list[float]:
        """Resolve object payload into one value per requested slot."""
        if time_key is None:
            time_key = str(self._source_config.get(CONF_TIME_KEY, "start"))
        if value_key is None:
            value_key = str(self._source_config.get(CONF_VALUE_KEY, "value"))

        points: list[tuple[datetime, float]] = []
        for index, point in enumerate(payload):
            if not isinstance(point, dict):
                raise SourceProviderError(
                    "source_parse",
                    f"{self._source_name} point {index + 1} is not an object",
                    details={"source": self._source_name, "index": index},
                )

            start_value = point.get(time_key)
            numeric_value = point.get(value_key)
            if not isinstance(start_value, str):
                raise SourceProviderError(
                    "source_parse",
                    f"{self._source_name} point {index + 1} missing `{time_key}`",
                    details={"source": self._source_name, "index": index, "key": time_key},
                )

            try:
                start_dt = datetime.fromisoformat(start_value)
            except ValueError as err:
                raise SourceProviderError(
                    "source_parse",
                    (
                        f"{self._source_name} point {index + 1} has invalid "
                        f"timestamp `{start_value}`"
                    ),
                    details={
                        "source": self._source_name,
                        "index": index,
                        "value": start_value,
                    },
                ) from err

            try:
                value = float(numeric_value)
            except (TypeError, ValueError) as err:
                raise SourceProviderError(
                    "source_parse",
                    (
                        f"{self._source_name} point {index + 1} has invalid "
                        f"numeric value `{numeric_value}`"
                    ),
                    details={
                        "source": self._source_name,
                        "index": index,
                        "value": numeric_value,
                    },
                ) from err

            points.append((self._as_utc(start_dt), value))

        slot_delta = timedelta(minutes=window.slot_minutes)
        start_at = self._as_utc(window.start_at)
        end_at = start_at + (slot_delta * window.slots)

        buckets: dict[int, list[float]] = {}
        for point_start, value in points:
            if self._clamp_mode == CLAMP_MODE_NEAREST:
                slot_index = self._nearest_slot_index(point_start, start_at, slot_delta)
                if slot_index < 0 or slot_index >= window.slots:
                    continue
            else:
                if point_start < start_at or point_start >= end_at:
                    continue
                offset = point_start - start_at
                if (offset.total_seconds() % slot_delta.total_seconds()) != 0:
                    raise SourceProviderError(
                        "source_validation",
                        (
                            f"{self._source_name} timestamp `{point_start.isoformat()}` "
                            "is not aligned to slot boundaries"
                        ),
                        details={"source": self._source_name},
                    )
                slot_index = int(offset // slot_delta)
            buckets.setdefault(slot_index, []).append(value)
        known: list[float | None] = [None] * window.slots
        for slot_index, slot_values in buckets.items():
            known[slot_index] = self._aggregate_values(slot_values)
        return self._complete_slots(known)

    def _numeric_values(self, payload: list[Any], window: SourceWindow) -> list[float]:
        """Resolve numeric payload into one value per requested slot."""
        values: list[float] = []
        for index, value in enumerate(payload):
            try:
                numeric = float(value)
            except (TypeError, ValueError) as err:
                raise SourceProviderError(
                    "source_parse",
                    (
                        f"{self._source_name} point {index + 1} has invalid "
                        f"numeric value `{value}`"
                    ),
                    details={"source": self._source_name, "index": index, "value": value},
                ) from err
            values.append(numeric)

        if len(values) == window.slots:
            return values

        if len(values) > window.slots:
            if len(values) % window.slots != 0:
                raise SourceProviderError(
                    "source_validation",
                    (
                        f"{self._source_name} source returned {len(values)} values, "
                        f"which cannot be evenly aggregated into {window.slots} slots"
                    ),
                    details={
                        "source": self._source_name,
                        "available_count": len(values),
                        "required_count": window.slots,
                    },
                )
            values_per_slot = len(values) // window.slots
            aggregated: list[float] = []
            for slot_index in range(window.slots):
                start = slot_index * values_per_slot
                end = start + values_per_slot
                aggregated.append(self._aggregate_values(values[start:end]))
            return aggregated
        known: list[float | None] = [None] * window.slots
        for index, value in enumerate(values):
            known[index] = value
        return self._complete_slots(known)

    def _aggregation_mode(self, source_config: dict[str, Any]) -> str:
        """Return validated source aggregation mode."""
        mode = str(source_config.get(CONF_AGGREGATION_MODE, AGGREGATION_MODE_MEAN))
        if mode not in VALID_AGGREGATION_MODES:
            raise SourceProviderError(
                "source_validation",
                f"{self._source_name} aggregation mode `{mode}` is not supported",
                details={"source": self._source_name, "aggregation_mode": mode},
            )
        return mode

    def _aggregate_values(self, values: list[float]) -> float:
        """Aggregate values with configured mode."""
        if self._aggregation_mode == AGGREGATION_MODE_FIRST:
            return values[0]
        if self._aggregation_mode == AGGREGATION_MODE_LAST:
            return values[-1]
        if self._aggregation_mode == AGGREGATION_MODE_MIN:
            return min(values)
        if self._aggregation_mode == AGGREGATION_MODE_MAX:
            return max(values)
        return float(sum(values) / len(values))

    def _complete_slots(self, known: list[float | None]) -> list[float]:
        """Complete missing slots using configured resample and edge modes."""
        completed = list(known)
        slot_count = len(completed)

        if self._resample_mode == RESAMPLE_MODE_FORWARD_FILL:
            last_value: float | None = None
            for index, value in enumerate(completed):
                if value is not None:
                    last_value = value
                    continue
                if last_value is not None:
                    completed[index] = last_value

        elif self._resample_mode == RESAMPLE_MODE_LINEAR:
            known_indices = [idx for idx, value in enumerate(completed) if value is not None]
            for left_idx, right_idx in pairwise(known_indices):
                if right_idx - left_idx <= 1:
                    continue
                left_value = completed[left_idx]
                right_value = completed[right_idx]
                if left_value is None or right_value is None:
                    continue
                gap = right_idx - left_idx
                slope = (right_value - left_value) / gap
                for fill_idx in range(left_idx + 1, right_idx):
                    completed[fill_idx] = left_value + (slope * (fill_idx - left_idx))

        if self._edge_fill_mode == EDGE_FILL_MODE_HOLD and slot_count > 0:
            first_index = next(
                (idx for idx, value in enumerate(completed) if value is not None), None
            )
            last_index = next(
                (idx for idx in range(slot_count - 1, -1, -1) if completed[idx] is not None),
                None,
            )
            if first_index is not None:
                first_value = completed[first_index]
                for idx in range(first_index):
                    completed[idx] = first_value
            if last_index is not None:
                last_value = completed[last_index]
                for idx in range(last_index + 1, slot_count):
                    completed[idx] = last_value

        missing = sum(1 for value in completed if value is None)
        if missing:
            raise SourceProviderError(
                "source_validation",
                (
                    f"{self._source_name} source resolved to {slot_count - missing} slots, "
                    f"but {slot_count} are required"
                ),
                details={
                    "source": self._source_name,
                    "available_count": slot_count - missing,
                    "required_count": slot_count,
                },
            )

        return [float(value) for value in completed if value is not None]

    def _as_utc(self, value: datetime) -> datetime:
        """Normalize datetime to UTC, assuming UTC for naive values."""
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def _nearest_slot_index(
        self, point_start: datetime, start_at: datetime, slot_delta: timedelta
    ) -> int:
        """Return nearest slot index for a timestamp."""
        offset_seconds = (point_start - start_at).total_seconds()
        slot_seconds = slot_delta.total_seconds()
        return int((offset_seconds / slot_seconds) + 0.5)

    def _clamp_mode(self, source_config: dict[str, Any]) -> str:
        """Return validated clamp mode."""
        mode = str(source_config.get(CONF_CLAMP_MODE, CLAMP_MODE_NONE))
        if mode not in VALID_CLAMP_MODES:
            raise SourceProviderError(
                "source_validation",
                f"{self._source_name} clamp mode `{mode}` is not supported",
                details={"source": self._source_name, "clamp_mode": mode},
            )
        return mode

    def _resample_mode(self, source_config: dict[str, Any]) -> str:
        """Return validated resample mode."""
        mode = str(source_config.get(CONF_RESAMPLE_MODE, RESAMPLE_MODE_NONE))
        if mode not in VALID_RESAMPLE_MODES:
            raise SourceProviderError(
                "source_validation",
                f"{self._source_name} resample mode `{mode}` is not supported",
                details={"source": self._source_name, "resample_mode": mode},
            )
        return mode

    def _edge_fill_mode(self, source_config: dict[str, Any]) -> str:
        """Return validated edge fill mode."""
        mode = str(source_config.get(CONF_EDGE_FILL_MODE, EDGE_FILL_MODE_NONE))
        if mode not in VALID_EDGE_FILL_MODES:
            raise SourceProviderError(
                "source_validation",
                f"{self._source_name} edge fill mode `{mode}` is not supported",
                details={"source": self._source_name, "edge_fill_mode": mode},
            )
        return mode


class MergedSourceProvider(TemplateAdapterSourceProvider):
    """Merge multiple providers into one shared normalization path."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        source_name: str,
        source_config: dict[str, Any],
        validate_built_in_entity,
        allow_partial_failures: bool,
    ) -> None:
        """Initialize the merged provider."""
        super().__init__(
            hass,
            source_name=source_name,
            source_config={**source_config, CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE},
        )
        self._hass = hass
        self._providers = source_providers(source_config)
        self._validate_built_in_entity = validate_built_in_entity
        self._allow_partial_failures = allow_partial_failures

    async def async_fetch_payload(self) -> Any:
        """Return one merged list from all wrapped providers."""
        return await self._async_collect_points(
            SourceWindow(start_at=datetime.now(tz=UTC), slot_minutes=60, slots=1),
            strict=True,
        )

    async def async_points(self, window: SourceWindow) -> list[dict[str, Any]]:
        """Return merged point objects from all providers."""
        return await self._async_collect_points(window, strict=not self._allow_partial_failures)

    async def async_values(self, window: SourceWindow) -> list[float]:
        """Return normalized values from all merged providers."""
        points = await self.async_points(window)
        return self._points_to_values(points, window)

    async def _async_collect_points(
        self,
        window: SourceWindow,
        *,
        strict: bool,
    ) -> list[dict[str, Any]]:
        """Collect point payloads from all configured providers."""
        merged: list[dict[str, Any]] = []
        failures: list[SourceProviderError] = []
        for provider_config in self._providers:
            try:
                points = await self._async_provider_points(
                    provider_config,
                    window,
                    strict=strict,
                )
            except SourceProviderError as err:
                failures.append(err)
                if strict:
                    raise
                self._log_provider_failure(provider_config, err)
                continue

            if not points:
                if strict:
                    raise SourceProviderError(
                        "source_validation",
                        f"{self._source_name} provider returned no usable points",
                        details={"source": self._source_name},
                    )
                self._log_provider_empty(provider_config)
                continue

            merged.extend(points)

        if merged:
            return merged
        if failures:
            raise failures[0]
        raise SourceProviderError(
            "source_validation",
            f"{self._source_name} source returned no usable points",
            details={"source": self._source_name, "available_count": 0},
        )

    async def _async_provider_points(
        self,
        provider_config: dict[str, Any],
        window: SourceWindow,
        *,
        strict: bool,
    ) -> list[dict[str, Any]]:
        """Return points for one provider config."""
        mode = source_mode(provider_config)
        provider_source_config = {**self._source_config, **provider_config}
        if mode == SOURCE_MODE_BUILT_IN:
            entity_id = str(provider_config[CONF_WATTPLAN_ENTITY_ID])
            if self._validate_built_in_entity is not None:
                self._validate_built_in_entity(entity_id)
            provider = ForecastProvider(
                self._hass,
                entity_id=entity_id,
                lookback_days=int(provider_config.get(CONF_HISTORY_DAYS, 14)),
            )
            values = await provider.async_values(window)
            slot_delta = timedelta(minutes=window.slot_minutes)
            start_at = self._as_utc(window.start_at)
            return [
                {
                    "start": (start_at + (slot_delta * index)).isoformat(),
                    "value": value,
                }
                for index, value in enumerate(values)
            ]

        if mode == SOURCE_MODE_ENERGY_PROVIDER:
            provider = EnergySolarForecastSourceProvider(
                self._hass,
                source_name=self._source_name,
                source_config=provider_source_config,
            )
            payload = await provider.async_fetch_payload()
            return provider._payload_to_points(payload, window, strict=strict)

        provider = TemplateAdapterSourceProvider(
            self._hass,
            source_name=self._source_name,
            source_config=provider_source_config,
        )
        payload = await provider.async_fetch_payload()
        return provider._payload_to_points(payload, window, strict=strict)

    def _log_provider_empty(self, provider_config: dict[str, Any]) -> None:
        """Log when one provider contributes no usable points."""
        _LOGGER.warning(
            "%s provider `%s` produced 0 usable points",
            self._source_name,
            self._provider_label(provider_config),
        )

    def _log_provider_failure(
        self,
        provider_config: dict[str, Any],
        err: SourceProviderError,
    ) -> None:
        """Log when one provider fails but another can still cover the source."""
        _LOGGER.warning(
            "%s provider `%s` failed during merged source resolution: %s",
            self._source_name,
            self._provider_label(provider_config),
            err,
        )

    def _provider_label(self, provider_config: dict[str, Any]) -> str:
        """Return one compact provider identifier."""
        mode = source_mode(provider_config)
        if mode == SOURCE_MODE_ENTITY_ADAPTER:
            return str(provider_config.get(CONF_WATTPLAN_ENTITY_ID, "entity"))
        if mode == SOURCE_MODE_SERVICE_ADAPTER:
            return str(provider_config.get(CONF_SERVICE, "service"))
        if mode == SOURCE_MODE_BUILT_IN:
            return str(provider_config.get(CONF_WATTPLAN_ENTITY_ID, "built_in"))
        if mode == SOURCE_MODE_ENERGY_PROVIDER:
            return str(provider_config.get(CONF_CONFIG_ENTRY_ID, "energy_provider"))
        return mode or "provider"


class EnergySolarForecastSourceProvider(TemplateAdapterSourceProvider):
    """Source provider that extends Energy solar forecasts across the horizon."""

    async def async_values(self, window: SourceWindow) -> list[float]:
        """Return normalized Energy forecast values for the whole horizon."""
        payload = await self._payload_provider.async_fetch_payload()
        if not isinstance(payload, list) or not payload:
            raise SourceProviderError(
                "source_fetch",
                f"{self._source_name} Energy provider returned no solar forecast",
                details={
                    "source": self._source_name,
                    "provider_reason": "no_forecast",
                },
            )

        base_slots = self._energy_payload_slots(payload, window)
        base_window = SourceWindow(
            start_at=window.start_at,
            slot_minutes=window.slot_minutes,
            slots=max(1, min(window.slots, base_slots)),
        )
        values = self._object_values(payload, base_window)
        if len(values) >= window.slots:
            return values[: window.slots]

        day_slots = int((24 * 60) / window.slot_minutes)
        if day_slots <= 0 or len(values) < day_slots:
            raise SourceProviderError(
                "source_validation",
                (
                    f"{self._source_name} Energy provider has {len(values)} usable "
                    f"values, but at least {day_slots} are required before the "
                    "daily repeat model can extend the horizon"
                ),
                details={
                    "source": self._source_name,
                    "available_count": len(values),
                    "required_count": day_slots,
                    "provider_reason": "not_enough_history",
                },
            )

        completed = list(values)
        while len(completed) < window.slots:
            repeat_index = len(completed) - day_slots
            completed.append(completed[repeat_index])
        return completed[: window.slots]

    def _energy_payload_slots(
        self, payload: list[dict[str, Any]], window: SourceWindow
    ) -> int:
        """Return the known slot count covered by the Energy payload."""
        start_at = self._as_utc(window.start_at)
        slot_delta = timedelta(minutes=window.slot_minutes)
        max_slot = 0
        for point in payload:
            stamp = point.get("start")
            if not isinstance(stamp, str):
                continue
            try:
                point_start = self._as_utc(datetime.fromisoformat(stamp))
            except ValueError:
                continue

            if self._clamp_mode == CLAMP_MODE_NEAREST:
                slot_index = self._nearest_slot_index(point_start, start_at, slot_delta)
            else:
                offset = point_start - start_at
                if offset.total_seconds() < 0:
                    continue
                slot_index = int(offset // slot_delta)
            if slot_index < 0:
                continue
            max_slot = max(max_slot, slot_index + 1)
        return max_slot
