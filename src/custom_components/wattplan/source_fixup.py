"""Post-provider source fixup for WattPlan."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .const import (
    CONF_EDGE_FILL_MODE,
    CONF_FIXUP_PROFILE,
    CONF_RESAMPLE_MODE,
    EDGE_FILL_MODE_NONE,
    FIXUP_PROFILE_EXTEND,
    FIXUP_PROFILE_REPAIR,
    FIXUP_PROFILE_STRICT,
    RESAMPLE_MODE_NONE,
)
from .source_provider import SourceProvider, SourceProviderError, SourceWindow


def effective_provider_config(source_config: dict[str, Any]) -> dict[str, Any]:
    """Return provider config after applying the selected fixup policy."""
    effective = dict(source_config)
    profile = str(effective.get(CONF_FIXUP_PROFILE, FIXUP_PROFILE_REPAIR))
    if profile == FIXUP_PROFILE_STRICT:
        effective[CONF_RESAMPLE_MODE] = RESAMPLE_MODE_NONE
        effective[CONF_EDGE_FILL_MODE] = EDGE_FILL_MODE_NONE
    return effective


class SourceFixupProvider(SourceProvider):
    """Apply post-provider fixups so the final result matches the horizon."""

    def __init__(
        self,
        provider: SourceProvider,
        *,
        profile: str,
    ) -> None:
        """Initialize the fixup wrapper."""
        self._provider = provider
        self._profile = profile

    async def async_values(self, window: SourceWindow) -> list[float]:
        """Return exactly `window.slots` values after fixup."""
        try:
            return await self._provider.async_values(window)
        except SourceProviderError as err:
            if self._profile != FIXUP_PROFILE_EXTEND:
                raise
            return await self._repeat_t_minus_24h(window, err)

    async def _repeat_t_minus_24h(
        self, window: SourceWindow, original_error: SourceProviderError
    ) -> list[float]:
        """Extend the missing tail by repeating data from 24 hours earlier."""
        day_slots = int((24 * 60) / window.slot_minutes)
        available_count = int(original_error.details.get("available_count", 0))
        known_slots = max(day_slots, available_count)
        if day_slots <= 0 or known_slots < day_slots:
            raise original_error

        base_window = replace(window, slots=min(window.slots, known_slots))
        try:
            values = await self._provider.async_values(base_window)
        except SourceProviderError:
            raise original_error from None

        if len(values) < day_slots:
            raise original_error

        completed = list(values)
        while len(completed) < window.slots:
            repeat_index = len(completed) - day_slots
            if repeat_index < 0:
                raise original_error
            completed.append(completed[repeat_index])
        return completed[: window.slots]

