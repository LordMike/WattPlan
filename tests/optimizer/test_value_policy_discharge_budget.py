"""Layer budget geometry tested against exact causal action enumeration."""

import math

import pytest

from custom_components.wattplan.optimizer.physics import transition
from custom_components.wattplan.optimizer.planners.evaluation import evaluate_schedule
from custom_components.wattplan.optimizer.planners.hybrid_policy import factory as hybrid_factory
from custom_components.wattplan.optimizer.planners.value_policy import factory
from tests.optimizer.test_dynamic_programming_planner import policies, request
from tests.optimizer.test_value_policy_paired_demand import joint_oracle
from tests.optimizer.test_value_policy_pv_tail import tail_oracle


OPTIONS = dict(energy_intervals=6, pv_scenario_spread=1.0,
    paired_demand_scenario_spread=0.25, paired_demand_scenario_slots=12,
    grid_charge_premium_per_kwh=0.03, include_initial_knot=True)


def proof_request():
    params = request(initial=2.5, prices=(0.01, 100.0, 100.0, 100.0),
        usage=(0.5, 0.2, 0.0, 0.3))
    params.slot_minutes = 60
    battery = params.battery_entities[0]
    battery.capacity_kwh, battery.minimum_kwh = 10.0, 1.0
    battery.charge_curve_kwh = battery.discharge_curve_kwh = [5.0]
    battery.charge_efficiency = battery.discharge_efficiency = 0.9
    return params


@pytest.mark.parametrize("planner", [factory, hybrid_factory])
@pytest.mark.parametrize("setting", [0, 1, None, 0.0, "true"])
def test_requires_strict_boolean(planner, setting):
    with pytest.raises(ValueError, match="include_discharge_budget_knot"):
        planner(include_discharge_budget_knot=setting)


@pytest.mark.parametrize("planner", [factory, hybrid_factory])
@pytest.mark.parametrize("extra", [{}, dict(include_charge_knots=True),
    dict(first_action_rollout=True),
    dict(first_action_rollout=True, rollout_pv_persistence=True),
    dict(pv_tail_low_probability=1 / 3)])
def test_false_preserves_full_output(planner, extra, monkeypatch):
    monkeypatch.setattr("custom_components.wattplan.optimizer.planners.shared.perf_counter", lambda: 0.0)
    monkeypatch.setattr("custom_components.wattplan.optimizer.planners.reporting.perf_counter", lambda: 0.0)
    params = proof_request()
    options = dict(OPTIONS, **extra)
    if extra.get("rollout_pv_persistence"):
        options["paired_demand_scenario_spread"] = 0.0
    assert planner(**options)(params) == planner(**options, include_discharge_budget_knot=False)(params)


@pytest.mark.parametrize("rollout", [False, True])
def test_saved_false_preserve_case_matches_exact_causal_oracle(rollout):
    params = proof_request()
    options = dict(OPTIONS, first_action_rollout=rollout)
    before = factory(**options)(params)
    result = factory(**options, include_discharge_budget_knot=True)(params)
    expected, action = joint_oracle(params, pv_spread=1.0, demand_spread=0.25, slots=12, premium=0.03)
    assert expected == pytest.approx((0.0, 0.0), abs=1e-10)
    assert action == "self_consume"
    assert policies(result)[0][0] == action
    if not rollout:
        assert policies(before)[0][0] == "preserve"
        assert before["projections"]["projected_cost"] == pytest.approx(0.005)
    assert result["diagnostics"]["initial_value_policy_expected_score"] == pytest.approx(expected, abs=1e-10)
    assert result["projections"]["projected_cost"] == pytest.approx(0.0, abs=1e-10)
    diag = result["diagnostics"]
    assert diag["discharge_budget_knot_nodes_added"] == 4
    assert diag["discharge_budget_knot_node_counts_by_slot"] == [10, 10, 10, 10, 9]
    assert diag["energy_knots"] == 9
    # Each layer's inventory is independently sufficient under adverse passive
    # physics. Different suffix nodes exercise next-layer interpolation.
    values = diag["discharge_budget_knot_values_by_slot"]
    assert values[:4] == pytest.approx([1 + 1.25 / 0.9, 1 + 0.625 / 0.9,
                                      1 + 0.375 / 0.9, 1 + 0.375 / 0.9])
    for slot, initial in enumerate(values[:4]):
        level = initial
        for i in range(slot, 4):
            step = transition(params.battery_entities, [level], ["self_consume"],
                usage=params.usage_kwh[i] * 1.25, pv=0.0)
            assert step.grid_import == pytest.approx(0.0, abs=1e-10)
            level = step.levels[0]


def test_rate_bound_efficiency_and_unavoidable_imports_are_physical():
    params = proof_request()
    params.usage_kwh = [2.0, 0.5, 0.0, 0.25]
    params.battery_entities[0].discharge_curve_kwh = [1.0, 1.0]
    params.battery_entities[0].discharge_efficiency = 0.5
    result = factory(**dict(OPTIONS, paired_demand_scenario_slots=1),
                     include_discharge_budget_knot=True)(params)
    diag = result["diagnostics"]
    assert diag["discharge_budget_knot_values_by_slot"][:4] == pytest.approx([4.5, 2.5, 1.5, 1.5])
    assert diag["discharge_budget_knot_unavoidable_adverse_import_kwh"] == pytest.approx(1.5)
    # At the first node, a 2.5 kWh adverse load still imports 1.5 kWh due to rate.
    step = transition(params.battery_entities, [4.5], ["self_consume"], usage=2.5, pv=0.0)
    assert step.grid_import == pytest.approx(1.5)


def test_capacity_excess_and_exact_dedup_do_not_add_nodes():
    params = request(initial=0.0, usage=(1.0, 1.0, 1.0, 1.0))
    result = factory(energy_intervals=3, include_discharge_budget_knot=True)(params)
    diag = result["diagnostics"]
    assert diag["discharge_budget_knot_capacity_exceeded_slots"] == [0]
    assert diag["discharge_budget_knot_deduplicated_slots"] == [1, 2, 3]
    assert diag["discharge_budget_knot_nodes_added"] == 0
    assert diag["discharge_budget_knot_node_counts_by_slot"] == [4] * 5


@pytest.mark.parametrize("unsupported", ["curve", "pv"])
def test_unsupported_requests_keep_old_tables_and_disclose_reason(unsupported):
    params = request(initial=0.8, pv=(0.25,) * 4)
    options = dict(energy_intervals=6, pv_scenario_spread=0.5)
    if unsupported == "curve":
        params.battery_entities[0].discharge_curve_kwh = [1.0, 0.5]
        options["pv_scenario_spread"] = 1.0
    before = factory(**options)(params)
    result = factory(**options, include_discharge_budget_knot=True)(params)
    assert result["entities"] == before["entities"]
    assert result["projections"] == before["projections"]
    diag = result["diagnostics"]
    assert diag["discharge_budget_knot_omission_reason"]
    assert diag["discharge_budget_knot_nodes_added"] == 0
    assert diag["discharge_budget_knot_applied"] is False


@pytest.mark.parametrize("pv_scale", [0.0, 1.0])
def test_supported_low_pv_zero_variable_charging_and_below_reserve(pv_scale):
    params = proof_request()
    params.solar_input_kwh = [0.25] * 4
    params.battery_entities[0].charge_curve_kwh = [5.0, 3.0, 0.2]
    params.battery_entities[0].initial_kwh = 0.5
    snapshot = params.model_dump()
    result = factory(**OPTIONS, pv_scale=pv_scale, include_discharge_budget_knot=True)(params)
    diag = result["diagnostics"]
    assert diag["discharge_budget_knot_evaluated"] is True
    assert diag["discharge_budget_knot_applied"] is True
    assert params.model_dump() == snapshot
    assert all(math.isfinite(x) for x in diag["evaluation"]["score"])
    assert diag["evaluation"]["score"] == pytest.approx(evaluate_schedule(params, policies(result)).score)


def test_targets_physical_modes_and_premium_match_exact_joint_oracle():
    params = request(initial=0.75, prices=(0.4, 1.0, 0.0, 0.0),
        usage=(0.5, 0.5, 0.0, 0.0), pv=(0.25, 0.25, 0.0, 0.0),
        target=dict(timeslot=1, soc_kwh=1.0, mode="at_least"))
    params.mode_switch_cost, params.throughput_cost_per_kwh, params.action_deadband_kwh = 0.37, 0.13, 0.3
    options = dict(OPTIONS, energy_intervals=24, paired_demand_scenario_spread=0.5,
        paired_demand_scenario_slots=1)
    expected, _ = joint_oracle(params, pv_spread=1.0, demand_spread=0.5, slots=1, premium=0.03)
    result = factory(**options, include_discharge_budget_knot=True)(params)
    assert result["diagnostics"]["initial_value_policy_expected_score"] == pytest.approx(expected)
    replay = evaluate_schedule(params, policies(result))
    assert result["diagnostics"]["evaluation"]["score"] == pytest.approx(replay.score)
    assert result["projections"]["projected_cost"] == pytest.approx(replay.tariff_cost)


def test_hybrid_forwarding_and_fleet_passive_omissions():
    params = proof_request()
    value = factory(**OPTIONS, include_discharge_budget_knot=True)(params)
    hybrid = hybrid_factory(**OPTIONS, prerefill_guard=True, include_discharge_budget_knot=True)(params)
    assert hybrid["entities"] == value["entities"]
    assert hybrid["diagnostics"]["hybrid_gate"]["discharge_budget_knot_applied"] is True
    for params, planner in [(request(batteries=2), factory),
                            (request(pv=(0.0, 0.0, 10.0, 0.0)), hybrid_factory)]:
        result = planner(include_discharge_budget_knot=True)(params)
        normal = planner()(params)
        assert result["entities"] == normal["entities"]
        diag = result["diagnostics"].get("hybrid_gate", result["diagnostics"])
        assert diag["discharge_budget_knot_applied"] is False


def test_weighted_pv_tail_modes_keep_exact_causal_score():
    params = request(initial=0.75, prices=(0.4, 1.0, 0.0, 0.0),
        usage=(0.5, 0.5, 0.0, 0.0), pv=(0.25, 0.5, 0.0, 0.0))
    params.mode_switch_cost, params.throughput_cost_per_kwh = 0.37, 0.13
    expected, _ = tail_oracle(params, premium=0.03)
    options = dict(OPTIONS, energy_intervals=24, paired_demand_scenario_spread=0.5,
        paired_demand_scenario_slots=1, pv_tail_low_probability=1 / 3)
    result = factory(**options, include_discharge_budget_knot=True)(params)
    assert result["diagnostics"]["initial_value_policy_expected_score"] == pytest.approx(expected)


def test_accepted_repair_replaces_budget_tables():
    params = request(initial=2.1, prices=(1.2, 0.3, 1.2, 0.3),
        usage=(0.0, 0.0, 1.1, 1.1), pv=(0.0, 0.2, 0.0, 0.2),
        target=dict(timeslot=3, soc_kwh=1.1, tolerance_kwh=0.02, mode="exact"))
    result = factory(energy_intervals=4, pv_scenario_spread=1.0,
        paired_demand_scenario_spread=0.25, include_discharge_budget_knot=True)(params)
    assert result["diagnostics"]["target_repair"]["improved"] is True
    assert result["diagnostics"]["discharge_budget_knot_evaluated"] is True
    assert result["diagnostics"]["discharge_budget_knot_applied"] is False
