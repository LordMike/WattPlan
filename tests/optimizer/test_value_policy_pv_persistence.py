"""Two-slot latent PV model: observations reveal factors, hidden labels do not."""

from functools import lru_cache

import pytest

from custom_components.wattplan.optimizer.physics import EPSILON, transition
from custom_components.wattplan.optimizer.planners.evaluation import evaluate_schedule, target_miss
from custom_components.wattplan.optimizer.planners.hybrid_policy import factory as hybrid_factory
from custom_components.wattplan.optimizer.planners.recovery import reserve_shortfall_cost
from custom_components.wattplan.optimizer.planners.value_policy import factory
from tests.optimizer.test_dynamic_programming_planner import policies, request
from tests.optimizer.test_value_policy_rollout import kernel_request


def persistence_oracle(params, *, spread=1.0, premium=0.0, pv_scale=1.0,
                       reveal_hidden=False, clairvoyant_first=False, persist_tail=False):
    """Independent belief-aware exhaustive oracle; optional invalid foresight traps."""
    battery = params.battery_entities[0]
    choices = ("self_consume", "preserve", "grid_charge") if battery.can_charge_from & 1 else ("self_consume", "preserve")
    prior = (1 - spread, 1 + spread)
    observed = params.solar_input_kwh[0] * pv_scale * prior[0] != params.solar_input_kwh[0] * pv_scale * prior[1]

    @lru_cache(None)
    def solve(slot, level, previous, known):
        if slot == len(params.usage_kwh):
            return (0.0, 0.0), None
        factors = (known,) if known is not None and (slot == 1 or persist_tail) else prior
        scores = []
        for action in choices:
            branches = []
            for factor in factors:
                step = transition([battery], [level], [action], usage=params.usage_kwh[slot],
                    pv=params.solar_input_kwh[slot] * pv_scale * factor)
                mode = (1 if step.charge > EPSILON and step.charge >= params.action_deadband_kwh
                        else -1 if sum(step.discharge) > EPSILON and sum(step.discharge) >= params.action_deadband_kwh else 0)
                miss = target_miss(battery.target, step.levels[0]) if battery.target and battery.target.timeslot == slot else 0.0
                next_known = factor if slot == 0 and (observed or reveal_hidden) else known if persist_tail else None
                future, _ = solve(slot + 1, step.levels[0], mode, next_known)
                cost = (step.grid_import * params.grid_import_price_per_kwh[slot]
                        - step.grid_export * params.grid_export_price_per_kwh[slot]
                        + (step.charge + sum(step.discharge)) * params.throughput_cost_per_kwh
                        + (mode != previous) * params.mode_switch_cost
                        + reserve_shortfall_cost([battery], step.levels)
                        + sum(step.grid_charge) * premium)
                branches.append((miss + future[0], cost + future[1]))
            scores.append(branches)
        if slot == 0 and clairvoyant_first:
            selected = [min(rows[b] for rows in scores) for b in range(len(factors))]
            return tuple(sum(row[k] for row in selected) / len(factors) for k in (0, 1)), None
        expected = [tuple(sum(row[k] for row in rows) / len(factors) for k in (0, 1)) for rows in scores]
        best = min(range(len(choices)), key=lambda index: expected[index])
        return expected[best], choices[best]

    return solve(0, battery.initial_kwh, 0, None)


@pytest.mark.parametrize("planner", [factory, hybrid_factory])
@pytest.mark.parametrize("setting", [0, 1, None, "true", 0.0])
def test_persistence_requires_boolean(planner, setting):
    with pytest.raises(ValueError, match="rollout_pv_persistence"):
        planner(first_action_rollout=True, rollout_pv_persistence=setting)


@pytest.mark.parametrize("planner", [factory, hybrid_factory])
def test_dependency_and_unsupported_demand_coupling_rejected(planner):
    with pytest.raises(ValueError, match="requires first_action_rollout"):
        planner(rollout_pv_persistence=True)
    with pytest.raises(ValueError, match="does not support paired_demand_scenario_spread"):
        planner(first_action_rollout=True, rollout_pv_persistence=True, paired_demand_scenario_spread=0.25)


@pytest.mark.parametrize("planner", [factory, hybrid_factory])
@pytest.mark.parametrize("pv_spread,demand_spread,rollout", [(0.0, 0.0, False), (1.0, 0.0, True), (1.0, 0.25, True)])
def test_explicit_false_preserves_entire_result(planner, pv_spread, demand_spread, rollout, monkeypatch):
    monkeypatch.setattr("custom_components.wattplan.optimizer.planners.shared.perf_counter", lambda: 0.0)
    monkeypatch.setattr("custom_components.wattplan.optimizer.planners.reporting.perf_counter", lambda: 0.0)
    params = request(initial=1.123, pv=(0.4, 0.0, 0.7, 0.0))
    options = dict(energy_intervals=6, pv_scenario_spread=pv_spread, paired_demand_scenario_spread=demand_spread,
        first_action_rollout=rollout, grid_charge_premium_per_kwh=0.03)
    assert planner(**options)(params) == planner(**options, rollout_pv_persistence=False)(params)


@pytest.mark.parametrize("first_pv,pv_scale", [(0.0, 1.0), (0.5, 0.0)])
def test_night_or_zero_scale_never_reveals_hidden_branch(first_pv, pv_scale):
    params = request(initial=1.0, ingress=2, prices=(0.0, 1.0, 3.0, 0.0),
        usage=(0.0, 1.0, 0.5, 0.0), pv=(first_pv, 0.25, 0.0, 0.0))
    expected, _ = persistence_oracle(params, pv_scale=pv_scale)
    options = dict(energy_intervals=12, pv_scenario_spread=1.0, pv_scale=pv_scale, first_action_rollout=True)
    ordinary = factory(**options)(params)
    result = factory(**options, rollout_pv_persistence=True)(params)
    assert result["diagnostics"]["initial_pv_persistence_search_score"] == pytest.approx(expected)
    assert result["diagnostics"]["rollout_pv_factor_revealed"] is False
    assert result["entities"] == ordinary["entities"]
    assert result["diagnostics"]["rollout_search_transitions"] == ordinary["diagnostics"]["rollout_search_transitions"]
    if pv_scale:
        hidden, _ = persistence_oracle(params, reveal_hidden=True)
        assert expected == pytest.approx((0.0, 0.75))
        assert hidden == pytest.approx((0.0, 0.5))


def test_daylight_factor_recourse_uses_pv_observation_despite_equal_battery_levels():
    params = request(initial=3.0, prices=(0.0, 1.0, 3.0, 0.0),
        usage=(0.0, 1.0, 2.5, 0.0), pv=(0.5, 0.25, 0.0, 0.0))
    params.battery_entities[0].discharge_curve_kwh = [3.0]
    low = transition(params.battery_entities, [3.0], ["self_consume"], usage=0.0, pv=0.0)
    high = transition(params.battery_entities, [3.0], ["self_consume"], usage=0.0, pv=1.0)
    assert low.levels == high.levels
    assert low.grid_export != high.grid_export
    expected, _ = persistence_oracle(params)
    assert expected == pytest.approx((0.0, 0.5))
    options = dict(energy_intervals=12, pv_scenario_spread=1.0, first_action_rollout=True)
    ordinary = factory(**options)(params)
    result = factory(**options, rollout_pv_persistence=True)(params)
    assert ordinary["diagnostics"]["initial_rollout_search_score"] == pytest.approx((0.0, 0.75))
    assert result["diagnostics"]["initial_pv_persistence_search_score"] == pytest.approx(expected)
    assert result["diagnostics"]["rollout_pv_factor_revealed"] is True
    assert result["diagnostics"]["rollout_search_transitions"] == 18


def test_first_action_precedes_factor_revelation():
    params = kernel_request(initial=1.0, prices=(1.0, 3.0), usage=(1.0, 0.5), pv=(0.25, 0.25))
    expected, _ = persistence_oracle(params)
    clairvoyant, _ = persistence_oracle(params, clairvoyant_first=True)
    assert expected == pytest.approx((0.0, 0.75))
    assert clairvoyant == pytest.approx((0.0, 0.5))
    result = factory(energy_intervals=2, pv_scenario_spread=1.0, first_action_rollout=True,
        rollout_pv_persistence=True)(params)
    assert result["diagnostics"]["initial_pv_persistence_search_score"] == pytest.approx(expected)


@pytest.mark.parametrize("target", [None, dict(timeslot=0, soc_kwh=1.0, mode="at_least"),
    dict(timeslot=1, soc_kwh=1.0, tolerance_kwh=0.02, mode="exact")])
def test_two_slot_oracle_matches_branch_modes_premiums_targets_and_original_reporting(target):
    params = kernel_request(initial=0.713, prices=(0.4, 1.0), usage=(0.43, 0.81),
        pv=(0.37, 0.29), target=target)
    params.mode_switch_cost = 0.37
    params.throughput_cost_per_kwh = 0.13
    params.action_deadband_kwh = 0.3
    before = params.model_dump()
    expected, _ = persistence_oracle(params, spread=0.5, premium=0.03)
    result = factory(energy_intervals=2, pv_scenario_spread=0.5, first_action_rollout=True,
        rollout_pv_persistence=True, grid_charge_premium_per_kwh=0.03)(params)
    assert result["diagnostics"]["initial_pv_persistence_search_score"] == pytest.approx(expected)
    replay = evaluate_schedule(params, policies(result))
    assert result["diagnostics"]["evaluation"]["score"] == pytest.approx(replay.score)
    assert result["projections"]["projected_cost"] == pytest.approx(replay.tariff_cost)
    assert params.model_dump() == before


def test_independent_tail_resets_at_slot_two():
    params = request(initial=1.0, ingress=2, prices=(0.0, 0.0, 1.0, 3.0),
        usage=(0.0, 0.0, 1.0, 0.5), pv=(0.25, 0.0, 0.25, 0.0))
    expected, _ = persistence_oracle(params)
    wrong_tail, _ = persistence_oracle(params, persist_tail=True)
    assert expected != pytest.approx(wrong_tail)
    result = factory(energy_intervals=12, pv_scenario_spread=1.0, first_action_rollout=True,
        rollout_pv_persistence=True)(params)
    assert result["diagnostics"]["initial_pv_persistence_search_score"] == pytest.approx(expected)


def test_ties_and_zero_spread_keep_original_action_priority():
    params = request(prices=(0.0,) * 4, usage=(0.0,) * 4, pv=(0.5,) * 4)
    result = factory(pv_scenario_spread=1.0, first_action_rollout=True, rollout_pv_persistence=True)(params)
    assert policies(result)[0][0] == "self_consume"
    zero = factory(first_action_rollout=True, rollout_pv_persistence=True)(params)
    ordinary = factory(first_action_rollout=True)(params)
    assert zero["entities"] == ordinary["entities"]
    assert zero["diagnostics"]["rollout_pv_persistence_evaluated"] is False
    assert zero["diagnostics"]["rollout_pv_persistence_applied"] is False


def test_fleet_passive_and_accepted_repair_disclose_persistence_not_applied():
    options = dict(pv_scenario_spread=1.0, first_action_rollout=True, rollout_pv_persistence=True)
    fleet = factory(**options)(request(batteries=2))
    assert fleet["diagnostics"]["rollout_pv_persistence_evaluated"] is False
    assert fleet["diagnostics"]["rollout_pv_persistence_applied"] is False
    passive = hybrid_factory(**options)(request(pv=(0.0, 0.0, 10.0, 0.0)))
    assert passive["diagnostics"]["hybrid_gate"]["rollout_pv_persistence_applied"] is False
    params = request(initial=2.1, prices=(1.2, 0.3, 1.2, 0.3), usage=(0.0, 0.0, 1.1, 1.1),
        pv=(0.0, 0.2, 0.0, 0.2), target=dict(timeslot=3, soc_kwh=1.1, tolerance_kwh=0.02, mode="exact"))
    repaired = factory(energy_intervals=4, **options)(params)
    assert repaired["diagnostics"]["target_repair"]["improved"] is True
    assert repaired["diagnostics"]["rollout_pv_persistence_evaluated"] is True
    assert repaired["diagnostics"]["rollout_pv_persistence_applied"] is False
    assert repaired["diagnostics"]["target_repair"]["rollout_pv_persistence_applied"] is False


def test_hybrid_matches_direct_value_and_does_not_depend_on_request_date():
    params = request(pv=(0.25, 0.4, 0.0, 0.0))
    options = dict(energy_intervals=6, pv_scenario_spread=1.0, grid_charge_premium_per_kwh=0.03,
        first_action_rollout=True, rollout_pv_persistence=True)
    direct = factory(**options)(params)
    hybrid = hybrid_factory(**options)(params)
    assert hybrid["entities"] == direct["entities"]
    assert hybrid["projections"] == direct["projections"]
    shifted = params.model_copy(update={"plan_start": params.plan_start.replace(month=1)})
    assert hybrid_factory(**options)(shifted)["entities"] == hybrid["entities"]
