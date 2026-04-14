"""Shared plan invariants used by the test suite."""

from __future__ import annotations

from typing import Any


def assert_plan_invariants(result: dict[str, Any]) -> dict[str, Any]:
    """Assert invariants that should hold for every produced plan result."""
    entities = result.get("entities")
    if not isinstance(entities, list):
        return result

    for entity in entities:
        if not isinstance(entity, dict):
            continue
        if entity.get("type") != "battery":
            continue

        schedule = entity.get("schedule")
        if not isinstance(schedule, list):
            continue

        for index, point in enumerate(schedule):
            if not isinstance(point, dict):
                continue

            state = str(point.get("state", "hold"))
            assert state in {
                "hold",
                "discharge",
                "charge_grid",
                "charge_pv",
                "charge_grid_pv",
            }, f"battery {entity.get('name')} schedule[{index}] has invalid state={state}"

    return result
