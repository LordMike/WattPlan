"""Config flow entrypoints for the WattPlan integration."""

from __future__ import annotations

# ruff: noqa: F401,F403
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentryFlow, SubentryFlowResult
from homeassistant.const import CONF_NAME

from .flows.main import WattPlanConfigFlow, WattPlanOptionsFlow
from .flows.subentries import (
    BatterySubentryFlowHandler,
    ComfortSubentryFlowHandler,
    OptionalSubentryFlowHandler,
)
from .source_providers import async_get_energy_solar_forecast_entries

__all__ = [
    "BatterySubentryFlowHandler",
    "ComfortSubentryFlowHandler",
    "OptionalSubentryFlowHandler",
    "WattPlanConfigFlow",
    "WattPlanOptionsFlow",
    "async_get_energy_solar_forecast_entries",
]
