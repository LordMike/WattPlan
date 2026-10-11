"""Independent tiny stochastic Bellman oracle for PV scenario dispatch."""

from functools import lru_cache

import pytest

from custom_components.wattplan.optimizer.physics import EPSILON, transition
from custom_components.wattplan.optimizer.planners.evaluation import evaluate_schedule, target_miss
from custom_components.wattplan.optimizer.planners.recovery import reserve_shortfall_cost
from custom_components.wattplan.optimizer.planners.value_policy import factory
from tests.optimizer.test_dynamic_programming_planner import request, policies


def stochastic_oracle(params, spread):
    """Choose action before PV branch; allow next action to observe resulting state."""
    battery = params.battery_entities[0]
    choices = ("self_consume", "preserve", "grid_charge")

    @lru_cache(None)
    def recurse(slot, level, previous):
        if slot == len(params.usage_kwh):
            return (0.0, 0.0)
        scores = []
        factors = (1 - spread, 1 + spread)
        for action in choices:
            branches = []
            for factor in factors:
                step = transition([battery], [level], [action], usage=params.usage_kwh[slot],
                                  pv=params.solar_input_kwh[slot] * factor)
                charge, discharge = step.charge, sum(step.discharge)
                mode = (1 if charge > EPSILON and charge >= params.action_deadband_kwh
                        else -1 if discharge > EPSILON and discharge >= params.action_deadband_kwh else 0)
                miss = target_miss(battery.target, step.levels[0]) if battery.target and battery.target.timeslot == slot else 0.0
                future_miss, future_cost = recurse(slot + 1, step.levels[0], mode)
                cost = (step.grid_import * params.grid_import_price_per_kwh[slot]
                        - step.grid_export * params.grid_export_price_per_kwh[slot]
                        + (charge + discharge) * params.throughput_cost_per_kwh
                        + (mode != previous) * params.mode_switch_cost
                        + reserve_shortfall_cost([battery], step.levels))
                branches.append((miss + future_miss, cost + future_cost))
            scores.append(tuple(sum(branch[k] for branch in branches) / 2 for k in (0, 1)))
        return min(scores)

    return recurse(0, battery.initial_kwh, 0)


@pytest.mark.parametrize("spread", [0.5, 1.0])
@pytest.mark.parametrize("switch_cost", [0.0, 0.37])
@pytest.mark.parametrize("target", [None, dict(timeslot=1, soc_kwh=1.0, mode="at_least")])
def test_scenario_expected_score_matches_independent_aligned_state_oracle(spread, switch_cost, target):
    params = request(prices=(0.4, 1.0, 0.0, 0.0), usage=(0.0, 1.0, 0.0, 0.0), pv=(0.5, 0.0, 0.0, 0.0), target=target)
    params.mode_switch_cost = switch_cost
    params.throughput_cost_per_kwh = 0.13
    # Branch states remain exact multiples of 0.25, so interpolation is exact.
    result = factory(energy_intervals=12, pv_scenario_spread=spread)(params)
    assert result["diagnostics"]["initial_value_policy_expected_score"] == pytest.approx(stochastic_oracle(params, spread))
    assert result["diagnostics"]["evaluation"]["score"] == pytest.approx(evaluate_schedule(params, policies(result)).score)


def test_zero_scenario_keeps_deterministic_path_and_diagnostics():
    params = request(initial=1.25, pv=(0.25, 0.5, 0.0, 0.25))
    default = factory(energy_intervals=4)(params)
    explicit = factory(energy_intervals=4, pv_scenario_spread=0.0)(params)
    assert policies(default) == policies(explicit)
    assert default["diagnostics"]["evaluation"] == explicit["diagnostics"]["evaluation"]
    assert default["diagnostics"]["search_transitions"] == explicit["diagnostics"]["search_transitions"]
    assert explicit["diagnostics"]["algorithm"] == "interpolated_backward_value_policy"
    assert "pv_scenario_spread" not in explicit["diagnostics"]


def test_zero_pv_branches_do_not_duplicate_physical_work():
    params = request()
    deterministic = factory(energy_intervals=4)(params)
    scenarios = factory(energy_intervals=4, pv_scenario_spread=1.0)(params)
    assert scenarios["diagnostics"]["search_transitions"] == deterministic["diagnostics"]["search_transitions"]
    assert policies(scenarios) == policies(deterministic)


@pytest.mark.parametrize("spread", [-0.1, 1.1, True, float("nan"), float("inf")])
def test_invalid_scenario_spread_is_rejected(spread):
    with pytest.raises(ValueError, match="pv_scenario_spread"):
        factory(pv_scenario_spread=spread)



def test_action_is_chosen_before_pv_realization_not_separately_per_branch():
    params = request(initial=1.0, prices=(1.0, 3.0, 0.0, 0.0),
                     usage=(1.0, 0.5, 0.0, 0.0), pv=(0.25, 0.0, 0.0, 0.0))
    result = factory(energy_intervals=6, pv_scenario_spread=1.0)(params)
    oracle = stochastic_oracle(params, 1.0)
    assert oracle == pytest.approx((0.0, 0.75))
    # A controller that sees current PV before acting pays only 0.5 on average:
    # preserve at zero PV, self-consume at 0.5 PV. Such foresight is forbidden.
    assert oracle[1] > 0.5
    assert result["diagnostics"]["initial_value_policy_expected_score"] == pytest.approx(oracle)


def test_opposing_physical_modes_are_not_averaged_to_idle():
    params = request(initial=1.0, prices=(0.0, 0.0, 0.0, 0.0),
                     usage=(0.5, 0.0, 0.0, 0.0), pv=(0.5, 0.0, 0.0, 0.0))
    params.mode_switch_cost = 0.37
    result = factory(energy_intervals=6, pv_scenario_spread=1.0)(params)
    assert result["diagnostics"]["initial_value_policy_expected_score"] == pytest.approx(stochastic_oracle(params, 1.0))
    # Self-consumption discharges in the low branch and charges in the high
    # branch; those two nonzero modes both incur switching costs.
    assert policies(result)[0][0] == "preserve"


def test_oracle_includes_preservation_as_a_competing_action():
    params = request(initial=1.0, prices=(0.4, 2.0, 0.0, 0.0),
                     usage=(1.0, 1.0, 0.0, 0.0), pv=(0.75, 0.0, 0.0, 0.0))
    result = factory(energy_intervals=12, pv_scenario_spread=1.0)(params)
    assert stochastic_oracle(params, 1.0) == pytest.approx((0.0, 0.2))
    # Preservation pays 0.4 only in the low branch and retains the initial
    # energy for the next demand, beating expected grid-charge cost 0.5.
    assert result["diagnostics"]["initial_value_policy_expected_score"] == pytest.approx((0.0, 0.2))
    assert policies(result)[0][0] == "preserve"



def test_expected_target_risk_does_not_trigger_nominal_target_repair():
    params = request(pv=(0.5, 0.0, 0.0, 0.0), usage=(0.0, 0.0, 0.0, 0.0), ingress=2,
                     target=dict(timeslot=0, soc_kwh=0.5, tolerance_kwh=0.02, mode="exact"))
    result = factory(energy_intervals=6, pv_scenario_spread=1.0)(params)
    assert result["diagnostics"]["initial_value_policy_expected_score"][0] == pytest.approx(0.48)
    assert result["diagnostics"]["evaluation"]["score"][0] == 0
    assert result["diagnostics"]["target_repair"] is None
