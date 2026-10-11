"""Causal exhaustive oracle for the mean-preserving PV-only tail distribution."""

from functools import lru_cache

import pytest

from custom_components.wattplan.optimizer.physics import EPSILON, transition
from custom_components.wattplan.optimizer.planners.evaluation import evaluate_schedule, target_miss
from custom_components.wattplan.optimizer.planners.hybrid_policy import factory as hybrid_factory
from custom_components.wattplan.optimizer.planners.recovery import reserve_shortfall_cost
from custom_components.wattplan.optimizer.planners.value_policy import factory
from tests.optimizer.test_dynamic_programming_planner import policies, request


OPTIONS = dict(pv_scenario_spread=1.0, paired_demand_scenario_spread=0.5,
               paired_demand_scenario_slots=1, pv_tail_low_probability=1 / 3)


def tail_oracle(params, *, bound=1, q=1 / 3, spread=1.0, premium=0.0, clairvoyant_slot=None):
    """Enumerate actions before outcomes, retaining observed physical histories."""
    battery = params.battery_entities[0]
    actions = ("self_consume", "preserve", "grid_charge") if battery.can_charge_from & 1 else ("self_consume", "preserve")

    @lru_cache(None)
    def solve(slot, level, previous):
        if slot == len(params.usage_kwh):
            return (0.0, 0.0), None
        outcomes = (((0.5, 1 - spread, 1.5), (0.5, 1 + spread, 0.5)) if slot < bound
                    else ((q, 1 - spread, 1.0), (1 - q, 1 + q * spread / (1 - q), 1.0)))
        action_scores = []
        for action in actions:
            scores = []
            for weight, pv_factor, demand_factor in outcomes:
                step = transition([battery], [level], [action],
                    usage=params.usage_kwh[slot] * demand_factor, pv=params.solar_input_kwh[slot] * pv_factor)
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
                scores.append((weight, (miss + future[0], cost + future[1])))
            action_scores.append(scores)
        if slot == clairvoyant_slot:
            return tuple(sum(outcomes[b][0] * min(rows[b][1] for rows in action_scores)[k]
                             for b in (0, 1)) for k in (0, 1)), None
        expected = [tuple(sum(w * s[k] for w, s in rows) for k in (0, 1)) for rows in action_scores]
        selected = min(range(len(actions)), key=lambda i: expected[i])
        return expected[selected], actions[selected]

    return solve(0, battery.initial_kwh, 0)


@pytest.mark.parametrize("planner", [factory, hybrid_factory])
@pytest.mark.parametrize("q", [True, False, 0.0, -0.1, 0.5001, float("nan"), float("inf"), None, "1/3"])
def test_probability_validation(planner, q):
    with pytest.raises(ValueError, match="pv_tail_low_probability"):
        planner(**dict(OPTIONS, pv_tail_low_probability=q))


@pytest.mark.parametrize("change", [dict(pv_scenario_spread=0.0),
    dict(paired_demand_scenario_spread=0.0), dict(paired_demand_scenario_slots=None),
    dict(first_action_rollout=True)])
@pytest.mark.parametrize("planner", [factory, hybrid_factory])
def test_explicit_configuration_dependencies(planner, change):
    with pytest.raises(ValueError):
        planner(**dict(OPTIONS, **change))


@pytest.mark.parametrize("planner", [factory, hybrid_factory])
@pytest.mark.parametrize("options", [{}, dict(OPTIONS, first_action_rollout=True),
    dict(OPTIONS, include_initial_knot=True, include_charge_knots=True)])
def test_half_probability_preserves_full_default_result(planner, options, monkeypatch):
    monkeypatch.setattr("custom_components.wattplan.optimizer.planners.shared.perf_counter", lambda: 0.0)
    monkeypatch.setattr("custom_components.wattplan.optimizer.planners.reporting.perf_counter", lambda: 0.0)
    options = {k: v for k, v in options.items() if k != "pv_tail_low_probability"}
    params = request(initial=1.123, pv=(0.4, 0.0, 0.7, 0.0))
    assert planner(**options)(params) == planner(**options, pv_tail_low_probability=0.5)(params)


def test_scalar_deficit_kink_changes_storage_choice_preserving_mean():
    params = request(initial=1.0, ingress=2, prices=(0.0, 1.7, 5.0, 0.0),
        usage=(0.0, 1.0, 1.0, 0.0), pv=(0.0, 0.0, 1.0, 0.0))
    result = factory(energy_intervals=6, **OPTIONS)(params)
    symmetric = factory(energy_intervals=6, **dict(OPTIONS, pv_tail_low_probability=0.5))(params)
    expected, _ = tail_oracle(params)
    assert expected == pytest.approx((0.0, 5 / 3))
    assert result["diagnostics"]["initial_value_policy_expected_score"] == pytest.approx(expected)
    assert policies(result)[0][1] == "self_consume"
    assert policies(symmetric)[0][1] == "preserve"
    assert sum(w * f for w, f in zip((1 / 3, 2 / 3), (0.0, 1.5))) == pytest.approx(1.0)


@pytest.mark.parametrize("target", [None, dict(timeslot=1, soc_kwh=1.0, mode="at_least")])
def test_weighted_modes_preferences_premium_and_target_first_match_oracle(target):
    params = request(initial=0.75, prices=(0.4, 1.0, 0.0, 0.0),
        usage=(0.5, 0.5, 0.0, 0.0), pv=(0.25, 0.5, 0.0, 0.0), target=target)
    params.mode_switch_cost, params.throughput_cost_per_kwh, params.action_deadband_kwh = 0.37, 0.13, 0.3
    expected, _ = tail_oracle(params, premium=0.03)
    result = factory(energy_intervals=24, grid_charge_premium_per_kwh=0.03, **OPTIONS)(params)
    assert result["diagnostics"]["initial_value_policy_expected_score"] == pytest.approx(expected)
    replay = evaluate_schedule(params, policies(result))
    assert result["diagnostics"]["evaluation"]["score"] == pytest.approx(replay.score)
    assert result["projections"]["projected_cost"] == pytest.approx(replay.tariff_cost)


def test_tail_action_precedes_own_pv_outcome():
    params = request(initial=1.0, ingress=2, prices=(0.0, 1.0, 3.0, 0.0),
        usage=(0.0, 1.0, 0.5, 0.0), pv=(0.0, 0.25, 0.0, 0.0))
    causal, _ = tail_oracle(params)
    forbidden, _ = tail_oracle(params, clairvoyant_slot=1)
    assert causal[1] > forbidden[1]
    result = factory(energy_intervals=24, **OPTIONS)(params)
    assert result["diagnostics"]["initial_value_policy_expected_score"] == pytest.approx(causal)


def test_nonunit_spread_and_quarter_probability_match_independent_oracle():
    params = request(initial=0.75, prices=(0.4, 1.0, 0.0, 0.0),
        usage=(0.5, 0.5, 0.0, 0.0), pv=(0.75, 0.75, 0.0, 0.0))
    params.mode_switch_cost, params.throughput_cost_per_kwh = 0.37, 0.13
    expected, _ = tail_oracle(params, q=0.25, spread=0.5, premium=0.03)
    options = dict(OPTIONS, pv_scenario_spread=0.5, pv_tail_low_probability=0.25)
    result = factory(energy_intervals=24, grid_charge_premium_per_kwh=0.03, **options)(params)
    assert result["diagnostics"]["initial_value_policy_expected_score"] == pytest.approx(expected)


def test_zero_pv_scaled_dedup_and_bound_beyond_horizon():
    params = request(initial=0.75, pv=(0.0,) * 4)
    for scale, pv in ((1.0, (0.0,) * 4), (0.0, (0.5,) * 4)):
        params.solar_input_kwh = list(pv)
        options = dict(OPTIONS, pv_scale=scale)
        tail = factory(**options)(params)
        normal = factory(**dict(options, pv_tail_low_probability=0.5))(params)
        assert tail["diagnostics"]["search_transitions"] == normal["diagnostics"]["search_transitions"]
        assert tail["entities"] == normal["entities"]
    params.solar_input_kwh = [0.25] * 4
    options = dict(OPTIONS, paired_demand_scenario_slots=12)
    tail = factory(**options)(params)
    normal = factory(**dict(options, pv_tail_low_probability=0.5))(params)
    assert tail["entities"] == normal["entities"]
    assert tail["diagnostics"]["pv_tail_effective_slots"] == 0
    assert tail["diagnostics"]["pv_tail_applied"] is False


def test_relative_bound_input_isolation_and_hybrid_forwarding():
    params = request(initial=0.75, usage=(0.5,) * 4, pv=(0.25,) * 4)
    snapshot = params.model_dump()
    value = factory(energy_intervals=24, **OPTIONS)(params)
    hybrid = hybrid_factory(energy_intervals=24, **OPTIONS)(params)
    assert params.model_dump() == snapshot
    assert value["entities"] == hybrid["entities"]
    shifted = params.model_copy(update={"plan_start": params.plan_start.replace(year=2027)})
    assert policies(factory(energy_intervals=24, **OPTIONS)(shifted)) == policies(value)
    assert value["diagnostics"]["pv_tail_start_slot"] == 1
    assert value["diagnostics"]["pv_tail_effective_slots"] == 3
    assert hybrid["diagnostics"]["hybrid_gate"]["pv_tail_applied"] is True


def test_fleet_passive_and_accepted_repair_disclose_omission():
    for params, planner in [(request(batteries=2), factory),
                            (request(pv=(0.0, 0.0, 10.0, 0.0)), hybrid_factory)]:
        tail = planner(**OPTIONS)(params)
        ordinary = planner(**dict(OPTIONS, pv_tail_low_probability=0.5))(params)
        assert tail["entities"] == ordinary["entities"]
        diag = tail["diagnostics"].get("hybrid_gate", tail["diagnostics"])
        assert diag["pv_tail_applied"] is False
    params = request(initial=2.1, prices=(1.2, 0.3, 1.2, 0.3),
        usage=(0.0, 0.0, 1.1, 1.1), pv=(0.0, 0.2, 0.0, 0.2),
        target=dict(timeslot=3, soc_kwh=1.1, tolerance_kwh=0.02, mode="exact"))
    result = factory(energy_intervals=4, **dict(OPTIONS, paired_demand_scenario_slots=3))(params)
    assert result["diagnostics"]["target_repair"]["improved"] is True
    assert result["diagnostics"]["pv_tail_evaluated"] is True
    assert result["diagnostics"]["pv_tail_applied"] is False
