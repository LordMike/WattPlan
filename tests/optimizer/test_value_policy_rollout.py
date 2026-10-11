"""Exact first-action rollout: independent causal oracle and physical replay."""

from functools import lru_cache
from statistics import median

import pytest

from custom_components.wattplan.optimizer.physics import EPSILON, transition
from custom_components.wattplan.optimizer.planners.evaluation import evaluate_schedule, target_miss
from custom_components.wattplan.optimizer.planners.hybrid_policy import factory as hybrid_factory
from custom_components.wattplan.optimizer.planners.recovery import reserve_shortfall_cost
from custom_components.wattplan.optimizer.planners.value_policy import factory
from tests.optimizer.test_dynamic_programming_planner import policies, request


def kernel_request(*, prices, usage, pv=None, **options):
    """Shorten a validated request to isolate the two-slot search kernel.

    Public requests have at least four slots. Short internal horizons directly
    exercise the rollout boundary and terminal value without an approximate tail.
    """
    params = request(**options)
    return params.model_copy(update={
        "grid_import_price_per_kwh": list(prices),
        "grid_export_price_per_kwh": [0.0] * len(prices),
        "usage_kwh": list(usage), "solar_input_kwh": list(pv or [0.0] * len(prices)),
    })


def exhaustive_scenario_oracle(params, *, spread=0.0, premium=0.0, pv_scale=1.0,
                               demand_scale=1.0, terminal_fraction=0.0, clairvoyant_slot=None):
    """Enumerate actions before each PV draw, observing only prior physical states.

    The deliberately invalid clairvoyant alternative demonstrates the difference
    between averaging action scores and selecting separately after the PV draw.
    """
    battery = params.battery_entities[0]
    actions = ("self_consume", "preserve", "grid_charge") if battery.can_charge_from & 1 else ("self_consume", "preserve")
    factors = (1 - spread, 1 + spread) if spread else (1.0,)
    credit = max(median(params.grid_import_price_per_kwh[-12:]), 0.0) * battery.discharge_efficiency * terminal_fraction

    @lru_cache(None)
    def solve(slot, level, previous):
        if slot == len(params.usage_kwh):
            return (0.0, -max(level - battery.minimum_kwh, 0.0) * credit), None
        scores = []
        for action in actions:
            branches = []
            for factor in factors:
                step = transition([battery], [level], [action],
                    usage=params.usage_kwh[slot] * demand_scale,
                    pv=params.solar_input_kwh[slot] * pv_scale * factor)
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
            scores.append(branches)
        if slot == clairvoyant_slot:
            selected = [min(rows[branch] for rows in scores) for branch in range(len(factors))]
            return tuple(sum(row[k] for row in selected) / len(factors) for k in (0, 1)), None
        expected = [tuple(sum(row[k] for row in branches) / len(factors) for k in (0, 1)) for branches in scores]
        best = min(range(len(actions)), key=lambda index: expected[index])
        return expected[best], actions[best]

    return solve(0, battery.initial_kwh, 0)


@pytest.mark.parametrize("planner", [factory, hybrid_factory])
@pytest.mark.parametrize("setting", [0, 1, None, "true", [], 0.0])
def test_rollout_requires_boolean(planner, setting):
    with pytest.raises(ValueError, match="first_action_rollout"):
        planner(first_action_rollout=setting)


@pytest.mark.parametrize("planner", [factory, hybrid_factory])
@pytest.mark.parametrize("spread", [0.0, 1.0])
def test_explicit_false_preserves_complete_default_result(planner, spread, monkeypatch):
    monkeypatch.setattr("custom_components.wattplan.optimizer.planners.shared.perf_counter", lambda: 0.0)
    monkeypatch.setattr("custom_components.wattplan.optimizer.planners.reporting.perf_counter", lambda: 0.0)
    params = request(initial=1.123, pv=(0.4, 0.0, 0.7, 0.0))
    options = dict(energy_intervals=6, pv_scenario_spread=spread,
                   grid_charge_premium_per_kwh=0.03, include_charge_knots=True)
    assert planner(**options)(params) == planner(**options, first_action_rollout=False)(params)


@pytest.mark.parametrize("spread,switch_cost,target", [
    (0.0, 0.0, None), (0.5, 0.37, None), (1.0, 0.0, None),
    (1.0, 0.37, dict(timeslot=0, soc_kwh=1.0, mode="at_least")),
    (1.0, 0.37, dict(timeslot=1, soc_kwh=1.0, mode="exact", tolerance_kwh=0.1)),
])
def test_two_slot_rollout_matches_independent_oracle_at_off_knot_states(spread, switch_cost, target):
    params = kernel_request(initial=0.713, prices=(0.4, 1.0), usage=(0.43, 0.81),
                     pv=(0.37, 0.29), target=target)
    params.mode_switch_cost = switch_cost
    params.throughput_cost_per_kwh = 0.13
    params.action_deadband_kwh = 0.3
    options = dict(pv_scale=0.8, demand_scale=1.1, terminal_value_fraction=0.4,
                   pv_scenario_spread=spread, grid_charge_premium_per_kwh=0.03)
    expected, _ = exhaustive_scenario_oracle(params, spread=spread, premium=0.03,
        pv_scale=0.8, demand_scale=1.1, terminal_fraction=0.4)
    result = factory(energy_intervals=2, first_action_rollout=True, **options)(params)
    assert result["diagnostics"]["initial_rollout_search_score"] == pytest.approx(expected)
    if spread:
        assert result["diagnostics"]["initial_value_policy_expected_score"] == pytest.approx(expected)
    replay = evaluate_schedule(params, policies(result))
    assert result["diagnostics"]["evaluation"]["score"] == pytest.approx(replay.score)
    assert result["projections"]["projected_cost"] == pytest.approx(replay.tariff_cost)


def test_first_action_cannot_observe_current_pv():
    params = kernel_request(initial=1.0, prices=(1.0, 3.0), usage=(1.0, 0.5), pv=(0.25, 0.0))
    causal, _ = exhaustive_scenario_oracle(params, spread=1.0)
    clairvoyant, _ = exhaustive_scenario_oracle(params, spread=1.0, clairvoyant_slot=0)
    assert causal == pytest.approx((0.0, 0.75))
    assert clairvoyant == pytest.approx((0.0, 0.5))
    result = factory(energy_intervals=2, pv_scenario_spread=1.0, first_action_rollout=True)(params)
    assert result["diagnostics"]["initial_rollout_search_score"] == pytest.approx(causal)


def test_second_action_cannot_observe_its_own_pv_branch():
    params = request(initial=1.0, ingress=2, prices=(0.0, 1.0, 3.0, 0.0),
                     usage=(0.0, 1.0, 0.5, 0.0), pv=(0.0, 0.25, 0.0, 0.0))
    causal, _ = exhaustive_scenario_oracle(params, spread=1.0)
    clairvoyant, _ = exhaustive_scenario_oracle(params, spread=1.0, clairvoyant_slot=1)
    assert causal == pytest.approx((0.0, 0.75))
    assert clairvoyant == pytest.approx((0.0, 0.5))
    # Slot two's states align with .25 knots; only the first two slots roll out.
    result = factory(energy_intervals=12, pv_scenario_spread=1.0, first_action_rollout=True)(params)
    assert result["diagnostics"]["initial_rollout_search_score"] == pytest.approx(causal)


def test_second_action_can_adapt_to_prior_branch_mode_and_state():
    params = kernel_request(initial=1.0, prices=(10.0, 0.2), usage=(0.5, 0.5), pv=(0.5, 0.0))
    params.mode_switch_cost = 0.4
    causal, action = exhaustive_scenario_oracle(params, spread=1.0)
    # First self-consumption discharges at low PV and charges at high PV.
    # Next self-consumption wins in the former branch, grid charging in the
    # latter avoids the .4 switching cost for a .3 tariff cost.
    assert action == "self_consume"
    assert causal == pytest.approx((0.0, 0.55))
    result = factory(energy_intervals=2, pv_scenario_spread=1.0, first_action_rollout=True)(params)
    assert policies(result)[0][0] == action
    assert result["diagnostics"]["initial_rollout_search_score"] == pytest.approx(causal)


@pytest.mark.parametrize("pv,extra", [((0.0, 0.0, 0.0, 0.0), 9),
    ((0.5, 0.5, 0.5, 0.5), 36), ((0.0, 0.5, 0.5, 0.5), 18)])
def test_only_first_action_rolls_out_and_zero_pv_deduplicates_work(pv, extra):
    params = request(pv=pv)
    ordinary = factory(energy_intervals=4, pv_scenario_spread=1.0)(params)
    rollout = factory(energy_intervals=4, pv_scenario_spread=1.0, first_action_rollout=True)(params)
    assert rollout["diagnostics"]["rollout_search_transitions"] == extra
    assert rollout["diagnostics"]["search_transitions"] - ordinary["diagnostics"]["search_transitions"] == extra


def test_one_slot_request_discloses_rollout_not_applied():
    params = kernel_request(prices=(0.1,), usage=(1.0,))
    ordinary = factory()(params)
    rollout = factory(first_action_rollout=True)(params)
    assert rollout["entities"] == ordinary["entities"]
    assert rollout["diagnostics"]["first_action_rollout_applied"] is False
    assert rollout["diagnostics"]["rollout_search_transitions"] == 0
    assert rollout["diagnostics"]["initial_rollout_search_score"] is None


def test_all_scores_tie_keeps_original_action_priority():
    params = request(prices=(0.0,) * 4, usage=(0.0,) * 4)
    result = factory(first_action_rollout=True)(params)
    assert policies(result)[0] == ["self_consume"] * 4


def test_large_premium_never_outweighs_target_priority():
    params = request(prices=(0.1, 1.0, 0.0, 0.0), usage=(0.0,) * 4,
                     target=dict(timeslot=0, soc_kwh=1.0, mode="at_least"))
    result = factory(first_action_rollout=True, grid_charge_premium_per_kwh=100.0)(params)
    assert policies(result)[0][0] == "grid_charge"
    assert result["diagnostics"]["evaluation"]["score"][0] == 0.0
    assert result["projections"]["projected_cost"] == pytest.approx(0.1)


@pytest.mark.parametrize("initial", [2.54, 2.56])
def test_rollout_uses_exact_half_slot_grid_headroom_and_signed_tariffs(initial):
    params = kernel_request(initial=initial, prices=(-0.1, 1.0), usage=(0.0, 1.0))
    params.battery_entities[0].charge_efficiency = 0.9
    expected, action = exhaustive_scenario_oracle(params, premium=0.03)
    result = factory(energy_intervals=2, grid_charge_premium_per_kwh=0.03,
                     first_action_rollout=True)(params)
    assert result["diagnostics"]["initial_rollout_search_score"] == pytest.approx(expected)
    assert policies(result)[0][0] == action
    assert (action == "grid_charge") == (initial < 2.55)


def test_accepted_target_repair_discloses_rollout_replaced():
    params = request(initial=2.1, prices=(1.2, 0.3, 1.2, 0.3),
                     usage=(0.0, 0.0, 1.1, 1.1), pv=(0.0, 0.2, 0.0, 0.2),
                     target=dict(timeslot=3, soc_kwh=1.1, tolerance_kwh=0.02, mode="exact"))
    result = factory(energy_intervals=4, first_action_rollout=True)(params)
    diagnostics = result["diagnostics"]
    assert diagnostics["target_repair"]["improved"] is True
    assert diagnostics["first_action_rollout_evaluated"] is True
    assert diagnostics["first_action_rollout_applied"] is False
    assert diagnostics["target_repair"]["first_action_rollout_applied"] is False
    assert diagnostics["initial_rollout_search_score"] is not None
    assert diagnostics["evaluation"]["score"] == pytest.approx(evaluate_schedule(params, policies(result)).score)


def test_fleet_fallback_unchanged_and_discloses_rollout_not_applied():
    params = request(batteries=2)
    ordinary = factory()(params)
    rollout = factory(first_action_rollout=True)(params)
    assert rollout["entities"] == ordinary["entities"]
    assert rollout["projections"] == ordinary["projections"]
    assert rollout["diagnostics"]["value_policy_fallback"] == "bounded_joint_state_dp"
    assert rollout["diagnostics"]["first_action_rollout_applied"] is False


def test_hybrid_forwards_rollout_and_discloses_passive_branch():
    params = request(pv=(0.0, 0.4, 0.0, 0.0))
    before = params.model_dump()
    options = dict(energy_intervals=6, pv_scenario_spread=1.0,
                   grid_charge_premium_per_kwh=0.03, first_action_rollout=True)
    direct = factory(**options)(params)
    hybrid = hybrid_factory(**options)(params)
    assert hybrid["entities"] == direct["entities"]
    assert hybrid["projections"] == direct["projections"]
    assert hybrid["diagnostics"]["hybrid_gate"]["first_action_rollout_applied"] is True
    assert params.model_dump() == before
    passive_params = request(pv=(0.0, 0.0, 10.0, 0.0))
    ordinary = hybrid_factory()(passive_params)
    passive = hybrid_factory(first_action_rollout=True)(passive_params)
    assert passive["entities"] == ordinary["entities"]
    assert passive["projections"] == ordinary["projections"]
    assert passive["diagnostics"]["hybrid_gate"]["first_action_rollout_applied"] is False
