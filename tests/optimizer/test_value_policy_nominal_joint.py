"""Independent exact causal oracle for bounded three-outcome joint uncertainty."""

from functools import lru_cache

import pytest

from custom_components.wattplan.optimizer.physics import EPSILON, transition
from custom_components.wattplan.optimizer.planners.evaluation import evaluate_schedule, target_miss
from custom_components.wattplan.optimizer.planners.hybrid_policy import factory as hybrid_factory
from custom_components.wattplan.optimizer.planners.recovery import reserve_shortfall_cost
from custom_components.wattplan.optimizer.planners.value_policy import factory
from tests.optimizer.test_dynamic_programming_planner import policies, request


OPTIONS = dict(pv_scenario_spread=1.0, paired_demand_scenario_spread=0.25,
               paired_demand_scenario_slots=2, include_nominal_joint_scenario=True)


def mixture_oracle(params, *, cap=6, bound=2, spread=1.0, demand=0.25,
                   q=0.5, premium=0.0, clairvoyant_slot=None):
    """Enumerate physical actions before outcomes; no interpolation or policy code."""
    battery = params.battery_entities[0]
    actions = ("self_consume", "preserve", "grid_charge") if battery.can_charge_from & 1 else ("self_consume", "preserve")

    @lru_cache(None)
    def solve(slot, level, previous):
        if slot == len(params.usage_kwh):
            return (0.0, 0.0), None
        if slot < min(cap, bound):
            outcomes = ((0.25, 1 - spread, 1 + demand), (0.5, 1.0, 1.0),
                        (0.25, 1 + spread, 1 - demand))
        elif slot < bound:
            outcomes = ((0.5, 1 - spread, 1 + demand), (0.5, 1 + spread, 1 - demand))
        else:
            outcomes = ((q, 1 - spread, 1.0), (1 - q, 1 + q * spread / (1 - q), 1.0))
        scores = []
        for action in actions:
            branches = []
            for weight, p, d in outcomes:
                step = transition([battery], [level], [action],
                    usage=params.usage_kwh[slot] * d, pv=params.solar_input_kwh[slot] * p)
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
                branches.append((weight, (miss + future[0], cost + future[1])))
            scores.append(branches)
        if slot == clairvoyant_slot:
            return tuple(sum(outcomes[b][0] * min(rows[b][1] for rows in scores)[k]
                             for b in range(len(outcomes))) for k in (0, 1)), None
        expected = [tuple(sum(w * row[k] for w, row in rows) for k in (0, 1)) for rows in scores]
        best = min(range(len(actions)), key=lambda i: expected[i])
        return expected[best], actions[best]

    return solve(0, battery.initial_kwh, 0)


@pytest.mark.parametrize("planner", [factory, hybrid_factory])
@pytest.mark.parametrize("value", [0, 1, None, 0.0, "true"])
def test_flag_requires_boolean(planner, value):
    with pytest.raises(ValueError, match="include_nominal_joint_scenario"):
        planner(include_nominal_joint_scenario=value)


@pytest.mark.parametrize("planner", [factory, hybrid_factory])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, None, "6"])
def test_slot_cap_requires_positive_integer_even_disabled(planner, value):
    with pytest.raises(ValueError, match="nominal_joint_scenario_slots"):
        planner(nominal_joint_scenario_slots=value)


@pytest.mark.parametrize("change", [dict(pv_scenario_spread=0), dict(paired_demand_scenario_spread=0),
    dict(paired_demand_scenario_slots=None), dict(first_action_rollout=True),
    dict(first_action_rollout=True, rollout_pv_persistence=True)])
def test_explicit_dependencies_and_rollout_rejection(change):
    with pytest.raises(ValueError):
        hybrid_factory(**dict(OPTIONS, **change))


@pytest.mark.parametrize("planner", [factory, hybrid_factory])
@pytest.mark.parametrize("extra", [{}, dict(first_action_rollout=True),
    dict(include_discharge_budget_knot=True, include_initial_knot=True),
    dict(pv_tail_low_probability=1 / 3)])
def test_default_false_whole_output_parity_and_irrelevant_cap(planner, extra, monkeypatch):
    monkeypatch.setattr("custom_components.wattplan.optimizer.planners.shared.perf_counter", lambda: 0.0)
    monkeypatch.setattr("custom_components.wattplan.optimizer.planners.reporting.perf_counter", lambda: 0.0)
    params = request(initial=1.123, pv=(0.4, 0.0, 0.7, 0.0))
    options = dict(OPTIONS, **extra)
    options.pop("include_nominal_joint_scenario")
    assert planner(**options)(params) == planner(**options, include_nominal_joint_scenario=False,
        nominal_joint_scenario_slots=100)(params)


@pytest.mark.parametrize("cap,first,score", [(1, "preserve", 2.325), (2, "self_consume", 1.590625)])
def test_saved_public_request_distinct_causal_inventory_choice(cap, first, score):
    params = request(initial=1.0, prices=(1.7, 5.0, 0.0, 0.0),
        usage=(1.0, 1.0, 0.0, 0.0), pv=(0.0, 1.0, 0.0, 0.0))
    params.battery_entities[0].capacity_kwh = 1.0
    expected, action = mixture_oracle(params, cap=cap, premium=0.03)
    assert action == first
    assert expected == pytest.approx((0.0, score))
    result = factory(energy_intervals=4, nominal_joint_scenario_slots=cap,
        grid_charge_premium_per_kwh=0.03, **OPTIONS)(params)
    assert policies(result)[0][0] == first
    assert result["diagnostics"]["initial_nominal_joint_search_score"] == pytest.approx(expected)
    replay = evaluate_schedule(params, policies(result))
    assert result["projections"]["projected_cost"] == pytest.approx(replay.tariff_cost)
    if cap == 2:
        assert replay.tariff_cost == 0.0
        assert replay.steps[-1].levels[0] == 0.0


@pytest.mark.parametrize("cap,bound,q", [(1, 2, 0.5), (2, 2, 0.5), (4, 1, 1 / 3)])
@pytest.mark.parametrize("target", [None, dict(timeslot=1, soc_kwh=1.0, mode="at_least")])
def test_modes_targets_premium_bounds_and_qtail_match_independent_oracle(cap, bound, q, target):
    params = request(initial=0.75, prices=(0.4, 1.0, 0.0, 0.0),
        usage=(0.5, 0.5, 0.0, 0.0), pv=(0.25, 0.5, 0.0, 0.0), target=target)
    params.mode_switch_cost, params.throughput_cost_per_kwh, params.action_deadband_kwh = 0.37, 0.13, 0.3
    expected, _ = mixture_oracle(params, cap=cap, bound=bound, demand=0.5, q=q, premium=0.03)
    options = dict(OPTIONS, paired_demand_scenario_spread=0.5, paired_demand_scenario_slots=bound,
        nominal_joint_scenario_slots=cap, pv_tail_low_probability=q)
    result = factory(energy_intervals=24, grid_charge_premium_per_kwh=0.03, **options)(params)
    assert result["diagnostics"]["initial_value_policy_expected_score"] == pytest.approx(expected)
    assert result["diagnostics"]["nominal_joint_scenario_effective_slots"] == min(cap, bound, 4)
    assert result["diagnostics"]["evaluation"]["score"] == pytest.approx(evaluate_schedule(params, policies(result)).score)


def test_current_action_cannot_know_its_joint_outcome():
    params = request(initial=1.0, prices=(1.0, 3.0, 0.0, 0.0),
        usage=(1.0, 0.5, 0.0, 0.0), pv=(0.25, 0.0, 0.0, 0.0))
    causal, _ = mixture_oracle(params)
    forbidden, _ = mixture_oracle(params, clairvoyant_slot=0)
    assert causal[1] > forbidden[1]
    result = factory(energy_intervals=24, **OPTIONS)(params)
    assert result["diagnostics"]["initial_value_policy_expected_score"] == pytest.approx(causal)


@pytest.mark.parametrize("scale", [1.0, 0.0])
def test_scaled_input_dedup_counts_physics_not_successor_levels(scale):
    params = request(usage=(0.0,) * 4, pv=(0.0,) * 4 if scale else (0.5,) * 4)
    options = dict(OPTIONS, pv_scale=scale)
    mixture = factory(energy_intervals=3, **options)(params)
    old = factory(energy_intervals=3, **dict(options, include_nominal_joint_scenario=False))(params)
    assert mixture["diagnostics"]["search_transitions"] == old["diagnostics"]["search_transitions"]
    assert mixture["entities"] == old["entities"]
    # Full battery can yield identical levels despite distinct observed PV/imports.
    params = request(initial=3.0, usage=(0.0,) * 4, pv=(0.5, 0.0, 0.0, 0.0))
    result = factory(energy_intervals=3, **OPTIONS)(params)
    old = factory(energy_intervals=3, **dict(OPTIONS, include_nominal_joint_scenario=False))(params)
    assert result["diagnostics"]["search_transitions"] - old["diagnostics"]["search_transitions"] == 15


def test_means_horizon_clamp_input_isolation_and_hybrid_forwarding():
    params = request(initial=0.75, usage=(0.5,) * 4, pv=(0.25,) * 4)
    snapshot = params.model_dump()
    options = dict(OPTIONS, paired_demand_scenario_slots=12, nominal_joint_scenario_slots=6)
    result = factory(energy_intervals=24, **options)(params)
    hybrid = hybrid_factory(energy_intervals=24, **options)(params)
    assert params.model_dump() == snapshot
    assert result["entities"] == hybrid["entities"]
    diag = result["diagnostics"]
    assert diag["nominal_joint_scenario_effective_slots"] == 4
    for factors in (diag["nominal_joint_scenario_pv_factors"], diag["nominal_joint_scenario_demand_factors"]):
        assert sum(w * f for w, f in zip(diag["nominal_joint_scenario_weights"], factors)) == pytest.approx(1.0)
    shifted = params.model_copy(update={"plan_start": params.plan_start.replace(year=2027)})
    assert policies(factory(energy_intervals=24, **options)(shifted)) == policies(result)
    assert "nominal" in diag["scenario_assumption"]


def test_two_scaled_input_pairs_accumulate_nominal_and_optimistic_weights():
    # One factor rounds to 1 while its opposite remains distinct. Zero demand
    # makes nominal/high input pairs identical, combining weights .5 and .25.
    params = request(initial=0.75, usage=(0.0,) * 4, pv=(0.5, 0.0, 0.0, 0.0))
    options = dict(OPTIONS, pv_scenario_spread=1e-16, paired_demand_scenario_spread=1e-16)
    result = factory(energy_intervals=3, **options)(params)
    old = factory(energy_intervals=3, **dict(options, include_nominal_joint_scenario=False))(params)
    assert result["diagnostics"]["search_transitions"] == old["diagnostics"]["search_transitions"]
    assert result["entities"] == old["entities"]


def test_efficiency_rate_reserve_and_premium_use_branch_physics():
    params = request(initial=0.125, prices=(0.4, 1.0, 0.0, 0.0),
        usage=(0.5, 0.25, 0.0, 0.0), pv=(0.25, 0.5, 0.0, 0.0))
    battery = params.battery_entities[0]
    battery.minimum_kwh = 0.25
    battery.charge_efficiency = battery.discharge_efficiency = 0.5
    battery.charge_curve_kwh = battery.discharge_curve_kwh = [0.25]
    expected, _ = mixture_oracle(params, demand=0.5, premium=0.03)
    result = factory(energy_intervals=48, grid_charge_premium_per_kwh=0.03,
        **dict(OPTIONS, paired_demand_scenario_spread=0.5))(params)
    assert result["diagnostics"]["initial_nominal_joint_search_score"] == pytest.approx(expected)
    assert result["diagnostics"]["evaluation"]["score"] == pytest.approx(evaluate_schedule(params, policies(result)).score)


def test_fleet_passive_and_accepted_repair_disclose_omission():
    for params, planner in [(request(batteries=2), factory),
                            (request(pv=(0.0, 0.0, 10.0, 0.0)), hybrid_factory)]:
        result = planner(**OPTIONS)(params)
        old = planner(**dict(OPTIONS, include_nominal_joint_scenario=False))(params)
        assert result["entities"] == old["entities"]
        diag = result["diagnostics"].get("hybrid_gate", result["diagnostics"])
        assert diag["nominal_joint_scenario_evaluated"] is False
        assert diag["nominal_joint_scenario_applied"] is False
    params = request(initial=2.1, prices=(1.2, 0.3, 1.2, 0.3),
        usage=(0.0, 0.0, 1.1, 1.1), pv=(0.0, 0.2, 0.0, 0.2),
        target=dict(timeslot=3, soc_kwh=1.1, tolerance_kwh=0.02, mode="exact"))
    result = factory(energy_intervals=4, nominal_joint_scenario_slots=1,
        **dict(OPTIONS, paired_demand_scenario_slots=4))(params)
    assert result["diagnostics"]["target_repair"]["improved"] is True
    assert result["diagnostics"]["nominal_joint_scenario_evaluated"] is True
    assert result["diagnostics"]["nominal_joint_scenario_applied"] is False
