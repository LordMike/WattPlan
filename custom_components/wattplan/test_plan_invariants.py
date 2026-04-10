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
            charge_source = int(point.get("charge_source", 0))

            if state == "charge":
                assert charge_source in {1, 2, 3}, (
                    f"battery {entity.get('name')} schedule[{index}] is charge "
                    f"but has invalid charge_source={charge_source}"
                )
            elif state in {"hold", "discharge"}:
                assert charge_source == 0, (
                    f"battery {entity.get('name')} schedule[{index}] is {state} "
                    f"but has non-empty charge_source={charge_source}"
                )

    return result
