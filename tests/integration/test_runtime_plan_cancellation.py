"""Runtime replacement tests for preset changes during active planning."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.wattplan.const import (
    CONF_ACTION_EMISSION_ENABLED,
    CONF_PLANNER_PRESET,
    CONF_PLANNING_ENABLED,
    DOMAIN,
    SERVICE_SET_TARGET,
)
from custom_components.wattplan.coordinator import CycleTrigger, PlanningStageError
from custom_components.wattplan import coordinator as coordinator_module
from tests.integration.test_integration_e2e import (
    _battery_subentry,
    _entry,
    _setup_entry,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


def _preset_options(preset: str) -> dict[str, object]:
    return {
        CONF_PLANNER_PRESET: preset,
        CONF_PLANNING_ENABLED: False,
        CONF_ACTION_EMISSION_ENABLED: False,
    }


@pytest.mark.asyncio
async def test_reload_cancels_running_optimizer_and_replacement_plans_immediately(
    hass: HomeAssistant,
) -> None:
    """A config reload drains obsolete work before planning with the new preset."""
    entry = _entry(
        title="Cancellation Home",
        subentries_data=[],
        options=_preset_options("highs"),
    )
    await _setup_entry(hass, entry)
    old = entry.runtime_data.coordinator
    started, cancelled = asyncio.Event(), asyncio.Event()
    original_runner = coordinator_module.async_run_preset

    async def hold_old_plan(params, preset):
        if preset == "highs":
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        return await original_runner(params, preset)

    with patch.object(
        coordinator_module, "async_run_preset", side_effect=hold_old_plan
    ):
        stale_run = asyncio.create_task(old.async_plan(trigger=CycleTrigger.SERVICE))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            hass.config_entries.async_update_entry(
                entry,
                options=_preset_options("greedy-8"),
            )
            await hass.async_block_till_done(wait_background_tasks=True)

            assert cancelled.is_set()
            replacement = entry.runtime_data.coordinator
            assert replacement is not old
            assert replacement.data is not None
            assert (
                replacement.data.diagnostics["optimizer"]["planner_preset"]
                == "greedy-8"
            )
            assert not replacement._plan_error.has_error
            with pytest.raises(PlanningStageError, match="configuration changed"):
                await stale_run
        finally:
            if not stale_run.done():
                stale_run.cancel()
                await asyncio.gather(stale_run, return_exceptions=True)


@pytest.mark.asyncio
async def test_reload_waits_for_old_cache_write_before_new_preset_commit(
    hass: HomeAssistant,
    hass_storage: dict,
) -> None:
    """An old shielded store write drains before the replacement cache commit."""
    entry = _entry(
        title="Cache Barrier Home",
        subentries_data=[],
        options=_preset_options("highs"),
    )
    await _setup_entry(hass, entry)
    old = entry.runtime_data.coordinator
    save_started, release_save = asyncio.Event(), asyncio.Event()
    original_save = old._persistence.async_persist_snapshot

    async def gated_save(**kwargs):
        save_started.set()
        await release_save.wait()
        await original_save(**kwargs)

    with patch.object(
        old._persistence, "async_persist_snapshot", side_effect=gated_save
    ):
        stale_run = asyncio.create_task(old.async_plan(trigger=CycleTrigger.SERVICE))
        reload_task = None
        try:
            await asyncio.wait_for(save_started.wait(), timeout=5)
            hass.config_entries.async_update_entry(
                entry,
                options=_preset_options("greedy-8"),
            )
            reload_task = asyncio.create_task(
                hass.async_block_till_done(wait_background_tasks=True)
            )

            # Unload is waiting for cancellation to drain the old shielded write.
            await asyncio.sleep(0.05)
            assert not reload_task.done()
            assert entry.runtime_data.coordinator is old

            release_save.set()
            await asyncio.wait_for(reload_task, timeout=10)
            with pytest.raises(PlanningStageError, match="configuration changed"):
                await stale_run

            replacement = entry.runtime_data.coordinator
            assert replacement is not old
            assert replacement.data is not None
            assert (
                replacement.data.diagnostics["optimizer"]["planner_preset"]
                == "greedy-8"
            )
            stored = hass_storage[f"wattplan.snapshot.{entry.entry_id}"]["data"]
            assert stored["planner_preset"] == "greedy-8"
            assert stored["config_fingerprint"] == replacement._config_fingerprint
            assert stored["config_fingerprint"] != old._config_fingerprint
        finally:
            release_save.set()
            if reload_task is not None and not reload_task.done():
                await asyncio.gather(reload_task, return_exceptions=True)
            if not stale_run.done():
                stale_run.cancel()
                await asyncio.gather(stale_run, return_exceptions=True)


@pytest.mark.asyncio
async def test_target_change_cancels_waiting_plan_without_automatic_replan(
    hass: HomeAssistant,
) -> None:
    """A target update invalidates a blocked plan even when run_optimize is false."""
    entry = _entry(
        title="Target Cancellation Home",
        subentries_data=[_battery_subentry(subentry_id="battery", name="battery")],
        options=_preset_options("highs"),
    )
    await _setup_entry(hass, entry)
    coordinator = entry.runtime_data.coordinator
    started, cancelled = asyncio.Event(), asyncio.Event()
    calls = 0

    async def hold_plan(params, preset):
        nonlocal calls
        calls += 1
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with patch.object(
        coordinator_module, "async_run_preset", side_effect=hold_plan
    ):
        stale_run = asyncio.create_task(
            coordinator.async_plan(trigger=CycleTrigger.SERVICE)
        )
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            await hass.services.async_call(
                DOMAIN,
                SERVICE_SET_TARGET,
                {
                    "battery": "battery",
                    "soc_kwh": 7.5,
                    "reach_at": dt_util.utcnow() + timedelta(hours=2),
                    "run_optimize": False,
                },
                blocking=True,
            )

            assert cancelled.is_set()
            assert entry.runtime_data.battery_targets["battery"].soc_kwh == 7.5
            assert coordinator is entry.runtime_data.coordinator
            assert not coordinator.action_recommendations_validated
            assert entry.runtime_data.optimizer_state is None
            assert calls == 1
            with pytest.raises(PlanningStageError, match="superseded this plan"):
                await stale_run
            assert coordinator.snapshot is not None
            assert not coordinator.action_recommendations_validated
        finally:
            if not stale_run.done():
                stale_run.cancel()
                await asyncio.gather(stale_run, return_exceptions=True)


@pytest.mark.asyncio
async def test_config_change_disqualifies_result_before_reload_listener_runs(
    hass: HomeAssistant,
) -> None:
    """A finished old request cannot publish after options change but before reload."""
    entry = _entry(
        title="Config Identity Home",
        subentries_data=[],
        options=_preset_options("highs"),
    )
    await _setup_entry(hass, entry)
    coordinator = entry.runtime_data.coordinator
    snapshot_before = coordinator.snapshot
    success_before = coordinator.last_success_at
    optimizer_started, release_optimizer = asyncio.Event(), asyncio.Event()
    reload_started, release_reload = asyncio.Event(), asyncio.Event()
    original_runner = coordinator_module.async_run_preset

    async def delayed_runner(params, preset):
        optimizer_started.set()
        await release_optimizer.wait()
        return await original_runner(params, preset)

    async def blocked_reload(_entry_id):
        reload_started.set()
        await release_reload.wait()
        return True

    with (
        patch.object(
            coordinator_module, "async_run_preset", side_effect=delayed_runner
        ),
        patch.object(
            hass.config_entries, "async_reload", side_effect=blocked_reload
        ),
    ):
        stale_run = asyncio.create_task(
            coordinator.async_plan(trigger=CycleTrigger.SERVICE)
        )
        try:
            await asyncio.wait_for(optimizer_started.wait(), timeout=5)
            hass.config_entries.async_update_entry(
                entry,
                options=_preset_options("greedy-8"),
            )
            await asyncio.wait_for(reload_started.wait(), timeout=5)

            release_optimizer.set()
            with pytest.raises(PlanningStageError, match="configuration changed"):
                await stale_run

            assert coordinator.snapshot is snapshot_before
            assert coordinator.last_success_at == success_before
            assert (
                coordinator.data.diagnostics["optimizer"]["planner_preset"]
                == "highs"
            )
        finally:
            release_optimizer.set()
            release_reload.set()
            if not stale_run.done():
                stale_run.cancel()
                await asyncio.gather(stale_run, return_exceptions=True)
