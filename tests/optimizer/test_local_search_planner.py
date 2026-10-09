"""Local-search restart bounds, seed construction and tiny-budget regressions."""

import pytest

from custom_components.wattplan.optimizer.models import OptimizationParams
from custom_components.wattplan.optimizer.planners.local_search import factory
from tests.optimizer.planner_conformance import economic_params as request


@pytest.mark.parametrize(
    "options",
    [
        {"max_evaluations": 0},
        {"max_evaluations": True},
        {"max_seconds": 0},
        {"max_seconds": float("nan")},
        {"seed": True},
        {"seed": 1.5},
        {"max_restarts": -1},
        {"max_restarts": True},
    ],
)
def test_invalid_options(options):
    with pytest.raises((ValueError, TypeError)):
        factory(**options)


def test_duplicate_candidates_cannot_cause_unbounded_restarts():
    result = factory(max_evaluations=200, max_restarts=100000)(request())
    diagnostic = result["diagnostics"]
    assert diagnostic["candidate_attempts"] <= diagnostic["candidate_attempt_limit"]
    assert diagnostic["candidate_attempt_limit"] == 1600
    assert diagnostic["evaluations"] <= 81  # Three actions to four slots.
    assert diagnostic["termination_reason"] == "candidate_attempt_limit"


@pytest.mark.parametrize("count", [1, 3, 96])
def test_first_candidate_restores_low_reserve_before_cheaper_later_price(count):
    payload = request(prices=(10, 1, 1, 1), usage=(0, 0, 0, 0)).model_dump()
    payload["battery_entities"][0]["minimum_kwh"] = 0.8
    result = factory(max_evaluations=count)(OptimizationParams(**payload))
    first = result["entities"][0]["schedule"][0]
    assert first["state"] == "grid_charge"
    assert first["level"] >= 0.8
    assert "battery_min_unmet" not in result["suboptimal_reasons"]
    assert result["diagnostics"]["evaluations"] <= count
    assert result["diagnostics"]["reserve_seed_construction_passes"] == 1


@pytest.mark.parametrize("count", [1, 3, 96])
def test_small_budget_prioritizes_reachable_immediate_target(count):
    payload = request(
        prices=(10, 1, 1, 1), usage=(0, 0, 0, 0), initial=0.2,
        target={"timeslot": 0, "soc_kwh": 1.0},
    ).model_dump()
    payload["battery_entities"][0]["charge_efficiency"] = 0.9
    result = factory(max_evaluations=count)(OptimizationParams(**payload))
    assert result["entities"][0]["schedule"][0]["state"] == "grid_charge"
    assert "battery_target_unmet" not in result["suboptimal_reasons"]
    assert result["diagnostics"]["evaluations"] <= count
    assert result["diagnostics"]["reserve_seed_construction_passes"] == 0


def test_recovery_and_target_never_override_grid_ingress_permission():
    payload = request(
        prices=(10, 1, 1, 1), usage=(0, 0, 0, 0),
        target={"timeslot": 0, "soc_kwh": 1.0},
    ).model_dump()
    payload["battery_entities"][0].update(minimum_kwh=0.8, can_charge_from=2)
    result = factory(max_evaluations=3)(OptimizationParams(**payload))
    assert all(item["state"] != "grid_charge" for item in result["entities"][0]["schedule"])
    assert {"battery_target_unmet", "battery_min_unmet"} <= set(result["suboptimal_reasons"])
