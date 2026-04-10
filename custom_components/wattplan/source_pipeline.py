"""Shared source pipeline assembly for WattPlan."""

from __future__ import annotations

from typing import Any

from .const import CONF_FIXUP_PROFILE, FIXUP_PROFILE_STRICT
from .source_config import build_source_base_provider, build_source_value_provider
from .source_providers import TemplateAdapterSourceProvider


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
