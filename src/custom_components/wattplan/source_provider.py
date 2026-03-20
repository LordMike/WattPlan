"""Compatibility facade for source provider modules."""

from .source_providers import (
    CONF_WATTPLAN_ENTITY_ID,
    MergedSourceProvider,
    TemplateAdapterSourceProvider,
    async_auto_detect_entity_adapter,
    async_auto_detect_service_adapter,
    async_get_energy_solar_forecast_entries,
    async_get_energy_solar_forecast_platforms,
    primary_provider_config,
    source_mode,
    source_providers,
)

__all__ = [
    "CONF_WATTPLAN_ENTITY_ID",
    "MergedSourceProvider",
    "TemplateAdapterSourceProvider",
    "async_auto_detect_entity_adapter",
    "async_auto_detect_service_adapter",
    "async_get_energy_solar_forecast_entries",
    "async_get_energy_solar_forecast_platforms",
    "primary_provider_config",
    "source_mode",
    "source_providers",
]
