"""Async boundary for one cancellable, isolated optimizer invocation."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys

from .optimizer import OptimizationParams


async def async_drain(task: asyncio.Future):
    """Finish cleanup despite repeated cancellation, then propagate cancellation."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return task.result()


async def async_run_preset(params: OptimizationParams, preset: str) -> dict:
    """Run a preset outside HA and reap its process before cancellation returns.

    The coordinator owns the whole-call timeout. A fresh interpreter isolates
    native solver state and permits interruption even inside a native solve.
    Preserve HA's import paths, including its optional dependency directory.
    """
    payload = json.dumps(
        {"preset": preset, "params": params.model_dump(mode="json")},
        allow_nan=False,
    ).encode()
    start = asyncio.create_task(asyncio.create_subprocess_exec(
        sys.executable, "-m", "custom_components.wattplan.optimizer.worker",
        cwd=str(Path(__file__).parents[2]),
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    ))
    process = None
    exchange = None
    try:
        # A cancellation while creating the process must still reap that child.
        process = await asyncio.shield(start)
        exchange = asyncio.create_task(process.communicate(payload))
        stdout, stderr = await asyncio.shield(exchange)
        if process.returncode:
            raise RuntimeError(
                f"Planner worker exited ({process.returncode}): "
                f"{stderr.decode(errors='replace')[-4000:]}"
            )
        result = json.loads(stdout)
        if not isinstance(result, dict):
            raise ValueError("Planner worker returned a non-object result")
        return result
    finally:
        async def cleanup():
            child = process if process is not None else await start
            if child.returncode is None:
                try:
                    child.kill()
                except ProcessLookupError:
                    pass
            # Drain pipes as well as wait: full stdout must not delay cleanup.
            if exchange is None:
                await child.communicate()
            else:
                await exchange

        await async_drain(asyncio.create_task(cleanup()))
