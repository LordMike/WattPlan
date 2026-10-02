"""Raw payload providers for source acquisition."""

from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
from datetime import UTC, datetime
from itertools import pairwise
import math
from typing import Any

from homeassistant.const import CONF_NAME
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.template import Template
import voluptuous as vol

from ..adapter_auto import resolve_nested_value
from ..datetime_utils import parse_datetime_like, typical_step
from ..const import (
    ADAPTER_TYPE_ATTRIBUTE_OBJECTS,
    ADAPTER_TYPE_ATTRIBUTE_VALUES,
    ADAPTER_TYPE_SERVICE_RESPONSE,
    CONF_ADAPTER_TYPE,
    CONF_CONFIG_ENTRY_ID,
    CONF_SERVICE,
    CONF_TEMPLATE,
)
from ..source_types import SourceProviderError
from .config import CONF_WATTPLAN_ENTITY_ID
from .discovery import async_get_energy_solar_forecast_platforms

SERVICE_CALL_TIMEOUT_SECONDS = 30


def split_service_name(service_name: str, *, label: str) -> tuple[str, str]:
    """Return validated domain and service parts for a service adapter."""
    domain, separator, service = str(service_name).strip().partition(".")
    if not separator or not domain.strip() or not service.strip():
        raise SourceProviderError(
            "source_validation",
            f"{label} service `{service_name}` is invalid",
            details={"service": service_name},
        )
    return domain.strip(), service.strip()


async def async_service_response(
    hass: HomeAssistant, service_name: str, *, label: str = "Service"
) -> Any:
    """Call a no-argument service and return its response payload.

    Every failure of the call itself is reported as a `source_fetch`
    `SourceProviderError` so callers get stale-cache fallback and health
    reporting instead of an unhandled Home Assistant exception.
    """
    domain, service = split_service_name(service_name, label=label)
    try:
        async with asyncio.timeout(SERVICE_CALL_TIMEOUT_SECONDS):
            return await hass.services.async_call(
                domain,
                service,
                {},
                blocking=True,
                return_response=True,
            )
    except (HomeAssistantError, vol.Invalid, TimeoutError) as err:
        raise SourceProviderError(
            "source_fetch",
            f"{label} service `{service_name}` failed: {err or type(err).__name__}",
            details={"service": service_name},
        ) from err


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
        from .discovery import _decoded_state_root

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

        root = _decoded_state_root(state)
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

        response = await async_service_response(
            self._hass, str(service_name), label=self._source_name
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

        rows: list[tuple[datetime, float]] = []
        for timestamp, value in sorted(wh_hours.items()):
            try:
                start_dt = parse_datetime_like(timestamp)
                if start_dt is None:
                    raise ValueError
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
            if not math.isfinite(numeric_value):
                raise SourceProviderError(
                    "source_parse",
                    (
                        f"{self._source_name} Energy provider returned non-finite "
                        f"numeric value `{value}`"
                    ),
                    details={
                        "source": self._source_name,
                        "config_entry_id": entry.entry_id,
                        "provider_reason": "nonfinite_value",
                    },
                )
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=UTC)
            rows.append((start_dt, numeric_value))

        return [
            {
                "start": start_dt.isoformat(),
                "value": numeric_value,
            }
            for start_dt, numeric_value in _zero_fill_holes(sorted(rows))
        ]


def _zero_fill_holes(rows: list[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    """Fill holes between forecast periods with zero energy.

    Energy solar forecasts may omit periods without production (for example
    the night between sunset and sunrise). Inside the forecast range a missing
    period means no energy, not unknown data, so it must not become a gap.
    """
    step = typical_step([start_dt for start_dt, _value in rows])
    if step is None:
        return rows
    filled: list[tuple[datetime, float]] = []
    for (start_dt, value), (next_start, _next_value) in pairwise(rows):
        filled.append((start_dt, value))
        hole_start = start_dt + step
        while hole_start < next_start:
            filled.append((hole_start, 0.0))
            hole_start += step
    filled.append(rows[-1])
    return filled


__all__ = [
    "BasePayloadProvider",
    "CONF_WATTPLAN_ENTITY_ID",
    "EnergySolarForecastPayloadProvider",
    "EntityAdapterPayloadProvider",
    "ServiceResponsePayloadProvider",
    "TemplatePayloadProvider",
    "async_service_response",
]
