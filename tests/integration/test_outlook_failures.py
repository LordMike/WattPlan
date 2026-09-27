"""Plan Outlook failure isolation tests."""

from __future__ import annotations

import logging
from unittest.mock import patch

from custom_components.wattplan.const import (
    CONF_ACTION_EMISSION_ENABLED,
    CONF_HOURS_TO_PLAN,
    CONF_NAME,
    CONF_PLANNING_ENABLED,
    CONF_SLOT_MINUTES,
    CONF_SOURCE_IMPORT_PRICE,
    CONF_SOURCE_MODE,
    CONF_SOURCES,
    CONF_TEMPLATE,
    DOMAIN,
    SERVICE_RUN_OPTIMIZE_NOW,
    SOURCE_MODE_TEMPLATE,
)
import pytest

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant

from tests.common import MockConfigEntry

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def _entry() -> MockConfigEntry:
    """Return a minimal entry that can accept a plan."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        data={
            CONF_NAME: "Home",
            CONF_SLOT_MINUTES: 60,
            CONF_HOURS_TO_PLAN: 4,
            CONF_SOURCES: {
                CONF_SOURCE_IMPORT_PRICE: {
                    CONF_SOURCE_MODE: SOURCE_MODE_TEMPLATE,
                    CONF_TEMPLATE: "{{ [0.2, 0.25, 0.3, 0.35] }}",
                },
            },
        },
        options={
            CONF_PLANNING_ENABLED: False,
            CONF_ACTION_EMISSION_ENABLED: False,
        },
    )


def _optimizer_result(_params: object) -> dict[str, object]:
    """Return a small valid result for plan projection."""
    return {
        "execution_time": 0.01,
        "fitness": 1.0,
        "avg_price": 0.25,
        "projections": {
            "baseline_cost": 1.0,
            "projected_cost": 0.8,
            "projected_savings_cost": 0.2,
            "projected_savings_pct": 20.0,
            "per_slot": [
                {
                    "baseline_cost": 0.25,
                    "projected_cost": 0.2,
                    "projected_savings_cost": 0.05,
                    "projected_savings_pct": 20.0,
                }
                for _ in range(4)
            ],
        },
        "suboptimal": False,
        "suboptimal_reasons": [],
        "problems": [],
        "successful_solves": 1,
        "reused_steps": 0,
        "entities": [],
        "optional_entity_options": [],
        "state": None,
    }


async def test_outlook_generation_failure_keeps_plan_usable_and_sensor_unavailable(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Outlook generation failure must not turn an accepted plan into no plan."""
    entry = _entry()
    entry.add_to_hass(hass)

    with patch("custom_components.wattplan.coordinator.optimize", _optimizer_result):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        with caplog.at_level(logging.ERROR):
            with patch(
                "custom_components.wattplan.coordinator_logic.projection.build_plan_outlook",
                side_effect=RuntimeError("outlook generation failed"),
            ):
                await hass.services.async_call(
                    DOMAIN, SERVICE_RUN_OPTIMIZE_NOW, {}, blocking=True
                )
                await hass.async_block_till_done()

    status = hass.states.get("sensor.home_status")
    outlook = hass.states.get("sensor.home_plan_outlook_en")
    assert status is not None
    assert status.attributes["has_usable_plan"] is True
    assert entry.runtime_data.coordinator.snapshot is not None
    assert outlook is not None
    assert outlook.state == STATE_UNAVAILABLE
    assert "Plan Outlook generation failed" in caplog.text
    assert entry.entry_id in caplog.text


async def test_outlook_render_failure_makes_only_outlook_sensor_unavailable(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Stored Outlook rendering failure must leave the accepted plan usable."""
    entry = _entry()
    entry.add_to_hass(hass)

    with patch("custom_components.wattplan.coordinator.optimize", _optimizer_result):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await hass.services.async_call(
            DOMAIN, SERVICE_RUN_OPTIMIZE_NOW, {}, blocking=True
        )
        await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    with caplog.at_level(logging.ERROR):
        with patch(
            "custom_components.wattplan.sensors.outlook.render_stored_plan_outlook",
            side_effect=RuntimeError("outlook rendering failed"),
        ):
            coordinator.async_update_listeners()
            await hass.async_block_till_done()

    status = hass.states.get("sensor.home_status")
    outlook = hass.states.get("sensor.home_plan_outlook_en")
    assert status is not None
    assert status.attributes["has_usable_plan"] is True
    assert outlook is not None
    assert outlook.state == STATE_UNAVAILABLE
    assert "Plan Outlook rendering failed" in caplog.text
    assert entry.entry_id in caplog.text


async def test_no_plan_outlook_remains_a_status_report(hass: HomeAssistant) -> None:
    """Genuine no-plan states should continue to provide a status report."""
    entry = _entry()
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    outlook = hass.states.get("sensor.home_plan_outlook_en")
    assert outlook is not None
    assert outlook.state.startswith("plan_unavailable_high_")
