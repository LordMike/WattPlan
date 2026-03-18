"""Canonical source provider and normalization helpers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant

from ..const import (
    ADAPTER_TYPE_ATTRIBUTE_OBJECTS,
    ADAPTER_TYPE_AUTO_DETECT,
    ADAPTER_TYPE_SERVICE_RESPONSE,
    CONF_ADAPTER_TYPE,
    CONF_CONFIG_ENTRY_ID,
    CONF_FIXUP_PROFILE,
    CONF_HISTORY_DAYS,
    CONF_PROVIDERS,
    CONF_SERVICE,
    CONF_SOURCE_MODE,
    CONF_TEMPLATE,
    CONF_TIME_KEY,
    CONF_VALUE_KEY,
    FIXUP_PROFILE_REPAIR,
    SOURCE_MODE_BUILT_IN,
    SOURCE_MODE_ENERGY_PROVIDER,
    SOURCE_MODE_ENTITY_ADAPTER,
    SOURCE_MODE_SERVICE_ADAPTER,
    SOURCE_MODE_TEMPLATE,
)
from ..forecast_provider import ForecastProvider
from ..source_fixup import SourceFixupProvider, effective_provider_config
from ..source_providers import (
    CONF_WATTPLAN_ENTITY_ID,
    EnergySolarForecastSourceProvider,
    MergedSourceProvider,
    TemplateAdapterSourceProvider,
    async_auto_detect_entity_adapter,
    async_auto_detect_service_adapter,
    primary_provider_config,
    source_mode,
    source_providers,
)
from ..source_types import SourceProvider
from .models import SourceConfigModel, SourceProviderModel

type ValidateBuiltInEntity = Callable[[str], None] | None


def normalize_source_config(source_config: dict[str, Any]) -> SourceConfigModel:
    """Return canonical internal source config view."""
    providers = tuple(SourceProviderModel(dict(provider)) for provider in source_providers(source_config))
    return SourceConfigModel(
        data=dict(source_config),
        providers=providers,
        mode=source_mode(source_config),
    )


def build_source_base_provider(
    hass: HomeAssistant,
    *,
    source_key: str,
    source_config: dict[str, Any],
    validate_built_in_entity: ValidateBuiltInEntity = None,
    allow_partial_failures: bool = False,
) -> SourceProvider:
    """Return the raw provider for one configured source."""
    normalized = normalize_source_config(source_config)
    providers_config = [provider.data for provider in normalized.providers]

    if len(providers_config) == 1 and normalized.mode == SOURCE_MODE_BUILT_IN:
        provider_config = providers_config[0]
        entity_id = str(provider_config[CONF_WATTPLAN_ENTITY_ID])
        if validate_built_in_entity is not None:
            validate_built_in_entity(entity_id)
        return ForecastProvider(
            hass,
            entity_id=entity_id,
            lookback_days=int(provider_config.get(CONF_HISTORY_DAYS, 14)),
        )
    if len(providers_config) == 1 and normalized.mode == SOURCE_MODE_ENERGY_PROVIDER:
        return EnergySolarForecastSourceProvider(
            hass,
            source_name=source_key,
            source_config=effective_provider_config({**source_config, **providers_config[0]}),
        )
    if len(providers_config) == 1 and normalized.mode != SOURCE_MODE_BUILT_IN:
        return TemplateAdapterSourceProvider(
            hass,
            source_name=source_key,
            source_config=effective_provider_config({**source_config, **providers_config[0]}),
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
    """Return the runtime value pipeline for one configured source."""
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


async def async_prepare_entity_source_input(
    hass: HomeAssistant, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Return explicit entity adapter config, resolving auto-detect if needed."""
    selected_entities = user_input[CONF_WATTPLAN_ENTITY_ID]
    entity_ids = [selected_entities] if isinstance(selected_entities, str) else [str(entity_id) for entity_id in selected_entities]
    adapter_type = str(user_input[CONF_ADAPTER_TYPE])
    manual = user_input.get("manual", {})
    if not isinstance(manual, dict):
        manual = {}
    root_key = str(manual.get(CONF_NAME, user_input.get(CONF_NAME, "")))
    time_key = str(manual.get(CONF_TIME_KEY, user_input.get(CONF_TIME_KEY, "")))
    value_key = str(manual.get(CONF_VALUE_KEY, user_input.get(CONF_VALUE_KEY, "")))

    if adapter_type == ADAPTER_TYPE_AUTO_DETECT:
        detected_list = await async_auto_detect_entity_adapter(hass, entity_ids)
        providers = [
            {
                CONF_SOURCE_MODE: SOURCE_MODE_ENTITY_ADAPTER,
                CONF_WATTPLAN_ENTITY_ID: entity_id,
                CONF_ADAPTER_TYPE: ADAPTER_TYPE_ATTRIBUTE_OBJECTS,
                CONF_NAME: detected.root_key,
                CONF_TIME_KEY: detected.time_key,
                CONF_VALUE_KEY: detected.value_key,
            }
            for entity_id, detected in zip(entity_ids, detected_list, strict=True)
        ]
    else:
        providers = [
            {
                CONF_SOURCE_MODE: SOURCE_MODE_ENTITY_ADAPTER,
                CONF_WATTPLAN_ENTITY_ID: entity_id,
                CONF_ADAPTER_TYPE: adapter_type,
                CONF_NAME: root_key,
                CONF_TIME_KEY: time_key,
                CONF_VALUE_KEY: value_key,
            }
            for entity_id in entity_ids
        ]

    source = {
        CONF_SOURCE_MODE: SOURCE_MODE_ENTITY_ADAPTER,
        CONF_PROVIDERS: providers,
        CONF_FIXUP_PROFILE: user_input[CONF_FIXUP_PROFILE],
    }
    source.update(user_input.get("advanced", {}))
    return source


async def async_prepare_service_source_input(
    hass: HomeAssistant, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Return explicit service adapter config, resolving auto-detect if needed."""
    adapter_type = str(user_input[CONF_ADAPTER_TYPE])
    manual = user_input.get("manual", {})
    if not isinstance(manual, dict):
        manual = {}
    root_key = str(manual.get(CONF_NAME, user_input.get(CONF_NAME, "")))
    time_key = str(manual.get(CONF_TIME_KEY, user_input.get(CONF_TIME_KEY, "")))
    value_key = str(manual.get(CONF_VALUE_KEY, user_input.get(CONF_VALUE_KEY, "")))
    service_name = str(user_input[CONF_SERVICE])
    resolved_adapter = adapter_type

    if adapter_type == ADAPTER_TYPE_AUTO_DETECT:
        detected = await async_auto_detect_service_adapter(hass, service_name)
        resolved_adapter = ADAPTER_TYPE_SERVICE_RESPONSE
        root_key = detected.root_key
        time_key = detected.time_key
        value_key = detected.value_key

    source = {
        CONF_SOURCE_MODE: SOURCE_MODE_SERVICE_ADAPTER,
        CONF_PROVIDERS: [
            {
                CONF_SOURCE_MODE: SOURCE_MODE_SERVICE_ADAPTER,
                CONF_SERVICE: service_name,
                CONF_ADAPTER_TYPE: resolved_adapter,
                CONF_NAME: root_key,
                CONF_TIME_KEY: time_key,
                CONF_VALUE_KEY: value_key,
            }
        ],
        CONF_FIXUP_PROFILE: user_input[CONF_FIXUP_PROFILE],
    }
    source.update(user_input.get("advanced", {}))
    return source


def staged_entity_source_input(user_input: dict[str, Any]) -> dict[str, Any]:
    """Return staged entity adapter config without resolving auto-detect."""
    selected_entities = user_input[CONF_WATTPLAN_ENTITY_ID]
    entity_ids = [selected_entities] if isinstance(selected_entities, str) else [str(entity_id) for entity_id in selected_entities]
    manual = user_input.get("manual", {})
    if not isinstance(manual, dict):
        manual = {}
    source = {
        CONF_SOURCE_MODE: SOURCE_MODE_ENTITY_ADAPTER,
        CONF_WATTPLAN_ENTITY_ID: entity_ids,
        CONF_ADAPTER_TYPE: str(user_input[CONF_ADAPTER_TYPE]),
        CONF_NAME: str(manual.get(CONF_NAME, user_input.get(CONF_NAME, ""))),
        CONF_TIME_KEY: str(manual.get(CONF_TIME_KEY, user_input.get(CONF_TIME_KEY, ""))),
        CONF_VALUE_KEY: str(manual.get(CONF_VALUE_KEY, user_input.get(CONF_VALUE_KEY, ""))),
        CONF_FIXUP_PROFILE: user_input[CONF_FIXUP_PROFILE],
    }
    source.update(user_input.get("advanced", {}))
    return source


def staged_service_source_input(user_input: dict[str, Any]) -> dict[str, Any]:
    """Return staged service adapter config without resolving auto-detect."""
    manual = user_input.get("manual", {})
    if not isinstance(manual, dict):
        manual = {}
    source = {
        CONF_SOURCE_MODE: SOURCE_MODE_SERVICE_ADAPTER,
        CONF_SERVICE: str(user_input[CONF_SERVICE]),
        CONF_ADAPTER_TYPE: str(user_input[CONF_ADAPTER_TYPE]),
        CONF_NAME: str(manual.get(CONF_NAME, user_input.get(CONF_NAME, ""))),
        CONF_TIME_KEY: str(manual.get(CONF_TIME_KEY, user_input.get(CONF_TIME_KEY, ""))),
        CONF_VALUE_KEY: str(manual.get(CONF_VALUE_KEY, user_input.get(CONF_VALUE_KEY, ""))),
        CONF_FIXUP_PROFILE: user_input[CONF_FIXUP_PROFILE],
    }
    source.update(user_input.get("advanced", {}))
    return source
