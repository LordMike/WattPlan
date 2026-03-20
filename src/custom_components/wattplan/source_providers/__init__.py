"""Internal source provider modules."""

from .config import CONF_WATTPLAN_ENTITY_ID, primary_provider_config, source_mode, source_providers
from .discovery import (
    async_auto_detect_entity_adapter,
    async_auto_detect_service_adapter,
    async_get_energy_solar_forecast_entries,
    async_get_energy_solar_forecast_platforms,
)
from .providers import MergedSourceProvider, TemplateAdapterSourceProvider

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
