"""Slot-boundary scheduling must not supersede the initial plan after setup."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from homeassistant.const import STATE_OFF
from homeassistant.core import HomeAssistant

from custom_components.wattplan.const import (
    CONF_ACTION_EMISSION_ENABLED,
    CONF_PLANNING_ENABLED,
)
from custom_components.wattplan.coordinator_parts import CycleTrigger
from custom_components.wattplan.optimizer.planners.presets import run_preset

from .test_integration_e2e import _battery_subentry, _entry

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@dataclass
class GatedSetup:
    """A config entry whose setup is held in the planner worker until released."""

    entry: object
    setup: asyncio.Task
    release: asyncio.Event


@pytest.fixture
async def gated_setup(hass: HomeAssistant, freezer):
    """Begin setup just before a boundary with the initial plan held in the worker."""
    freezer.move_to("2026-06-15 12:59:54+00:00")
    started, release = asyncio.Event(), asyncio.Event()

    async def gated(params, preset):
        started.set()
        await release.wait()
        return await hass.async_add_executor_job(run_preset, params, preset)

    entry = _entry(
        title="Home",
        subentries_data=[_battery_subentry(subentry_id="battery", name="battery")],
        options={CONF_PLANNING_ENABLED: True, CONF_ACTION_EMISSION_ENABLED: True},
    )
    entry.add_to_hass(hass)
    hass.states.async_set("sensor.battery_soc", "5.0")
    hass.states.async_set("binary_sensor.comfort_on_off", STATE_OFF)
    with patch("custom_components.wattplan.coordinator.async_run_preset", gated):
        setup = asyncio.create_task(hass.config_entries.async_setup(entry.entry_id))
        await asyncio.wait_for(started.wait(), 5)
        try:
            yield GatedSetup(entry, setup, release)
        finally:
            release.set()


async def test_scheduler_starts_after_initial_plan(
    hass: HomeAssistant, freezer, gated_setup: GatedSetup
):
    entry = gated_setup.entry
    # The 13:00 boundary is 6 s away, but no run may be armed to replace this one.
    assert entry.runtime_data.coordinator.next_refresh_at is None
    freezer.move_to("2026-06-15 13:00:30+00:00")
    gated_setup.release.set()
    assert await gated_setup.setup
    await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    assert coordinator.snapshot is not None
    assert coordinator.next_refresh_at == datetime(2026, 6, 15, 14, 0, 2, tzinfo=UTC)


async def test_unload_during_initial_plan_leaves_scheduler_stopped(
    hass: HomeAssistant, gated_setup: GatedSetup
):
    coordinator = gated_setup.entry.runtime_data.coordinator
    await coordinator.async_shutdown()
    gated_setup.release.set()
    assert await gated_setup.setup
    await hass.async_block_till_done()

    assert coordinator.next_refresh_at is None


async def test_superseded_initial_plan_is_not_reported_as_failure(
    hass: HomeAssistant, caplog, gated_setup: GatedSetup
):
    coordinator = gated_setup.entry.runtime_data.coordinator
    caplog.set_level(logging.INFO, logger="custom_components.wattplan")
    newer = asyncio.create_task(coordinator.async_plan(trigger=CycleTrigger.SERVICE))
    assert await gated_setup.setup
    gated_setup.release.set()
    await newer
    await hass.async_block_till_done()

    records = [r for r in caplog.records if "Initial planner run" in r.getMessage()]
    assert [(r.levelno, "superseded" in r.getMessage()) for r in records] == [
        (logging.INFO, True)
    ]
    assert coordinator.snapshot is not None


async def test_failed_initial_plan_still_starts_scheduler(
    hass: HomeAssistant, freezer, caplog
):
    freezer.move_to("2026-06-15 12:59:54+00:00")
    entry = _entry(
        title="Home",
        subentries_data=[_battery_subentry(subentry_id="battery", name="battery")],
        options={CONF_PLANNING_ENABLED: True, CONF_ACTION_EMISSION_ENABLED: True},
    )
    entry.add_to_hass(hass)
    hass.states.async_set("sensor.battery_soc", "5.0")
    hass.states.async_set("binary_sensor.comfort_on_off", STATE_OFF)
    with patch(
        "custom_components.wattplan.coordinator.async_run_preset",
        side_effect=RuntimeError("solver crashed"),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.runtime_data.coordinator.next_refresh_at is not None
    assert any(
        r.levelno == logging.WARNING and "Initial planner run failed" in r.getMessage()
        for r in caplog.records
    )
