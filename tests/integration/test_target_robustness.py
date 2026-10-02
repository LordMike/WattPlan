"""Battery target validation and persistence tests."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util

from custom_components.wattplan.const import DOMAIN, SERVICE_SET_TARGET

from .test_integration_e2e import (
    _battery_subentry,
    _entry,
    _fake_optimize_with_entities,
    _setup_entry,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


async def _set_target(hass: HomeAssistant, **data: Any) -> None:
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_TARGET,
        {"battery": "battery", "run_optimize": False, **data},
        blocking=True,
    )


async def _setup_battery_entry(hass: HomeAssistant):
    entry = _entry(
        title="Home",
        subentries_data=[_battery_subentry(subentry_id="battery", name="battery")],
    )
    with patch(
        "custom_components.wattplan.coordinator.optimize",
        side_effect=_fake_optimize_with_entities,
    ):
        await _setup_entry(hass, entry)
    return entry


async def test_set_target_rejects_soc_above_capacity(hass: HomeAssistant) -> None:
    entry = await _setup_battery_entry(hass)  # capacity is 10 kWh

    with pytest.raises(ServiceValidationError, match="exceeds the capacity"):
        await _set_target(
            hass,
            soc_kwh=10.5,
            reach_at=dt_util.utcnow() + timedelta(hours=2),
        )

    assert entry.runtime_data.battery_targets == {}


async def test_set_target_rejects_deadline_in_the_past(hass: HomeAssistant) -> None:
    entry = await _setup_battery_entry(hass)

    with pytest.raises(ServiceValidationError, match="in the future"):
        await _set_target(
            hass,
            soc_kwh=8.0,
            reach_at=dt_util.utcnow() - timedelta(minutes=5),
        )

    assert entry.runtime_data.battery_targets == {}


async def test_set_target_accepts_full_capacity(hass: HomeAssistant) -> None:
    entry = await _setup_battery_entry(hass)

    await _set_target(
        hass,
        soc_kwh=10.0,
        reach_at=dt_util.utcnow() + timedelta(hours=2),
    )

    assert entry.runtime_data.battery_targets["battery"].soc_kwh == 10.0
