"""Home Assistant Energy platform support for WattPlan testbed PV forecasts."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .runtime import TestbedRuntime


async def async_get_solar_forecast(
    hass: HomeAssistant, config_entry_id: str
) -> dict[str, dict[str, float]] | None:
    """Return aggregated solar forecast for one testbed entry."""
    runtime: TestbedRuntime | None = hass.data.get(DOMAIN, {}).get(config_entry_id)
    if runtime is None:
        return None
    return {"wh_hours": runtime.energy_pv_wh_hours()}
