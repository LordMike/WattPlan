"""Single-site grid direction and permitted battery charging provenance."""

import pytest

from custom_components.wattplan.optimizer.models import BatteryEntityParams, OptimizationParams
from custom_components.wattplan.optimizer.physics import transition
from custom_components.wattplan.optimizer.planners import create_planner
from custom_components.wattplan.optimizer.planners.evaluation import build_result, evaluate_schedule
from custom_components.wattplan.optimizer.planners.reporting import optional_entity_options


def battery(name="battery", ingress=1):
    return BatteryEntityParams(
        name=name, initial_kwh=0, minimum_kwh=0, capacity_kwh=3,
        charge_curve_kwh=[1], discharge_curve_kwh=[1], can_charge_from=ingress,
    )


def assert_conservation(step, *, usage, pv):
    assert step.grid_import * step.grid_export == pytest.approx(0)
    assert step.grid_import + pv + sum(step.discharge) == pytest.approx(
        usage + step.charge + step.grid_export
    )


@pytest.mark.parametrize("pv", [0.25, 1.0, 2.0])
def test_grid_only_battery_cannot_import_while_pv_surplus_exports(pv):
    step = transition([battery()], [0], ["grid_charge"], usage=0, pv=pv)
    assert step.levels == [0]
    assert step.grid_charge == [0]
    assert step.pv_charge == [0]
    assert step.grid_import == 0
    assert step.grid_export == pv
    assert_conservation(step, usage=0, pv=pv)


def test_household_demand_absorbing_pv_allows_true_grid_charging():
    step = transition([battery()], [0], ["grid_charge"], usage=1, pv=0.25)
    assert step.levels == [1]
    assert step.grid_charge == [1]
    assert step.grid_import == 1.75
    assert_conservation(step, usage=1, pv=0.25)


def test_dual_ingress_uses_pv_then_grid_to_fill_charge_rate():
    step = transition([battery(ingress=3)], [0], ["grid_charge"], usage=0, pv=0.4)
    assert step.levels == [1]
    assert step.pv_charge == [0.4]
    assert step.grid_charge == pytest.approx([0.6])
    assert step.grid_import == pytest.approx(0.6)
    assert_conservation(step, usage=0, pv=0.4)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("pv", [1.0, 1.5])
def test_residual_surplus_is_checked_after_all_permitted_pv_allocations(reverse, pv):
    batteries = [battery("grid", 1), battery("solar", 2)]
    modes = ["grid_charge", "preserve"]
    if reverse:
        batteries.reverse()
        modes.reverse()
    step = transition(batteries, [0, 0], modes, usage=0, pv=pv)
    by_name = dict(zip((b.name for b in batteries), step.levels, strict=True))
    assert by_name == {"grid": 1.0 if pv == 1 else 0.0, "solar": 1.0}
    assert step.grid_export == pytest.approx(pv - 1)
    assert_conservation(step, usage=0, pv=pv)


@pytest.mark.parametrize("fraction", [0.25, 0.5])
def test_fractional_latched_grid_action_still_respects_site_direction(fraction):
    blocked = transition(
        [battery()], [0], ["grid_charge"], usage=0, pv=fraction,
        slot_fraction=fraction, grid_eligible=[True],
    )
    assert blocked.grid_charge == [0]
    assert blocked.levels == [0]
    assert_conservation(blocked, usage=0, pv=fraction)
    allowed = transition(
        [battery()], [0], ["grid_charge"], usage=fraction, pv=fraction,
        slot_fraction=fraction, grid_eligible=[True],
    )
    assert allowed.grid_charge == [fraction]
    assert_conservation(allowed, usage=fraction, pv=fraction)


@pytest.mark.parametrize("usage,expected_discharge", [(0, 0), (1, 1)])
def test_opposed_battery_policies_only_discharge_for_household_load(usage, expected_discharge):
    step = transition(
        [battery("charge"), battery("discharge")], [0, 2],
        ["grid_charge", "self_consume"], usage=usage, pv=0,
    )
    assert step.discharge == [0, expected_discharge]
    assert step.grid_charge == [1, 0]
    assert step.grid_import == 1
    assert_conservation(step, usage=usage, pv=0)


@pytest.mark.parametrize("planner", ["highs", "dynamic-programming", "charge-window", "cross-entropy"])
def test_forbidden_surplus_charge_cannot_falsely_satisfy_target(planner):
    raw = battery().model_dump()
    raw["target"] = {"timeslot": 0, "soc_kwh": 1}
    params = OptimizationParams(
        grid_import_price_per_kwh=[0.2] * 4,
        grid_export_price_per_kwh=[0.1] * 4,
        usage_kwh=[0] * 4, solar_input_kwh=[1, 0, 0, 0],
        battery_entities=[raw], comfort_entities=[],
    )
    result = create_planner(planner)(params)
    first = result["energy_flows"]["per_slot"][0]
    assert first["grid_import_kwh"] == 0
    assert first["grid_export_kwh"] == 1
    assert result["entities"][0]["schedule"][0]["level"] == 0
    assert "battery_target_unmet" in result["problems"]
    assert result["entities"][0]["schedule"][0]["state"] != "grid_charge"


def normalization_request(*, initial=0, pv=None, ingress=1, optional=False):
    raw = battery(ingress=ingress).model_dump()
    raw["initial_kwh"] = initial
    return OptimizationParams(
        grid_import_price_per_kwh=[1.0] * 4,
        grid_export_price_per_kwh=[0.1] * 4,
        usage_kwh=[0] * 4, solar_input_kwh=pv or [0] * 4,
        battery_entities=[raw], comfort_entities=[],
        optional_entities=[{
            "name": "appliance", "duration_timeslots": 1,
            "start_before_timeslot": 1, "energy_kwh": 1.0, "options": 1,
        }] if optional else [],
    )


@pytest.mark.parametrize("params", [
    normalization_request(initial=3),
    normalization_request(initial=2.75),
    normalization_request(pv=[1, 0, 0, 0]),
    normalization_request(pv=[1, 0, 0, 0], ingress=3),
])
def test_zero_grid_action_normalization_preserves_exact_accepted_evaluation(params):
    candidate = [["grid_charge", "preserve", "preserve", "preserve"]]
    accepted = evaluate_schedule(params, candidate)
    assert accepted.steps[0].grid_charge[0] == 0.0
    result = build_result(params, candidate, evaluation=accepted)
    published = [[slot["state"] for slot in result["entities"][0]["schedule"]]]
    assert published[0][0] == "preserve"
    assert candidate[0][0] == "grid_charge"  # The search candidate is immutable here.
    assert evaluate_schedule(params, published) == accepted
    assert result["diagnostics"]["evaluation"]["normalized_zero_grid_charge_actions"] == 1


def test_nonzero_grid_flow_below_epsilon_is_not_normalized():
    params = normalization_request(pv=[1 - 5e-7, 0, 0, 0], ingress=3)
    candidate = [["grid_charge", "preserve", "preserve", "preserve"]]
    accepted = evaluate_schedule(params, candidate)
    assert 0 < accepted.steps[0].grid_charge[0] < 1e-6
    result = build_result(params, candidate, evaluation=accepted)
    assert result["entities"][0]["schedule"][0]["state"] == "grid_charge"
    assert result["diagnostics"]["evaluation"]["normalized_zero_grid_charge_actions"] == 0


def test_optional_costs_use_normalized_published_controls():
    params = normalization_request(pv=[1, 0, 0, 0], optional=True)
    candidate = [["grid_charge", "preserve", "preserve", "preserve"]]
    accepted = evaluate_schedule(params, candidate)
    result = build_result(params, candidate, evaluation=accepted)
    published = [[slot["state"] for slot in result["entities"][0]["schedule"]]]
    options = result["optional_entity_options"]
    assert options == optional_entity_options(params, published, accepted)
    assert options[0]["options"][0]["incremental_cost"] == pytest.approx(0.1)
    old_options = optional_entity_options(params, candidate, accepted)
    assert old_options[0]["options"][0]["incremental_cost"] == pytest.approx(1.1)
