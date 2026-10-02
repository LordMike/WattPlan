"""Persistence for user-supplied battery targets."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import datetime
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .runtime import BatteryTarget

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
SAVE_DELAY_SECONDS = 1.0


class BatteryTargetStore:
    """Keep battery targets across reloads and restarts for one config entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialize the store for one config entry."""
        self._store = Store[dict[str, Any]](
            hass, STORAGE_VERSION, f"{DOMAIN}.targets.{entry_id}", private=True
        )

    async def async_load(
        self,
        valid_subentry_ids: Collection[str],
        *,
        now: datetime | None = None,
    ) -> dict[str, BatteryTarget]:
        """Return stored targets that are still in the future and belong to a battery."""
        payload = await self._store.async_load()
        stored = payload.get("targets") if isinstance(payload, dict) else None
        if not isinstance(stored, dict):
            return {}

        current_time = now or dt_util.utcnow()
        targets: dict[str, BatteryTarget] = {}
        for subentry_id, item in stored.items():
            if subentry_id not in valid_subentry_ids or not isinstance(item, dict):
                continue
            try:
                reach_at = dt_util.parse_datetime(str(item["reach_at"]))
                soc_kwh = float(item["soc_kwh"])
            except (KeyError, TypeError, ValueError):
                _LOGGER.debug("Ignoring invalid stored battery target %s", subentry_id)
                continue
            if reach_at is None:
                continue
            reach_at = dt_util.as_utc(reach_at)
            if reach_at > current_time:
                targets[subentry_id] = BatteryTarget(soc_kwh=soc_kwh, reach_at=reach_at)
        return targets

    def async_schedule_save(self, targets: Mapping[str, BatteryTarget]) -> None:
        """Save the current targets shortly after the latest change."""
        self._store.async_delay_save(lambda: self._payload(targets), SAVE_DELAY_SECONDS)

    async def async_save(self, targets: Mapping[str, BatteryTarget]) -> None:
        """Save the current targets now, replacing any pending delayed save."""
        await self._store.async_save(self._payload(targets))

    @staticmethod
    def _payload(targets: Mapping[str, BatteryTarget]) -> dict[str, Any]:
        return {
            "targets": {
                subentry_id: {
                    "soc_kwh": float(target.soc_kwh),
                    "reach_at": target.reach_at.isoformat(),
                }
                for subentry_id, target in targets.items()
            }
        }
