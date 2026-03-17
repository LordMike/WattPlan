"""Shared config helpers for source providers."""

from __future__ import annotations

from typing import Any

from ..const import CONF_PROVIDERS, CONF_SOURCE_MODE, SOURCE_MODE_ENTITY_ADAPTER

CONF_WATTPLAN_ENTITY_ID = "entity_id"


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
