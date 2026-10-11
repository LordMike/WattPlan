"""Paired demand/PV scenarios: causal joint oracle and unchanged reporting."""

from functools import lru_cache

import pytest

from custom_components.wattplan.optimizer.physics import EPSILON, transition
from custom_components.wattplan.optimizer.planners.evaluation import evaluate_schedule, target_miss
from custom_components.wattplan.optimizer.planners.hybrid_policy import factory as hybrid_factory
from custom_components.wattplan.optimizer.planners.recovery import reserve_shortfall_cost
from custom_components.wattplan.optimizer.planners.value_policy import factory
from tests.optimizer.test_dynamic_programming_planner import policies, request
from tests.optimizer.test_value_policy_rollout import kernel_request


def joint_oracle(params, *, pv_spread, demand_spread, slots=None, premium=0.0, clairvoyant_slot=None):
    """Enumerate current actions before joint outcomes; observe prior states only."""
    battery = params.battery_entities[0]
    choices = ("self_consume", "preserve", "grid_charge") if battery.can_charge_from & 1 else ("self_consume", "preserve")

    @lru_cache(None)
    def solve(slot, level, previous):
        if slot == len(params.usage_kwh):
            return (0.0, 0.0), None
        spread = demand_spread if slots is None or slot < slots else 0.0
        factors = ((1 - pv_spread, 1 + spread), (1 + pv_spread, 1 - spread))
        action_branches = []
        for action in choices:
            branches = []
            for pv_factor, demand_factor in factors:
                step = transition([battery], [level], [action],
                    usage=params.usage_kwh[slot] * demand_factor,
                    pv=params.solar_input_kwh[slot] * pv_factor)
                mode = (1 if step.charge > EPSILON and step.charge >= params.action_deadband_kwh
                        else -1 if sum(step.discharge) > EPSILON and sum(step.discharge) >= params.action_deadband_kwh else 0)
                miss = target_miss(battery.target, step.levels[0]) if battery.target and battery.target.timeslot == slot else 0.0
                future, _ = solve(slot + 1, step.levels[0], mode)
                cost = (step.grid_import * params.grid_import_price_per_kwh[slot]
                        - step.grid_export * params.grid_export_price_per_kwh[slot]
                        + (step.charge + sum(step.discharge)) * params.throughput_cost_per_kwh
                        + (mode != previous) * params.mode_switch_cost
                        + reserve_shortfall_cost([battery], step.levels)
                        + sum(step.grid_charge) * premium)
                branches.append((miss + future[0], cost + future[1]))
            action_branches.append(branches)
        if slot == clairvoyant_slot:
            selected = [min(rows[b] for rows in action_branches) for b in (0, 1)]
            return tuple((selected[0][k] + selected[1][k]) / 2 for k in (0, 1)), None
        expected = [tuple((rows[0][k] + rows[1][k]) / 2 for k in (0, 1)) for rows in action_branches]
        best = min(range(len(choices)), key=lambda index: expected[index])
        return expected[best], choices[best]

    return solve(0, battery.initial_kwh, 0)


@pytest.mark.parametrize("planner", [factory, hybrid_factory])
@pytest.mark.parametrize("spread", [-0.1, 1.1, True, float("nan"), float("inf"), None])
def test_invalid_demand_spread_rejected(planner, spread):
    with pytest.raises(ValueError, match="paired_demand_scenario_spread"):
        planner(paired_demand_scenario_spread=spread)


@pytest.mark.parametrize("bound", [0, -1, True, False, 1.5, "12"])
def test_invalid_slot_bound_rejected_even_when_spread_zero(bound):
    with pytest.raises(ValueError, match="paired_demand_scenario_slots"):
        hybrid_factory(paired_demand_scenario_slots=bound)


@pytest.mark.parametrize("planner", [factory, hybrid_factory])
@pytest.mark.parametrize("pv_spread,rollout,bound", [(0.0, False, 12), (1.0, False, None), (1.0, True, 1)])
def test_zero_demand_spread_preserves_entire_result_with_any_valid_bound(planner, pv_spread, rollout, bound, monkeypatch):
    monkeypatch.setattr("custom_components.wattplan.optimizer.planners.shared.perf_counter", lambda: 0.0)
    monkeypatch.setattr("custom_components.wattplan.optimizer.planners.reporting.perf_counter", lambda: 0.0)
    params = request(initial=1.123, pv=(0.4, 0.0, 0.7, 0.0))
    options = dict(energy_intervals=6, pv_scenario_spread=pv_spread,
                   first_action_rollout=rollout, grid_charge_premium_per_kwh=0.03)
    assert planner(**options)(params) == planner(**options, paired_demand_scenario_spread=0.0,
        paired_demand_scenario_slots=bound)(params)


@pytest.mark.parametrize("pv_spread,bound,ingress,target", [
    (1.0, None, 3, None), (0.0, None, 3, None), (1.0, 1, 3, None),
    (1.0, None, 2, None), (1.0, 2, 3, dict(timeslot=1, soc_kwh=1.0, mode="at_least")),
])
def test_joint_score_matches_independent_aligned_state_oracle(pv_spread, bound, ingress, target):
    params = request(initial=0.75, prices=(0.4, 1.0, 0.0, 0.0),
                     usage=(0.5, 0.5, 0.0, 0.0), pv=(0.25, 0.25, 0.0, 0.0),
                     ingress=ingress, target=target)
    params.mode_switch_cost = 0.37
    params.throughput_cost_per_kwh = 0.13
    params.action_deadband_kwh = 0.3
    expected, _ = joint_oracle(params, pv_spread=pv_spread, demand_spread=0.5, slots=bound, premium=0.03)
    result = factory(energy_intervals=12, pv_scenario_spread=pv_spread,
        paired_demand_scenario_spread=0.5, paired_demand_scenario_slots=bound,
        grid_charge_premium_per_kwh=0.03)(params)
    assert result["diagnostics"]["initial_value_policy_expected_score"] == pytest.approx(expected)
    assert result["diagnostics"]["pv_scenario_weights"] == [0.5, 0.5]
    assert result["diagnostics"]["paired_demand_scenario_factors"] == [1.5, 0.5]
    replay = evaluate_schedule(params, policies(result))
    assert result["diagnostics"]["evaluation"]["score"] == pytest.approx(replay.score)
    assert result["projections"]["projected_cost"] == pytest.approx(replay.tariff_cost)


def test_rollout_cannot_choose_current_action_after_joint_branch():
    params = kernel_request(initial=1.0, prices=(1.0, 3.0), usage=(1.0, 0.5), pv=(0.25, 0.0))
    expected, _ = joint_oracle(params, pv_spread=1.0, demand_spread=0.25)
    clairvoyant, _ = joint_oracle(params, pv_spread=1.0, demand_spread=0.25, clairvoyant_slot=0)
    assert expected == pytest.approx((0.0, 0.75))
    assert clairvoyant == pytest.approx((0.0, 0.625))
    result = factory(energy_intervals=2, first_action_rollout=True, pv_scenario_spread=1.0,
                     paired_demand_scenario_spread=0.25)(params)
    assert result["diagnostics"]["initial_rollout_search_score"] == pytest.approx(expected)


def test_rollout_next_action_precedes_its_joint_branch_and_respects_bound():
    params = request(initial=1.0, ingress=2, prices=(0.0, 1.0, 3.0, 0.0),
                     usage=(0.0, 1.0, 0.5, 0.0), pv=(0.0, 0.25, 0.0, 0.0))
    expected, _ = joint_oracle(params, pv_spread=1.0, demand_spread=0.25, slots=2)
    clairvoyant, _ = joint_oracle(params, pv_spread=1.0, demand_spread=0.25, slots=2, clairvoyant_slot=1)
    assert expected == pytest.approx((0.0, 0.75))
    assert clairvoyant == pytest.approx((0.0, 0.625))
    result = factory(energy_intervals=12, first_action_rollout=True, pv_scenario_spread=1.0,
        paired_demand_scenario_spread=0.25, paired_demand_scenario_slots=2)(params)
    assert result["diagnostics"]["initial_rollout_search_score"] == pytest.approx(expected)


def test_rollout_bound_one_restores_nominal_second_slot_demand_but_keeps_pv_branches():
    params = kernel_request(initial=0.713, prices=(0.4, 1.0), usage=(0.43, 0.81), pv=(0.37, 0.29))
    params.mode_switch_cost = 0.37
    params.throughput_cost_per_kwh = 0.13
    expected, _ = joint_oracle(params, pv_spread=1.0, demand_spread=0.5, slots=1, premium=0.03)
    result = factory(energy_intervals=2, first_action_rollout=True, pv_scenario_spread=1.0,
        paired_demand_scenario_spread=0.5, paired_demand_scenario_slots=1,
        grid_charge_premium_per_kwh=0.03)(params)
    assert result["diagnostics"]["initial_rollout_search_score"] == pytest.approx(expected)
    assert result["diagnostics"]["rollout_search_transitions"] == 36


def test_reserve_recovery_and_charge_saturation_use_each_branch_physics():
    params = request(initial=0.25, prices=(0.4, 1.0, 0.0, 0.0),
        usage=(0.5, 0.5, 0.0, 0.0), pv=(0.25, 0.25, 0.0, 0.0))
    params.battery_entities[0].minimum_kwh = 0.5
    expected, _ = joint_oracle(params, pv_spread=1.0, demand_spread=0.5, premium=0.03)
    result = factory(energy_intervals=12, pv_scenario_spread=1.0,
        paired_demand_scenario_spread=0.5, grid_charge_premium_per_kwh=0.03)(params)
    assert result["diagnostics"]["initial_value_policy_expected_score"] == pytest.approx(expected)
    replay = evaluate_schedule(params, policies(result))
    assert result["diagnostics"]["evaluation"]["score"] == pytest.approx(replay.score)


def test_expected_joint_target_risk_does_not_trigger_nominal_repair():
    params = request(initial=0.5, ingress=2, usage=(0.5, 0.0, 0.0, 0.0),
        target=dict(timeslot=0, soc_kwh=0.0, tolerance_kwh=0.02, mode="exact"))
    result = factory(energy_intervals=12, paired_demand_scenario_spread=0.5)(params)
    assert result["diagnostics"]["initial_value_policy_expected_score"][0] == pytest.approx(0.115)
    assert result["diagnostics"]["evaluation"]["score"][0] == 0.0
    assert result["diagnostics"]["target_repair"] is None


@pytest.mark.parametrize("pv_scale,demand_scale,usage,pv,extra", [
    (1.0, 1.0, (0.5,) * 4, (0.0,) * 4, 15),
    (1.0, 1.0, (0.0,) * 4, (0.0,) * 4, 0),
    (1.0, 0.0, (0.5,) * 4, (0.0,) * 4, 0),
    (0.0, 1.0, (0.5,) * 4, (0.5,) * 4, 15),
])
def test_zero_pv_demand_branches_and_truly_identical_inputs_deduplicate(pv_scale, demand_scale, usage, pv, extra):
    params = request(usage=usage, pv=pv)
    options = dict(energy_intervals=3, pv_scale=pv_scale, demand_scale=demand_scale)
    ordinary = factory(**options)(params)
    paired = factory(**options, paired_demand_scenario_spread=0.25, paired_demand_scenario_slots=1)(params)
    # Four knots, three actions, plus the one forward action: one extra branch
    # only at relative slot zero contributes (4 + 1) * 3 transitions.
    assert paired["diagnostics"]["search_transitions"] - ordinary["diagnostics"]["search_transitions"] == extra


def test_bound_keeps_full_pv_and_planning_horizon_and_reports_slot_hours():
    params = request(usage=(0.5,) * 4, pv=(0.25,) * 4)
    paired = factory(energy_intervals=3, pv_scenario_spread=1.0,
        paired_demand_scenario_spread=0.5, paired_demand_scenario_slots=1)(params)
    assert paired["diagnostics"]["paired_demand_scenario_effective_slots"] == 1
    assert paired["diagnostics"]["paired_demand_scenario_effective_hours"] == 0.25
    assert paired["diagnostics"]["forecast_horizon_slots"] == 4
    assert len(policies(paired)[0]) == 4
    assert paired["diagnostics"]["scenario_branch_evaluations"] == 40
    clamped = factory(paired_demand_scenario_spread=0.25, paired_demand_scenario_slots=12)(params)
    assert clamped["diagnostics"]["paired_demand_scenario_effective_slots"] == 4
    assert clamped["diagnostics"]["paired_demand_scenario_effective_hours"] == 1.0


def test_per_branch_grid_charge_premium_and_target_priority_leave_reporting_unadjusted():
    params = kernel_request(prices=(0.1, 0.0), usage=(0.5, 0.0), pv=(0.5, 0.0),
        target=dict(timeslot=0, soc_kwh=1.0, mode="at_least"))
    expected, _ = joint_oracle(params, pv_spread=1.0, demand_spread=0.5, premium=100.0)
    result = factory(energy_intervals=12, pv_scenario_spread=1.0,
        paired_demand_scenario_spread=0.5, grid_charge_premium_per_kwh=100.0)(params)
    # Low branch household demand .75 and grid battery charge1; high branch
    # PV1, household demand.25, grid battery charge.25. Average grid charge.625.
    assert expected == pytest.approx((0.0, 62.6))
    assert result["diagnostics"]["initial_value_policy_expected_score"] == pytest.approx(expected)
    assert policies(result)[0][0] == "grid_charge"
    assert result["projections"]["projected_cost"] == pytest.approx(0.1)


def test_fleet_and_accepted_target_repair_disclose_uncertainty_omission():
    fleet = request(batteries=2)
    ordinary = factory()(fleet)
    paired = factory(paired_demand_scenario_spread=0.25)(fleet)
    assert paired["entities"] == ordinary["entities"]
    assert paired["diagnostics"]["paired_demand_scenario_evaluated"] is False
    assert paired["diagnostics"]["paired_demand_scenario_applied"] is False
    params = request(initial=2.1, prices=(1.2, 0.3, 1.2, 0.3),
        usage=(0.0, 0.0, 1.1, 1.1), pv=(0.0, 0.2, 0.0, 0.2),
        target=dict(timeslot=3, soc_kwh=1.1, tolerance_kwh=0.02, mode="exact"))
    repaired = factory(energy_intervals=4, paired_demand_scenario_spread=0.25)(params)
    assert repaired["diagnostics"]["target_repair"]["improved"] is True
    assert repaired["diagnostics"]["paired_demand_scenario_evaluated"] is True
    assert repaired["diagnostics"]["paired_demand_scenario_applied"] is False
    assert repaired["diagnostics"]["target_repair"]["paired_demand_scenario_applied"] is False


def test_hybrid_forwarding_input_isolation_calendar_independence_and_passive_omission():
    params = request(pv=(0.0, 0.4, 0.0, 0.0))
    before = params.model_dump()
    options = dict(energy_intervals=6, pv_scenario_spread=1.0,
        paired_demand_scenario_spread=0.25, paired_demand_scenario_slots=1,
        grid_charge_premium_per_kwh=0.03, first_action_rollout=True)
    direct = factory(**options)(params)
    hybrid = hybrid_factory(**options)(params)
    assert hybrid["entities"] == direct["entities"]
    assert hybrid["projections"] == direct["projections"]
    assert hybrid["diagnostics"]["hybrid_gate"]["paired_demand_scenario_applied"] is True
    assert params.model_dump() == before
    shifted = params.model_copy(update={"plan_start": params.plan_start.replace(month=1)})
    assert hybrid_factory(**options)(shifted)["entities"] == hybrid["entities"]
    passive_params = request(pv=(0.0, 0.0, 10.0, 0.0))
    passive = hybrid_factory(**options)(passive_params)
    ordinary = hybrid_factory()(passive_params)
    assert passive["entities"] == ordinary["entities"]
    assert passive["projections"] == ordinary["projections"]
    assert passive["diagnostics"]["hybrid_gate"]["paired_demand_scenario_applied"] is False
