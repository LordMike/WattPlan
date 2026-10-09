"""CEM probability updates, sampling bounds, masks and seed regressions."""

import pytest

from custom_components.wattplan.optimizer.models import OptimizationParams
from custom_components.wattplan.optimizer.planners.evaluation import evaluate_schedule
from custom_components.wattplan.optimizer.planners.cross_entropy import factory
from tests.optimizer.planner_conformance import (
    battery_policies as policies, economic_params as request,
)


@pytest.mark.parametrize(
    "options",
    [
        {"max_evaluations": 0},
        {"max_evaluations": True},
        {"max_seconds": 0},
        {"max_seconds": float("nan")},
        {"seed": True},
        {"seed": 1.5},
    ],
)
def test_invalid_options(options):
    with pytest.raises((ValueError, TypeError)):
        factory(**options)


def test_collapsed_distribution_cannot_cause_unbounded_sampling():
    result = factory(
        max_evaluations=200, guidance=1.0, probability_floor=1e-12
    )(request())
    diagnostic = result["diagnostics"]
    assert diagnostic["candidate_attempts"] <= diagnostic["candidate_attempt_limit"]
    assert diagnostic["candidate_attempt_limit"] == 1600
    assert diagnostic["evaluations"] <= 81  # Three actions to four slots.
    assert diagnostic["termination_reason"] == "candidate_attempt_limit"


def test_elite_update_is_smoothed_masked_and_normalized():
    from custom_components.wattplan.optimizer.planners.cross_entropy import _update

    distributions = [[[0.7, 0.3, 0.0]]]
    elite = [((0, 0), [["preserve"]])]
    _update(distributions, elite, smoothing=0.5, floor=0.1, allowed=[(0, 1)])
    assert distributions[0][0] == pytest.approx([0.38, 0.62, 0])
    assert sum(distributions[0][0]) == pytest.approx(1)


def test_cem_samples_and_updates_multiple_generations():
    params = request(
        prices=(0.1, 1, 0.2, 2, 0.3, 3), usage=(0, 1, 0, 1, 0, 1)
    )
    result = factory(
        max_evaluations=100, population_size=4, guidance=0.65, block_slots=1
    )(params)
    assert result["diagnostics"]["generations"] >= 2
    assert result["diagnostics"]["candidate_attempts"] > 0


@pytest.mark.parametrize("options", [
    {"population_size": True}, {"population_size": 1}, {"block_slots": 0},
    {"block_slots": 673}, {"elite_fraction": 0}, {"elite_fraction": float("nan")},
    {"smoothing": 0}, {"probability_floor": 0.34}, {"guidance": 0},
    {"guidance": True}, {"probability_floor": float("inf")},
])
def test_invalid_cem_options(options):
    with pytest.raises((ValueError, TypeError)):
        factory(**options)


def test_rounding_fallback_never_emits_forbidden_grid_action():
    from custom_components.wattplan.optimizer.planners.cross_entropy import _categorical

    assert _categorical([0.5, 0.4999999999999998, 0], 0.9999999999999999, (0, 1)) == 1


def test_floor_applies_to_one_hot_initial_guidance():
    params = request(
        prices=(0.1, 1, 0.2, 2, 0.3, 3), usage=(0, 1, 0, 1, 0, 1)
    )
    result = factory(
        max_evaluations=100, population_size=4, guidance=1,
        probability_floor=1 / 3, block_slots=1,
    )(params)
    assert result["diagnostics"]["generations"] >= 2


@pytest.mark.parametrize("count", [1, 48])
@pytest.mark.parametrize("mode,initial,goal,usage", [
    ("at_least", 0, 1, (0, 0, 0, 0)),
    ("exact", 0, 1, (0, 0, 0, 0)),
    ("at_most", 1, 0, (1, 0, 0, 0)),
    ("exact", 1, 0, (1, 0, 0, 0)),
    ("at_least", 1, 0.5, (1, 0, 0, 0)),
])
def test_target_seed_handles_earliest_expensive_deadline(count, mode, initial, goal, usage):
    params = request(prices=(10, 0.01, 0.01, 0.01), usage=usage, initial=initial,
                     target={"timeslot": 0, "soc_kwh": goal, "mode": mode})
    actual = factory(max_evaluations=count, seed=0)(params)
    assert actual["diagnostics"]["evaluation"]["score"][0] == 0
    assert actual["diagnostics"]["evaluations"] <= count
    assert actual["diagnostics"]["seed_candidates"][0].startswith("target-")


@pytest.mark.parametrize("count", [1, 48])
def test_reserve_recovers_immediately_under_tiny_and_selected_budgets(count):
    params = request(prices=(10, 0.01, 0.01, 0.01), usage=(0,) * 4)
    battery = params.battery_entities[0]
    battery.minimum_kwh = 1.0
    battery.charge_curve_kwh = [0.4]
    actual = factory(max_evaluations=count, seed=0)(params)
    assert policies(actual)[0][:3] == ["grid_charge"] * 3
    assert [slot["level"] for slot in actual["entities"][0]["schedule"]][:3] == pytest.approx([0.4, 0.8, 1.2])
    assert actual["projections"]["projected_cost"] < 5
    assert actual["fitness"] > 1000
    assert "battery_min_unmet" in actual["problems"]
    assert actual["diagnostics"]["evaluation"]["score"] == list(evaluate_schedule(params, policies(actual)).score)


def test_pv_only_reserve_recovery_respects_mask_and_real_energy():
    params = request(prices=(10,) * 4, usage=(0,) * 4, solar=(0.4,) * 4)
    battery = params.battery_entities[0]
    battery.minimum_kwh = 1.0
    battery.can_charge_from = 2
    battery.charge_curve_kwh = [0.4]
    actual = factory(max_evaluations=1)(params)
    assert "grid_charge" not in policies(actual)[0]
    assert [slot["level"] for slot in actual["entities"][0]["schedule"]] == pytest.approx([0.4, 0.8, 1.2, 1.6])
    assert actual["projections"]["projected_cost"] == 0
