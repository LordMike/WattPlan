"""Build self-contained records of optimizer runs for troubleshooting."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.loader import async_get_integration

from .const import DOMAIN


async def async_integration_version(hass: Any) -> str:
    """Return the installed WattPlan integration version."""
    integration = await async_get_integration(hass, DOMAIN)
    return str(integration.version or "unknown")


def build_reproduction(
    request: dict[str, Any],
    result: dict[str, Any],
    *,
    integration_version: str,
) -> dict[str, Any]:
    """Preserve the validated optimizer call arguments and complete result."""
    window = request["window"]
    payload = {
        "schema_version": 1,
        "integration_version": integration_version,
        "request": {
            "window": {
                "start_at": window.start_at.isoformat(),
                "end_at": (
                    window.start_at
                    + timedelta(minutes=window.slot_minutes * window.slots)
                ).isoformat(),
            },
            "slot_minutes": request["slot_minutes"],
            "optimizer_params": request.get(
                "_reproduction_optimizer_params", request["optimizer_params"]
            ),
        },
        "result": result,
    }
    return payload
