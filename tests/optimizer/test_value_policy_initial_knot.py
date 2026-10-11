"""Exact initial table node removes a measured idle-state interpolation error."""

from itertools import product
import math

import pytest

from custom_components.wattplan.optimizer.planners.evaluation import evaluate_schedule
from custom_components.wattplan.optimizer.planners.hybrid_policy import factory as hybrid_factory
from custom_components.wattplan.optimizer.planners.value_policy import factory
from tests.optimizer.test_dynamic_programming_planner import policies, request
from tests.optimizer.test_value_policy_paired_demand import joint_oracle


@pytest.mark.parametrize("planner", [factory, hybrid_factory])
@pytest.mark.parametrize("value", [0, 1, 0.0, None, "true"])
def test_initial_knot_requires_boolean(planner, value):
    with pytest.raises(ValueError, match="include_initial_knot"):
        planner(include_initial_knot=value)


@pytest.mark.parametrize("planner", [factory, hybrid_factory])
@pytest.mark.parametrize("options", [
    {}, dict(pv_scenario_spread=1.0, include_charge_knots=True),
    dict(pv_scenario_spread=1.0, paired_demand_scenario_spread=0.25,
         paired_demand_scenario_slots=1, first_action_rollout=True),
    dict(pv_scenario_spread=1.0, first_action_rollout=True, rollout_pv_persistence=True),
])
def test_default_preserves_entire_result(planner, options, monkeypatch):
    monkeypatch.setattr("custom_components.wattplan.optimizer.planners.shared.perf_counter", lambda: 0.0)
    monkeypatch.setattr("custom_components.wattplan.optimizer.planners.reporting.perf_counter", lambda: 0.0)
    params = request(initial=1.123, pv=(0.4, 0.0, 0.7, 0.0))
    options = dict(energy_intervals=6, grid_charge_premium_per_kwh=0.03, **options)
    assert planner(**options)(params) == planner(**options, include_initial_knot=False)(params)


@pytest.mark.parametrize("standard", [False, True])
@pytest.mark.parametrize("rollout,demand", [(False, 0.0), (False, 0.25), (True, 0.25)])
def test_measured_idle_state_case_matches_independent_zero_cost_oracle(standard, rollout, demand):
    params = request(initial=0.8, prices=(0.2, 10.0, 10.0, 1.0), usage=(0.0, 0.0, 0.0, 0.8))
    grid = 2
    if standard:
        battery = params.battery_entities[0]
        battery.capacity_kwh, battery.minimum_kwh, battery.initial_kwh = 10.0, 1.0, 2.5
        battery.charge_curve_kwh = battery.discharge_curve_kwh = [5.0]
        battery.charge_efficiency = battery.discharge_efficiency = 0.9
        params.usage_kwh[-1], params.grid_import_price_per_kwh[0] = 1.08, 0.01
        grid = 6
    elif demand:
        params.usage_kwh[-1] = 0.64
    options = dict(energy_intervals=grid, pv_scenario_spread=1.0,
        paired_demand_scenario_spread=demand, paired_demand_scenario_slots=12,
        first_action_rollout=rollout, grid_charge_premium_per_kwh=0.03)
    before = factory(**options)(params)
    result = factory(**options, include_initial_knot=True)(params)
    assert policies(before)[0][0] == "grid_charge"
    assert policies(result)[0][0] == "self_consume"
    oracle, action = joint_oracle(params, pv_spread=1.0, demand_spread=demand, premium=0.03)
    assert oracle == pytest.approx((0.0, 0.0))
    assert action == "self_consume"
    replay = evaluate_schedule(params, policies(result))
    assert replay.tariff_cost == pytest.approx(0.0)
    assert sum(sum(step.grid_charge) for step in replay.steps) == 0.0
    assert result["diagnostics"]["initial_value_policy_expected_score"] == pytest.approx(oracle)
    assert result["diagnostics"]["energy_knots"] == before["diagnostics"]["energy_knots"] + 1


@pytest.mark.parametrize("initial", [0.0, 1.0, 3.0, math.nextafter(1.0, 2.0), 0.2])
def test_exact_dedup_edges_near_node_and_below_reserve(initial):
    params = request(initial=initial, usage=(0.0, 0.0, 0.0, 0.0))
    params.battery_entities[0].minimum_kwh = 0.5
    snapshot = params.model_dump()
    before = factory(energy_intervals=3)(params)
    result = factory(energy_intervals=3, include_initial_knot=True)(params)
    added = initial not in (0.0, 0.5, 1.0, 2.0, 3.0)
    diag = result["diagnostics"]
    assert diag["initial_knot_added"] is added
    assert diag["initial_knot_value_kwh"] == initial
    assert diag["initial_knot_evaluated"] is True
    assert diag["initial_knot_applied"] is True
    assert diag["energy_knots"] == before["diagnostics"]["energy_knots"] + int(added)
    assert all(math.isfinite(v) for v in diag["evaluation"]["score"])
    assert params.model_dump() == snapshot
    assert diag["evaluation"]["score"] == pytest.approx(evaluate_schedule(params, policies(result)).score)


def test_existing_target_or_charge_node_is_not_added_twice():
    for params, options in [
        (request(initial=0.8, target=dict(timeslot=3, soc_kwh=0.8, mode="at_least")), {}),
        (request(initial=2.0), dict(include_charge_knots=True)),
    ]:
        result = factory(energy_intervals=2, include_initial_knot=True, **options)(params)
        before = factory(energy_intervals=2, **options)(params)
        assert result["diagnostics"]["initial_knot_added"] is False
        assert result["diagnostics"]["energy_knots"] == before["diagnostics"]["energy_knots"]
        assert result["entities"] == before["entities"]


def test_target_modes_switches_and_premium_retain_original_physics_score():
    params = request(initial=0.8, prices=(0.2, 1.0, 0.5, 0.0), usage=(0.0, 0.0, 0.8, 0.0),
        target=dict(timeslot=1, soc_kwh=1.8, mode="at_least"))
    params.mode_switch_cost, params.throughput_cost_per_kwh = 0.07, 0.03
    result = factory(energy_intervals=15, include_initial_knot=True, grid_charge_premium_per_kwh=0.04)(params)
    def score(actions):
        replay = evaluate_schedule(params, [actions])
        return replay.score[0], replay.objective + 0.04 * sum(sum(s.grid_charge) for s in replay.steps)
    optimal = min(score(actions) for actions in product(("self_consume", "preserve", "grid_charge"), repeat=4))
    assert score(policies(result)[0]) == pytest.approx(optimal)
    assert result["diagnostics"]["evaluation"]["score"] == pytest.approx(evaluate_schedule(params, policies(result)).score)


def test_hybrid_forwarding_and_passive_fleet_omissions():
    params = request(initial=0.8)
    options = dict(energy_intervals=6, pv_scenario_spread=1.0, include_initial_knot=True)
    value = factory(**options)(params)
    hybrid = hybrid_factory(**options)(params)
    assert hybrid["entities"] == value["entities"]
    assert hybrid["diagnostics"]["hybrid_gate"]["initial_knot_applied"] is True
    for params, planner in [(request(batteries=2), factory),
                            (request(pv=(0.0, 0.0, 10.0, 0.0)), hybrid_factory)]:
        result = planner(include_initial_knot=True)(params)
        before = planner()(params)
        assert result["entities"] == before["entities"]
        assert result["projections"] == before["projections"]
        diag = result["diagnostics"].get("hybrid_gate", result["diagnostics"])
        assert diag["initial_knot_evaluated"] is False
        assert diag["initial_knot_added"] is False
        assert diag["initial_knot_applied"] is False


def test_accepted_repair_reports_anchor_evaluated_but_replaced():
    params = request(initial=2.1, prices=(1.2, 0.3, 1.2, 0.3),
        usage=(0.0, 0.0, 1.1, 1.1), pv=(0.0, 0.2, 0.0, 0.2),
        target=dict(timeslot=3, soc_kwh=1.1, tolerance_kwh=0.02, mode="exact"))
    result = factory(energy_intervals=4, paired_demand_scenario_spread=0.25, include_initial_knot=True)(params)
    diag = result["diagnostics"]
    assert diag["target_repair"]["improved"] is True
    assert diag["initial_knot_evaluated"] is True
    assert diag["initial_knot_added"] is True
    assert diag["initial_knot_applied"] is False
    assert diag["target_repair"]["initial_knot_applied"] is False


@pytest.mark.parametrize("options", [
    dict(pv_scenario_spread=1.0, paired_demand_scenario_spread=0.25,
         paired_demand_scenario_slots=1, first_action_rollout=True),
    dict(pv_scenario_spread=1.0, first_action_rollout=True, rollout_pv_persistence=True),
])
def test_enabled_combined_models_replay_original_inputs_and_ignore_date(options):
    params = request(initial=1.123, pv=(0.4, 0.0, 0.7, 0.0))
    snapshot = params.model_dump()
    planner = factory(energy_intervals=6, include_initial_knot=True,
        include_charge_knots=True, grid_charge_premium_per_kwh=0.03, **options)
    result = planner(params)
    assert params.model_dump() == snapshot
    assert result["diagnostics"]["evaluation"]["score"] == pytest.approx(evaluate_schedule(params, policies(result)).score)
    shifted = params.model_copy(update={"plan_start": params.plan_start.replace(year=2027)})
    assert policies(planner(shifted)) == policies(result)
