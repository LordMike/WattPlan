"""Shared plan invariants used by the test suite.

The helper fails loudly on malformed results: a plan without an ``entities``
list, or a battery without a well-formed ``schedule``, is a test failure
rather than something to skip.
"""

from __future__ import annotations

from typing import Any

BATTERY_STATES = frozenset({"preserve", "self_consume", "grid_charge"})


def assert_plan_invariants(result: dict[str, Any]) -> dict[str, Any]:
    """Assert invariants that should hold for every produced plan result."""
    assert isinstance(result, dict), f"plan result must be a dict, got {type(result)}"
    entities = result.get("entities")
    assert isinstance(entities, list), "plan result must contain an entities list"

    for position, entity in enumerate(entities):
        assert isinstance(entity, dict), f"entities[{position}] must be a dict"
        if entity.get("type") != "battery":
            continue

        name = entity.get("name")
        schedule = entity.get("schedule")
        assert isinstance(schedule, list), f"battery {name} must have a schedule list"

        for index, point in enumerate(schedule):
            assert isinstance(
                point, dict
            ), f"battery {name} schedule[{index}] must be a dict"
            state = point.get("state", "self_consume")
            assert (
                state in BATTERY_STATES
            ), f"battery {name} schedule[{index}] has invalid state={state}"

    return result
