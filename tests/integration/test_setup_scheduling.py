"""Slot-boundary scheduling must not supersede the initial plan after setup."""

from __future__ import annotations

import asyncio
import logging
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

from tests.common import async_fire_time_changed

from .test_integration_e2e import (  # noqa: F401
    _battery_subentry,
    _entry,
    _setup_entry,
    entity_registry_enabled_by_default,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


async def _start_gated_setup(hass: HomeAssistant, freezer):
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
    patcher = patch("custom_components.wattplan.coordinator.async_run_preset", gated)
    patcher.start()
    setup = asyncio.create_task(hass.config_entries.async_setup(entry.entry_id))
    await asyncio.wait_for(started.wait(), 5)
    return entry, setup, release, patcher


async def test_scheduler_starts_after_initial_plan(hass: HomeAssistant, freezer):
    entry, setup, release, patcher = await _start_gated_setup(hass, freezer)
    try:
        # The 13:00 boundary is 6 s away, but no run may be armed to replace this one.
        assert entry.runtime_data.coordinator.next_refresh_at is None
        freezer.move_to("2026-06-15 13:00:30+00:00")
        release.set()
        assert await setup
        await hass.async_block_till_done()
    finally:
        release.set()
        patcher.stop()

    coordinator = entry.runtime_data.coordinator
    assert coordinator.snapshot is not None
    assert coordinator.next_refresh_at == datetime(2026, 6, 15, 14, 0, 2, tzinfo=UTC)


async def test_unload_during_initial_plan_leaves_scheduler_stopped(
    hass: HomeAssistant, freezer
):
    entry, setup, release, patcher = await _start_gated_setup(hass, freezer)
    coordinator = entry.runtime_data.coordinator
    try:
        await coordinator.async_shutdown()
        release.set()
        assert await setup
        await hass.async_block_till_done()
    finally:
        release.set()
        patcher.stop()

    assert coordinator.next_refresh_at is None


async def test_superseded_initial_plan_is_not_reported_as_failure(
    hass: HomeAssistant, freezer, caplog
):
    entry, setup, release, patcher = await _start_gated_setup(hass, freezer)
    coordinator = entry.runtime_data.coordinator
    caplog.set_level(logging.INFO, logger="custom_components.wattplan")
    try:
        newer = asyncio.create_task(coordinator.async_plan(trigger=CycleTrigger.SERVICE))
        assert await setup
        release.set()
        await newer
        await hass.async_block_till_done()
    finally:
        release.set()
        patcher.stop()

    records = [r for r in caplog.records if "Initial planner run" in r.getMessage()]
    assert [(r.levelno, "superseded" in r.getMessage()) for r in records] == [
        (logging.INFO, True)
    ]
    assert coordinator.snapshot is not None


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_next_run_sensor_tracks_armed_schedule(hass: HomeAssistant, freezer):
    """Next Run publishes the armed boundary after setup and after each scheduled run."""
    freezer.move_to("2026-06-15 12:30:00+00:00")
    entry = _entry(
        title="Home",
        subentries_data=[_battery_subentry(subentry_id="battery", name="battery")],
        options={CONF_PLANNING_ENABLED: True, CONF_ACTION_EMISSION_ENABLED: True},
    )
    await _setup_entry(hass, entry)
    assert hass.states.get("sensor.home_next_run").state == "2026-06-15T13:00:02+00:00"

    freezer.move_to("2026-06-15 13:00:02+00:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert entry.runtime_data.coordinator.last_attempt_at == datetime(
        2026, 6, 15, 13, 0, 2, tzinfo=UTC
    )
    assert hass.states.get("sensor.home_next_run").state == "2026-06-15T14:00:02+00:00"
