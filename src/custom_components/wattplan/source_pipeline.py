"""Shared source pipeline assembly for WattPlan."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant

from .const import (
    CONF_FIXUP_PROFILE,
    CONF_HISTORY_DAYS,
    CONF_PROVIDERS,
    CONF_SOURCE_MODE,
    SOURCE_MODE_ENTITY_ADAPTER,
    FIXUP_PROFILE_REPAIR,
    FIXUP_PROFILE_STRICT,
    SOURCE_MODE_BUILT_IN,
    SOURCE_MODE_ENERGY_PROVIDER,
)
from .forecast_provider import ForecastProvider
from .source_fixup import SourceFixupProvider, effective_provider_config
from .source_provider import (
    EnergySolarForecastSourceProvider,
    MergedSourceProvider,
    TemplateAdapterSourceProvider,
    source_providers,
)
from .source_types import SourceProvider

type ValidateBuiltInEntity = Callable[[str], None] | None


def build_source_base_provider(
    hass: HomeAssistant,
    *,
    source_key: str,
    source_config: dict[str, Any],
    validate_built_in_entity: ValidateBuiltInEntity = None,
    allow_partial_failures: bool = False,
) -> SourceProvider:
    """Return the raw provider for one configured source.

    This keeps flow review, runtime planning, and exports on the same source
    selection path so provider composition cannot drift across entry points.
    """

    mode = source_config.get(CONF_SOURCE_MODE)
    providers_config = source_providers(source_config)

    if len(providers_config) == 1 and mode == SOURCE_MODE_BUILT_IN:
        provider_config = providers_config[0]
        entity_id = str(provider_config["entity_id"])
        if validate_built_in_entity is not None:
            validate_built_in_entity(entity_id)
        return ForecastProvider(
            hass,
            entity_id=entity_id,
            lookback_days=int(provider_config.get(CONF_HISTORY_DAYS, 14)),
        )
    if len(providers_config) == 1 and mode == SOURCE_MODE_ENERGY_PROVIDER:
        effective_config = effective_provider_config(
            {**source_config, **providers_config[0]}
        )
        return EnergySolarForecastSourceProvider(
            hass,
            source_name=source_key,
            source_config=effective_config,
        )
    if len(providers_config) == 1 and mode != SOURCE_MODE_BUILT_IN:
        effective_config = effective_provider_config(
            {**source_config, **providers_config[0]}
        )
        return TemplateAdapterSourceProvider(
            hass,
            source_name=source_key,
            source_config=effective_config,
        )

    return MergedSourceProvider(
        hass,
        source_name=source_key,
        source_config=source_config,
        validate_built_in_entity=validate_built_in_entity,
        allow_partial_failures=allow_partial_failures,
    )


def build_source_value_provider(
    hass: HomeAssistant,
    *,
    source_key: str,
    source_config: dict[str, Any],
    validate_built_in_entity: ValidateBuiltInEntity = None,
) -> SourceProvider:
    """Return the runtime value pipeline for one configured source.

    Every source goes through the shared fixup wrapper so planner review and
    runtime apply the same repair and stale-reuse behavior.
    """

    return SourceFixupProvider(
        build_source_base_provider(
            hass,
            source_key=source_key,
            source_config=source_config,
            validate_built_in_entity=validate_built_in_entity,
            allow_partial_failures=True,
        ),
        profile=str(source_config.get(CONF_FIXUP_PROFILE, FIXUP_PROFILE_REPAIR)),
    )


async def async_fetch_source_payload(
    hass: HomeAssistant,
    *,
    source_key: str,
    source_config: dict[str, Any],
) -> Any:
    """Return raw payload for review using the same base provider selection."""

    provider = build_source_base_provider(
        hass,
        source_key=source_key,
        source_config={**source_config, CONF_FIXUP_PROFILE: FIXUP_PROFILE_STRICT},
    )
    if isinstance(provider, TemplateAdapterSourceProvider):
        return await provider.async_fetch_payload()
    raise TypeError("Only template-style source providers expose raw payload")
