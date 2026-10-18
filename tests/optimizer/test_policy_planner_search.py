"""Shared search guarantees; native HiGHS and passive policies have other semantics.

Public-contract checks for every algorithm live in test_planner_contract and
test_live_planner_parity. These cases cover the common discrete optimizing
planners, with budget subsets explicit where their mechanisms differ. Assertions concern published behavior rather than internal search mechanics.
"""

import pytest

from custom_components.wattplan.optimizer.planners import create_planner
from custom_components.wattplan.optimizer.planners import evaluation as evaluator
from custom_components.wattplan.optimizer.planners.registry import BUILTINS
from tests.optimizer.planner_conformance import (
    assert_conforms, battery_policies, conformance_params, economic_params,
)

POLICY_PLANNERS = [name for name in BUILTINS if name != "highs"]
POLICY_SEARCHES = [name for name in POLICY_PLANNERS
                  if create_planner(name).capabilities.economic_scope == "household"]
TARGET_SEARCHES = [name for name in POLICY_PLANNERS
                  if create_planner(name).capabilities.targets == "optimized"]


@pytest.fixture(params=POLICY_SEARCHES)
def policy_planner(request):
    # Alternate seeds exercise reproducibility beyond the published seed-zero presets.
    options = {"seed": 11} if request.param in {"local-search", "genetic", "cross-entropy"} else {}
    return create_planner(request.param, options)


@pytest.fixture(params=TARGET_SEARCHES)
def target_planner(request):
    return create_planner(request.param)


@pytest.fixture(params=POLICY_PLANNERS)
def any_policy_planner(request):
    return create_planner(request.param)


def test_simple_cheap_charge_then_expensive_discharge(policy_planner):
    result = policy_planner(economic_params())
    assert result["projections"]["projected_cost"] == pytest.approx(0.1)
    assert battery_policies(result)[0][:2] == ["grid_charge", "self_consume"]
    assert result["projections"]["final_soc_kwh"] == {"house": 0}


def test_signed_tariffs_are_scored_from_the_executed_policy(policy_planner):
    params = economic_params(prices=(-1, 2), usage=(0, 1), solar=(0.5, 0))
    params.grid_export_price_per_kwh = [-2, -0.5, 0, 0]
    result = policy_planner(params)
    evaluated = evaluator.evaluate_schedule(params, battery_policies(result))
    assert evaluated.tariff_cost < 0
    assert result["projections"]["projected_cost"] == pytest.approx(evaluated.tariff_cost)


@pytest.mark.parametrize("mode,initial,goal,usage", [
    ("at_least", 0, 2, (0, 0)), ("exact", 0, 1, (0, 0)),
    ("at_most", 2, 0, (1, 1)), ("exact", 2, 0, (1, 1)),
])
def test_reachable_target_modes_take_priority_over_tariff_cost(target_planner, mode, initial, goal, usage):
    params = economic_params(prices=(10, 10), usage=usage, initial=initial,
                             target={"timeslot": 1, "soc_kwh": goal, "mode": mode})
    result = target_planner(params)
    assert result["diagnostics"]["evaluation"]["score"][0] == 0
    assert "battery_target_unmet" not in result["suboptimal_reasons"]


def test_inclusive_deadline_cannot_borrow_energy_from_a_later_cheap_slot(any_policy_planner):
    params = economic_params(prices=(100, -100), usage=(0, 0),
                             target={"timeslot": 0, "soc_kwh": 2, "mode": "exact"})
    result = any_policy_planner(params)
    check = result["diagnostics"]["evaluation"]["target_checks"][0]
    if any_policy_planner.capabilities.targets == "optimized":
        assert check["soc_kwh"] == 1
        assert check["miss_kwh"] == 1
    else:
        assert check["soc_kwh"] == 0
        assert check["miss_kwh"] == 2
    assert "battery_target_unmet" in result["suboptimal_reasons"]


def test_repeatability_input_isolation_and_changed_requests(any_policy_planner):
    policy_planner = any_policy_planner
    params = conformance_params()[2]
    before = params.model_dump(mode="json")
    first, second = policy_planner(params), policy_planner(params)
    assert first["entities"] == second["entities"]
    assert first["diagnostics"]["evaluation"] == second["diagnostics"]["evaluation"]
    assert params.model_dump(mode="json") == before
    changed = economic_params(prices=(5, -3), usage=(1, 0), initial=2,
                              target={"timeslot": 0, "soc_kwh": 1, "mode": "exact"})
    after = policy_planner(changed)
    if policy_planner.capabilities.targets == "optimized":
        assert after["diagnostics"]["evaluation"]["score"][0] == 0
    else:
        assert any("targets are measured but not optimized" in note
                   for note in after["diagnostics"]["planner"]["notes"])
    assert after["projections"]["projected_cost"] == pytest.approx(
        evaluator.evaluate_schedule(changed, battery_policies(after)).tariff_cost
    )
    assert any("lookahead_slots" in note for note in after["diagnostics"]["planner"]["notes"])


@pytest.mark.parametrize("name", [name for name in POLICY_PLANNERS
    if create_planner(name).capabilities.economic_scope != "household"])
def test_specialized_controls_leave_metadata_free_household_passive_and_report_actual_cost(name):
    planner = create_planner(name)
    params = economic_params()
    result = planner(params)
    rows = battery_policies(result)
    assert rows == [["self_consume"] * len(params.usage_kwh)]
    evaluated = evaluator.evaluate_schedule(params, rows)
    assert result["projections"]["projected_cost"] == pytest.approx(evaluated.tariff_cost)
    assert result["diagnostics"]["evaluation"]["score"] == pytest.approx(evaluated.score)
    assert planner.capabilities.targets == "observed"
    assert planner.capabilities.reserve_recovery is False


@pytest.mark.parametrize("name", POLICY_PLANNERS)
def test_curves_and_preferences_preserve_physical_scoring(name):
    params = conformance_params()[1]
    params.battery_entities[0].charge_curve_kwh = [1, 0.5, 0.2]
    params.battery_entities[0].discharge_curve_kwh = [0.2, 0.8, 1]
    params.throughput_cost_per_kwh = 2
    params.mode_switch_cost = 0.7
    result = create_planner(name)(params)
    evaluated = evaluator.evaluate_schedule(params, battery_policies(result))
    assert result["diagnostics"]["evaluation"]["score"] == pytest.approx(evaluated.score)


@pytest.mark.parametrize("name,option", [
    (name, "max_evaluations")
    for name in ("greedy", "local-search", "charge-window", "genetic", "cross-entropy")
] + [(name, "max_expansions")
     for name in ("beam-search", "dynamic-programming", "mobility-fleet-beam")])
@pytest.mark.parametrize("limit", [1, 8])
def test_bounded_search_still_returns_complete_physical_plan(name, option, limit):
    params = economic_params(prices=(10, 9, 8, 1), usage=(0,) * 4)
    params.battery_entities[0].minimum_kwh = 1.0
    params.battery_entities[0].charge_curve_kwh = [0.4]
    result = assert_conforms(name, params, {option: limit})
    counter = "expansions" if option == "max_expansions" else "evaluations"
    assert 0 <= result["diagnostics"][counter] <= limit
    # Stopping early may leave reserve unmet, but cannot invent recovery energy
    # or conceal a shortfall in the accepted plan.
    levels = [point["level"] for point in result["entities"][0]["schedule"]]
    assert all(level <= 0.4 * (index + 1) + 1e-6 for index, level in enumerate(levels))
    shortfall = any(level < params.battery_entities[0].minimum_kwh - 1e-6 for level in levels)
    assert ("battery_min_unmet" in result["suboptimal_reasons"]) is shortfall
