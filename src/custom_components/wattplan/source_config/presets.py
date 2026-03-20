"""Source defaults and recommendation helpers."""

from __future__ import annotations

from typing import Any

from homeassistant.const import CONF_NAME

from ..const import (
    ADAPTER_TYPE_AUTO_DETECT,
    AGGREGATION_MODE_FIRST,
    CLAMP_MODE_NEAREST,
    CONF_ADAPTER_TYPE,
    CONF_AGGREGATION_MODE,
    CONF_CLAMP_MODE,
    CONF_EDGE_FILL_MODE,
    CONF_FIXUP_PROFILE,
    CONF_PROVIDERS,
    CONF_RESAMPLE_MODE,
    CONF_SOURCE_EXPORT_PRICE,
    CONF_SOURCE_IMPORT_PRICE,
    CONF_SOURCE_PV,
    CONF_SOURCE_USAGE,
    CONF_TIME_KEY,
    CONF_VALUE_KEY,
    EDGE_FILL_MODE_HOLD,
    FIXUP_PROFILE_EXTEND,
    RESAMPLE_MODE_NONE,
    SOURCE_MODE_BUILT_IN,
    SOURCE_MODE_ENTITY_ADAPTER,
    SOURCE_MODE_ENERGY_PROVIDER,
    SOURCE_MODE_NOT_USED,
    SOURCE_MODE_TEMPLATE,
)
from .provider import primary_provider_config, source_mode


def default_modifier_values() -> dict[str, str]:
    """Return default advanced fixup settings."""
    return {
        CONF_AGGREGATION_MODE: AGGREGATION_MODE_FIRST,
        CONF_CLAMP_MODE: CLAMP_MODE_NEAREST,
        CONF_RESAMPLE_MODE: RESAMPLE_MODE_NONE,
        CONF_EDGE_FILL_MODE: EDGE_FILL_MODE_HOLD,
    }


def source_base_defaults(source: dict[str, Any]) -> dict[str, Any]:
    """Return defaults for provider-specific input including fixup settings."""
    provider = primary_provider_config(source)
    defaults = {
        **dict(provider),
        **{key: value for key, value in source.items() if key not in {CONF_PROVIDERS}},
    }
    providers = source.get(CONF_PROVIDERS)
    if isinstance(providers, list) and providers and source_mode(source) == SOURCE_MODE_ENTITY_ADAPTER:
        defaults["entity_id"] = [
            provider["entity_id"]
            for provider in providers
            if isinstance(provider, dict) and "entity_id" in provider
        ]
    return defaults


def preferred_source_mode(
    key: str,
    *,
    include_not_used: bool,
    include_built_in: bool = False,
    include_energy_provider: bool = False,
) -> str:
    """Return the recommended default mode for one source selection step."""
    if key == CONF_SOURCE_IMPORT_PRICE:
        return SOURCE_MODE_ENTITY_ADAPTER
    if key == CONF_SOURCE_EXPORT_PRICE:
        return SOURCE_MODE_NOT_USED if include_not_used else SOURCE_MODE_ENTITY_ADAPTER
    if key == CONF_SOURCE_USAGE:
        if include_built_in:
            return SOURCE_MODE_BUILT_IN
        return SOURCE_MODE_NOT_USED if include_not_used else SOURCE_MODE_ENTITY_ADAPTER
    if key == CONF_SOURCE_PV:
        if include_energy_provider:
            return SOURCE_MODE_ENERGY_PROVIDER
        return SOURCE_MODE_NOT_USED if include_not_used else SOURCE_MODE_ENTITY_ADAPTER
    return SOURCE_MODE_NOT_USED if include_not_used else SOURCE_MODE_TEMPLATE


def auto_detect_step_defaults(
    user_input: dict[str, Any], resolved_source: dict[str, Any]
) -> dict[str, Any]:
    """Return defaults that preserve auto mode while showing resolved keys."""
    defaults = dict(user_input)
    provider = primary_provider_config(resolved_source)
    defaults[CONF_NAME] = provider.get(CONF_NAME, "")
    defaults[CONF_TIME_KEY] = provider.get(CONF_TIME_KEY, "")
    defaults[CONF_VALUE_KEY] = provider.get(CONF_VALUE_KEY, "")
    defaults[CONF_ADAPTER_TYPE] = ADAPTER_TYPE_AUTO_DETECT
    return defaults


def apply_horizon_fill_defaults(source_config: dict[str, Any]) -> dict[str, Any]:
    """Return source config with the recommended fill defaults enabled."""
    return {
        **source_config,
        CONF_FIXUP_PROFILE: FIXUP_PROFILE_EXTEND,
        CONF_AGGREGATION_MODE: AGGREGATION_MODE_FIRST,
        CONF_CLAMP_MODE: CLAMP_MODE_NEAREST,
        CONF_RESAMPLE_MODE: RESAMPLE_MODE_LINEAR,
        CONF_EDGE_FILL_MODE: EDGE_FILL_MODE_HOLD,
    }


def source_fill_defaults_needed(source_config: dict[str, Any]) -> bool:
    """Return whether the source config differs from the fill defaults."""
    repaired = apply_horizon_fill_defaults(source_config)
    watched_keys = {
        CONF_FIXUP_PROFILE,
        CONF_AGGREGATION_MODE,
        CONF_CLAMP_MODE,
        CONF_RESAMPLE_MODE,
        CONF_EDGE_FILL_MODE,
    }
    return any(source_config.get(key) != repaired[key] for key in watched_keys)
