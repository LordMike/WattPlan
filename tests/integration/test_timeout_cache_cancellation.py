"""Timeout cache removal must drain before a replacement plan can persist."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.wattplan import coordinator as coordinator_module
from custom_components.wattplan.coordinator import CycleTrigger, PlanningStageError
from tests.integration.test_integration_e2e import _entry, _setup_entry

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.mark.asyncio
async def test_replacement_plan_waits_for_timeout_cache_removal(
    hass: HomeAssistant,
    hass_storage: dict,
) -> None:
    """A new successful cache cannot race ahead of timeout cleanup."""
    entry = _entry(title="Timeout Cache Barrier", subentries_data=[])
    await _setup_entry(hass, entry)
    coordinator = entry.runtime_data.coordinator
    storage_key = f"wattplan.snapshot.{entry.entry_id}"
    old_payload = hass_storage[storage_key]["data"]
    assert old_payload["planner_preset"] == coordinator._persistence._planner_preset

    remove_started, release_remove = asyncio.Event(), asyncio.Event()
    replacement_save_started = asyncio.Event()
    original_remove = coordinator._snapshot_store.async_remove
    original_persist = coordinator._persistence.async_persist_snapshot
    original_runner = coordinator_module.async_run_preset
    runner_calls = 0

    async def gated_remove() -> None:
        remove_started.set()
        await release_remove.wait()
        await original_remove()

    async def timeout_then_succeed(params, preset):
        nonlocal runner_calls
        runner_calls += 1
        if runner_calls == 1:
            raise TimeoutError("simulated optimizer deadline")
        return await original_runner(params, preset)

    async def observe_replacement_save(**kwargs) -> None:
        replacement_save_started.set()
        await original_persist(**kwargs)

    with (
        patch.object(
            coordinator._snapshot_store, "async_remove", side_effect=gated_remove
        ),
        patch.object(
            coordinator._persistence,
            "async_persist_snapshot",
            side_effect=observe_replacement_save,
        ),
        patch.object(
            coordinator_module, "async_run_preset", side_effect=timeout_then_succeed
        ),
    ):
        timed_out_plan = asyncio.create_task(
            coordinator.async_plan(trigger=CycleTrigger.SERVICE)
        )
        replacement_plan = None
        try:
            await asyncio.wait_for(remove_started.wait(), timeout=5)
            assert hass_storage[storage_key]["data"] == old_payload

            replacement_plan = asyncio.create_task(
                coordinator.async_plan(trigger=CycleTrigger.SERVICE)
            )
            await asyncio.sleep(0.05)

            assert not replacement_plan.done()
            assert runner_calls == 1
            assert not replacement_save_started.is_set()
            # The old bytes have not been removed or replaced while cleanup is
            # blocked; no replacement plan has reached its cache commit.
            assert hass_storage[storage_key]["data"] == old_payload

            release_remove.set()
            with pytest.raises(PlanningStageError, match="superseded this plan"):
                await timed_out_plan
            await asyncio.wait_for(replacement_plan, timeout=20)

            assert runner_calls == 2
            assert replacement_save_started.is_set()
            assert coordinator.snapshot is not None
            assert coordinator.has_usable_plan
            assert hass_storage[storage_key]["data"] != old_payload
        finally:
            release_remove.set()
            for task in (timed_out_plan, replacement_plan):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(
                    task
                    for task in (timed_out_plan, replacement_plan)
                    if task is not None
                ),
                return_exceptions=True,
            )
