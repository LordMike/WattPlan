"""Interpolated value policy: exact replay, target knots and forecast isolation."""

from itertools import product

import pytest

from custom_components.wattplan.optimizer.planners.evaluation import evaluate_schedule
from custom_components.wattplan.optimizer.planners.value_policy import factory
from tests.optimizer.test_dynamic_programming_planner import request, policies
from tests.optimizer.planner_conformance import assert_conforms, conformance_params


@pytest.mark.parametrize("params", [request(), request(initial=1.0),
    request(ingress=2, pv=(1.0, 0.0, 1.0, 0.0)),
    request(target=dict(timeslot=1, soc_kwh=2.0, mode="at_least")),
    request(prices=(-0.3, 0.2, 1.0, 0.0), usage=(0.0, 0.0, 1.0, 0.0))])
def test_aligned_knots_match_tiny_exhaustive_oracle(params):
    result = factory(energy_intervals=3)(params)
    choices = ("self_consume", "preserve", "grid_charge") if params.battery_entities[0].can_charge_from & 1 else ("self_consume", "preserve")
    oracle = min(evaluate_schedule(params, [actions]).score
                 for actions in product(choices, repeat=len(params.usage_kwh)))
    assert evaluate_schedule(params, policies(result)).score == pytest.approx(oracle)


@pytest.mark.parametrize("params", conformance_params())
def test_shared_physical_and_reporting_contract(params):
    assert_conforms("custom_components.wattplan.optimizer.planners.value_policy:factory", params, {"energy_intervals":4})


def test_forecast_scaling_does_not_mutate_inputs_or_report_scaled_physics():
    params = request(initial=1.123, usage=(0.6, 0.8, 1.4, 0.2), pv=(0.7, 0.0, 0.1, 0.0))
    before = params.model_dump()
    result = factory(energy_intervals=4, pv_scale=0.5, demand_scale=1.2,
                     terminal_value_fraction=0.3)(params)
    assert params.model_dump() == before
    replay = evaluate_schedule(params, policies(result))
    assert result["diagnostics"]["evaluation"]["score"] == pytest.approx(replay.score)
    for action, step in zip(result["entities"][0]["schedule"], replay.steps, strict=True):
        assert action["level"] == pytest.approx(step.levels[0])


def test_multiple_batteries_explicitly_report_joint_fallback():
    result = factory(pv_scale=0.5)(request(batteries=2))
    assert result["diagnostics"]["value_policy_fallback"] == "bounded_joint_state_dp"
    assert result["diagnostics"]["forecast_adjustments_applied"] is False


@pytest.mark.parametrize("options", [{"energy_intervals":True}, {"energy_intervals":1},
    {"pv_scale":-1}, {"demand_scale":float("nan")}, {"terminal_value_fraction":float("inf")}])
def test_reject_invalid_configuration(options):
    with pytest.raises(ValueError):
        factory(**options)



def test_value_table_tracks_physical_switches_and_throughput():
    params = request(initial=1.0, prices=(0.1, 0.8, 0.2, 1.1), usage=(1.0, 1.0, 1.0, 1.0))
    params.mode_switch_cost = 0.37
    params.throughput_cost_per_kwh = 0.13
    params.action_deadband_kwh = 0.4
    result = factory(energy_intervals=3)(params)
    oracle = min(evaluate_schedule(params, [actions]).score for actions in
                 product(("self_consume", "preserve", "grid_charge"), repeat=4))
    assert evaluate_schedule(params, policies(result)).score == pytest.approx(oracle)


def test_exact_target_interpolation_miss_gets_bounded_physical_repair():
    params = request(initial=2.1, prices=(1.2, 0.3, 1.2, 0.3),
                     usage=(0.0, 0.0, 1.1, 1.1), pv=(0.0, 0.2, 0.0, 0.2),
                     target=dict(timeslot=3, soc_kwh=1.1, tolerance_kwh=0.02, mode="exact"))
    result = factory(energy_intervals=4)(params)
    oracle = min(evaluate_schedule(params, [actions]).score for actions in
                 product(("self_consume", "preserve", "grid_charge"), repeat=4))
    assert evaluate_schedule(params, policies(result)).score == pytest.approx(oracle)
    assert result["diagnostics"]["target_repair"]["improved"] is True
    assert result["diagnostics"]["evaluations"] == 3
    assert result["diagnostics"]["target_repair"]["evaluations"] == 2
    assert result["diagnostics"]["target_repair"]["original_target_miss_kwh"] == pytest.approx(0.18)


@pytest.mark.parametrize("curve", [[5.0], [5.0, 1.0]])
def test_charge_knots_follow_shared_flat_and_tapered_headroom_boundaries(curve):
    from custom_components.wattplan.optimizer.physics import battery_power_limits, grid_charge_headroom_sufficient
    from custom_components.wattplan.optimizer.planners.value_policy import _charge_knots
    battery = request().battery_entities[0]
    battery.capacity_kwh = 10.0
    battery.charge_efficiency = 0.9
    battery.charge_curve_kwh = curve
    roots = sorted(_charge_knots(battery))
    assert len(roots) == 2
    saturation, eligibility = roots
    power = battery_power_limits(battery, saturation)[0]
    assert (battery.capacity_kwh - saturation) / battery.charge_efficiency == pytest.approx(power)
    low, high = eligibility - 1e-7, eligibility + 1e-7
    assert grid_charge_headroom_sufficient(battery, low, battery_power_limits(battery, low)[0])
    assert not grid_charge_headroom_sufficient(battery, high, battery_power_limits(battery, high)[0])
    result = factory(energy_intervals=4, include_charge_knots=True)(request())
    assert result["diagnostics"]["include_charge_knots"] is True


def test_charge_knots_setting_requires_boolean():
    with pytest.raises(ValueError, match="include_charge_knots"):
        factory(include_charge_knots=1)
