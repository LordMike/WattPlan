"""The plan invariant helper must reject malformed plans, not skip them."""

import pytest

from tests.plan_invariants import assert_plan_invariants


def _battery(schedule):
    return {"type": "battery", "name": "b1", "schedule": schedule}


def test_accepts_valid_plan() -> None:
    plan = {"entities": [_battery([{"state": "grid_charge"}, {}])]}
    assert assert_plan_invariants(plan) is plan


@pytest.mark.parametrize(
    "plan",
    [
        [],
        {},
        {"entities": None},
        {"entities": ["x"]},
        {"entities": [{"type": "battery", "name": "b1"}]},
        {"entities": [_battery(["x"])]},
        {"entities": [_battery([{"state": "charging"}])]},
    ],
)
def test_rejects_malformed_plan(plan) -> None:
    with pytest.raises(AssertionError):
        assert_plan_invariants(plan)
