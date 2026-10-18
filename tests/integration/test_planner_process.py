"""Real child-process execution and cancellation at every IPC boundary."""

import asyncio
import sys
from unittest.mock import patch

import pytest

from custom_components.wattplan.optimizer import OptimizationParams
from custom_components.wattplan.planner_process import async_run_preset


def _params():
    return OptimizationParams(
        grid_import_price_per_kwh=[1, 2, 3, 4],
        usage_kwh=[1, 1, 1, 1],
        solar_input_kwh=[0, 0, 0, 0],
        battery_entities=[{
            "name": "battery", "initial_kwh": 1.0, "minimum_kwh": 0.0,
            "capacity_kwh": 4.0, "charge_curve_kwh": [2.0],
            "discharge_curve_kwh": [2.0], "can_charge_from": 3,
        }],
        comfort_entities=[],
    )


@pytest.mark.parametrize("preset", ["highs", "local-search-3"])
async def test_real_process_roundtrip(preset):
    result = await async_run_preset(_params(), preset)
    assert result["diagnostics"]["planner"]["preset"] == preset
    assert len(result["energy_flows"]["per_slot"]) == 4
    assert len(result["entities"][0]["schedule"]) == 4


@pytest.mark.parametrize("phase", ["startup", "compute", "output"])
async def test_cancellation_reaps_real_child(phase):
    """Kill native/unresponsive work, including blocked input/output exchange."""
    original = asyncio.create_subprocess_exec
    started, release = asyncio.Event(), asyncio.Event()
    processes = []

    async def spawn(*args, **kwargs):
        script = (
            "import sys,time; sys.stdout.write('x'*1000000); sys.stdout.flush(); time.sleep(60)"
            if phase == "output" else "import time; time.sleep(60)"
        )
        process = await original(sys.executable, "-c", script, **kwargs)
        processes.append(process)
        started.set()
        if phase == "startup":
            await release.wait()
        return process

    with patch("asyncio.create_subprocess_exec", side_effect=spawn):
        task = asyncio.create_task(async_run_preset(_params(), "highs"))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
    assert processes[0].returncode is not None
    # A real new planner starts successfully after the old child has been reaped.
    result = await async_run_preset(_params(), "local-search-3")
    assert result["diagnostics"]["planner"]["preset"] == "local-search-3"


async def test_timeout_reaps_worker():
    original = asyncio.create_subprocess_exec
    processes = []

    async def spawn(*args, **kwargs):
        process = await original(
            sys.executable, "-c", "import time; time.sleep(60)", **kwargs
        )
        processes.append(process)
        return process

    with patch("asyncio.create_subprocess_exec", side_effect=spawn):
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.2):
                await async_run_preset(_params(), "highs")
    assert processes and processes[0].returncode is not None


async def test_worker_error_does_not_poison_next_invocation():
    with pytest.raises(RuntimeError, match="Unknown planner preset"):
        await async_run_preset(_params(), "removed-preset")
    result = await async_run_preset(_params(), "highs")
    assert result["diagnostics"]["planner"]["preset"] == "highs"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_real_worker_publishes_home_assistant_plan(hass, monkeypatch):
    from custom_components.wattplan import coordinator as coordinator_module
    from .test_integration_e2e import _battery_subentry, _entry, _setup_entry

    monkeypatch.setattr(coordinator_module, "async_run_preset", async_run_preset)
    entry = _entry(
        title="Process Home",
        subentries_data=[_battery_subentry(subentry_id="battery", name="battery")],
    )
    await _setup_entry(hass, entry)
    coordinator = entry.runtime_data.coordinator
    assert coordinator.action_recommendations_validated
    assert len(coordinator.snapshot.action_schedules["batteries"]["battery"]) == 4
    assert coordinator.restore_payload()["planner_preset"] == "highs"


@pytest.mark.parametrize("phase", ["startup", "exchange"])
async def test_repeated_cancellation_cannot_interrupt_cleanup(phase):
    original = asyncio.create_subprocess_exec
    started, release = asyncio.Event(), asyncio.Event()
    processes = []

    async def spawn(*args, **kwargs):
        child = await original(
            sys.executable, "-c", "import time; time.sleep(60)", **kwargs
        )
        processes.append(child)
        if phase == "exchange":
            communicate = child.communicate

            async def gated_communicate(*args):
                started.set()
                await release.wait()
                return await communicate(*args)

            child.communicate = gated_communicate
        else:
            started.set()
            await release.wait()
        return child

    with patch("asyncio.create_subprocess_exec", side_effect=spawn):
        task = asyncio.create_task(async_run_preset(_params(), "highs"))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
    assert processes[0].returncode is not None
