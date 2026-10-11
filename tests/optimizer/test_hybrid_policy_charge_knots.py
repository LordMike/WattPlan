"""Hybrid forwards physical charge knots without changing its forecast gate."""

import pytest

from custom_components.wattplan.optimizer.planners.evaluation import evaluate_schedule
from custom_components.wattplan.optimizer.planners.hybrid_policy import factory
from custom_components.wattplan.optimizer.planners.value_policy import factory as value_factory
from tests.optimizer.test_dynamic_programming_planner import policies, request


@pytest.mark.parametrize("setting", [0, 1, None, "true", [], 0.0])
def test_charge_knots_uses_value_factory_boolean_validation(setting):
    with pytest.raises(ValueError, match="include_charge_knots"):
        factory(include_charge_knots=setting)


@pytest.mark.parametrize("spread", [0.0, 1.0])
@pytest.mark.parametrize("pv", [(0.0, 0.5, 0.0, 0.0), (0.0, 0.0, 10.0, 0.0)])
def test_explicit_false_preserves_complete_default_result(spread, pv, monkeypatch):
    monkeypatch.setattr("custom_components.wattplan.optimizer.planners.shared.perf_counter", lambda: 0.0)
    monkeypatch.setattr("custom_components.wattplan.optimizer.planners.reporting.perf_counter", lambda: 0.0)
    params = request(pv=pv)
    options = dict(energy_intervals=6, pv_scenario_spread=spread, grid_charge_premium_per_kwh=0.03)
    assert factory(**options)(params) == factory(**options, include_charge_knots=False)(params)


@pytest.mark.parametrize("premium", [0.0, 0.03])
def test_value_branch_matches_direct_value_with_scenarios_premium_and_exact_reporting(premium):
    params = request(initial=1.123, pv=(0.4, 0.0, 0.7, 0.0))
    params.battery_entities[0].charge_efficiency = 0.9
    before = params.model_dump()
    options = dict(energy_intervals=6, pv_scenario_spread=1.0,
                   grid_charge_premium_per_kwh=premium, include_charge_knots=True)
    hybrid = factory(**options)(params)
    direct = value_factory(**options)(params)
    assert hybrid["diagnostics"]["hybrid_gate"]["selected_policy"] == "value_policy"
    assert hybrid["diagnostics"]["hybrid_gate"]["include_charge_knots_applied"] is True
    assert hybrid["diagnostics"]["energy_knots"] > 8
    assert hybrid["entities"] == direct["entities"]
    assert hybrid["projections"] == direct["projections"]
    assert hybrid["diagnostics"]["initial_value_policy_expected_score"] == direct["diagnostics"]["initial_value_policy_expected_score"]
    replay = evaluate_schedule(params, policies(hybrid))
    assert hybrid["diagnostics"]["evaluation"]["score"] == pytest.approx(replay.score)
    assert params.model_dump() == before


def test_passive_branch_unchanged_and_discloses_unused_knots():
    params = request(pv=(0.0, 0.0, 10.0, 0.0))
    ordinary = factory()(params)
    enabled = factory(include_charge_knots=True)(params)
    assert enabled["diagnostics"]["hybrid_gate"]["selected_policy"] == "self_consumption"
    assert enabled["diagnostics"]["hybrid_gate"]["include_charge_knots"] is True
    assert enabled["diagnostics"]["hybrid_gate"]["include_charge_knots_applied"] is False
    assert enabled["entities"] == ordinary["entities"]
    assert enabled["projections"] == ordinary["projections"]
    assert "include_charge_knots" not in enabled["diagnostics"]


def test_target_keeps_priority_with_charge_knots_and_large_premium():
    params = request(target=dict(timeslot=0, soc_kwh=1.0, mode="at_least"),
                     pv=(0.0, 0.0, 10.0, 0.0))
    result = factory(include_charge_knots=True, grid_charge_premium_per_kwh=100.0)(params)
    assert result["diagnostics"]["hybrid_gate"]["forced_value_reason"] == "battery_target"
    assert policies(result)[0][0] == "grid_charge"
    assert result["diagnostics"]["evaluation"]["score"][0] == 0.0


def test_fleet_fallback_does_not_claim_charge_knots_applied():
    params = request(batteries=2)
    ordinary = factory()(params)
    enabled = factory(include_charge_knots=True)(params)
    assert enabled["diagnostics"]["value_policy_fallback"] == "bounded_joint_state_dp"
    assert enabled["diagnostics"]["hybrid_gate"]["include_charge_knots_applied"] is False
    assert enabled["entities"] == ordinary["entities"]
    assert enabled["projections"] == ordinary["projections"]


@pytest.mark.parametrize("preference", ["throughput_cost_per_kwh", "mode_switch_cost", "action_deadband_kwh"])
def test_preferences_force_value_and_remain_consistent_with_direct_value(preference):
    params = request(initial=1.0, pv=(0.0, 0.0, 10.0, 0.0))
    setattr(params, preference, 0.3)
    options = dict(energy_intervals=6, pv_scenario_spread=1.0,
                   grid_charge_premium_per_kwh=0.03, include_charge_knots=True)
    hybrid = factory(**options)(params)
    direct = value_factory(**options)(params)
    assert hybrid["diagnostics"]["hybrid_gate"]["forced_value_reason"] == "battery_preferences"
    assert hybrid["diagnostics"]["hybrid_gate"]["include_charge_knots_applied"] is True
    assert hybrid["entities"] == direct["entities"]
    assert hybrid["projections"] == direct["projections"]
