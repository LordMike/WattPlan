"""Raw payload providers for source acquisition."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from homeassistant.const import CONF_NAME
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.template import Template

from ..adapter_auto import resolve_nested_value
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


def split_service_name(service_name: str, *, label: str) -> tuple[str, str]:
    """Return validated domain and service parts for a service adapter."""
    try:
        return service_name.split(".", 1)
    except ValueError as err:
        raise SourceProviderError(
            "source_validation",
            f"{label} service `{service_name}` is invalid",
            details={"service": service_name},
        ) from err


async def async_service_response(hass: HomeAssistant, service_name: str) -> Any:
    """Call a no-argument service and return its response payload."""
    domain, service = split_service_name(service_name, label="Service")
    return await hass.services.async_call(
        domain,
        service,
        {},
        blocking=True,
        return_response=True,
    )


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

        try:
            response = await async_service_response(self._hass, str(service_name))
        except SourceProviderError as err:
            raise SourceProviderError(
                "source_validation",
                f"{self._source_name} service `{service_name}` is invalid",
                details={"source": self._source_name, "service": str(service_name)},
            ) from err
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


__all__ = [
    "BasePayloadProvider",
    "CONF_WATTPLAN_ENTITY_ID",
    "EnergySolarForecastPayloadProvider",
    "EntityAdapterPayloadProvider",
    "ServiceResponsePayloadProvider",
    "TemplatePayloadProvider",
    "async_service_response",
]
