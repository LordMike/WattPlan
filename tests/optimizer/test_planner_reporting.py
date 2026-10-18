"""Accepted-plan projections and advisory fixed-policy replay contracts."""

import pytest

from custom_components.wattplan.optimizer.models import OptimizationParams
from custom_components.wattplan.optimizer.planners.evaluation import evaluate_schedule
from custom_components.wattplan.optimizer.planners.reporting import (
    cost_projection,
    enrich_result,
    optional_entity_options,
)


def params_for_reporting(**overrides):
    payload = {
        "grid_import_price_per_kwh": [0.2, 0.4, 0.6, 0.8],
        "grid_export_price_per_kwh": [0.1] * 4,
        "usage_kwh": [1.0] * 4,
        "solar_input_kwh": [0.0] * 4,
        "battery_entities": [
            {
                "name": "battery",
                "initial_kwh": 2.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 4.0,
                "charge_curve_kwh": [1.0],
                "discharge_curve_kwh": [1.0],
                "can_charge_from": 3,
            }
        ],
        "comfort_entities": [],
    }
    payload.update(overrides)
    return OptimizationParams(**payload)


def report(params, policies):
    evaluation = evaluate_schedule(params, policies)
    result = {"projections": {"final_soc_kwh": {"retained": 1.0}}, "execution_time": 0.123}
    return enrich_result(params, policies, evaluation, result)


def test_projection_reuses_physics_and_keeps_preferences_out_of_cost():
    params = params_for_reporting(
        throughput_cost_per_kwh=0.3, mode_switch_cost=0.1
    )
    result = report(params, [["grid_charge", "preserve", "self_consume", "self_consume"]])
    projection = result["projections"]
    assert projection["baseline_cost"] == pytest.approx(2.0)
    assert projection["projected_cost"] == pytest.approx(0.8)
    assert projection["projected_savings_cost"] == pytest.approx(1.2)
    assert projection["projected_savings_pct"] == pytest.approx(60.0)
    assert [slot["projected_cost"] for slot in projection["per_slot"]] == pytest.approx([0.4, 0.4, 0.0, 0.0])
    assert projection["final_soc_kwh"] == {"retained": 1.0}
    assert result["avg_price"] == pytest.approx(0.8 / 3.0)
    assert result["fitness"] == pytest.approx(0.8 + 3 * 0.3 + 3 * 0.1)
    assert result["execution_time"] == 0.123
    assert "successful_solves" not in result
    assert "reused_steps" not in result
    flows = result["energy_flows"]["per_slot"]
    assert [slot["grid_import_kwh"] for slot in flows] == [2.0, 1.0, 0.0, 0.0]
    assert [slot["load_kwh"] for slot in flows] == [2.0, 1.0, 0.0, 0.0]
    assert flows[0]["grid_charge_kwh"] == 1.0
    assert not result["suboptimal"]


def test_negative_export_baseline_has_zero_percentage_and_no_import_sentinel():
    params = params_for_reporting(battery_entities=[], usage_kwh=[0.0] * 4, solar_input_kwh=[2.0] * 4)
    result = report(params, [])
    assert result["projections"]["baseline_cost"] == pytest.approx(-0.8)
    assert result["projections"]["projected_savings_pct"] == 0.0
    assert result["avg_price"] == 1000.0
    assert all(slot["grid_export_kwh"] == 2.0 for slot in result["energy_flows"]["per_slot"])


@pytest.mark.parametrize("mode,target", [("at_least", 3.0), ("at_most", 1.0), ("exact", 3.0)])
def test_target_violations_become_observed_plan_health(mode, target):
    params = params_for_reporting()
    params.battery_entities[0].target = params.battery_entities[0].TargetParams(
        timeslot=2, mode=mode, soc_kwh=target, tolerance_kwh=0.1
    )
    result = report(params, [["preserve"] * 4])
    assert result["suboptimal"] and result["overconstrained"]
    assert result["problems"] == result["suboptimal_reasons"] == ["battery_target_unmet"]
    assert result["diagnostics"]["evaluation"]["constraint_status"] == "observed_plan_violations_not_infeasibility_proof"


def test_target_tolerance_and_out_of_horizon_do_not_mark_a_miss():
    params = params_for_reporting()
    for timeslot in (2, 8):
        params.battery_entities[0].target = params.battery_entities[0].TargetParams(
            timeslot=timeslot, mode="exact", soc_kwh=2.1, tolerance_kwh=0.2
        )
        assert not report(params, [["preserve"] * 4])["suboptimal"]


def test_initial_reserve_is_not_a_violation_if_restored_in_first_slot():
    params = params_for_reporting()
    params.battery_entities[0].initial_kwh = 0.0
    params.battery_entities[0].minimum_kwh = 1.0
    recovered = report(params, [["grid_charge", "preserve", "preserve", "preserve"]])
    assert not recovered["suboptimal"]
    missed = report(params, [["preserve"] * 4])
    assert missed["suboptimal_reasons"] == ["battery_min_unmet"]
    assert len(missed["diagnostics"]["evaluation"]["reserve_checks"]) == 4


def optional(**overrides):
    return {
        "name": "washer",
        "duration_timeslots": 1,
        "energy_kwh": 1.0,
        "start_before_timeslot": 4,
        "options": 4,
        **overrides,
    }


def test_appliance_cost_includes_later_battery_replacement_energy():
    params = params_for_reporting(
        grid_import_price_per_kwh=[0.1, 0.2, 1.0, 0.5],
        usage_kwh=[0.0, 0.0, 2.0, 0.0],
        optional_entities=[optional()],
    )
    params.battery_entities[0].discharge_curve_kwh = [2.0]
    policies = [["self_consume"] * 4]
    evaluation = evaluate_schedule(params, policies)
    original_levels = [step.levels.copy() for step in evaluation.steps]
    options = optional_entity_options(params, policies, evaluation)[0]["options"]
    by_start = {option["start_timeslot"]: option for option in options}
    # Free battery energy at slot zero costs 1.0 to replace at the later demand.
    assert by_start[0]["incremental_cost"] == pytest.approx(1.0)
    assert options[0]["start_timeslot"] == 3
    assert options[0]["incremental_cost"] == pytest.approx(0.5)
    assert [step.levels for step in evaluation.steps] == original_levels
    for start in range(4):
        loaded = params.model_copy(update={"usage_kwh": [usage + (slot == start) for slot, usage in enumerate(params.usage_kwh)]})
        brute = evaluate_schedule(loaded, policies).tariff_cost - evaluation.tariff_cost
        assert by_start[start]["incremental_cost"] == pytest.approx(brute)


def test_optional_gap_profile_negative_tariffs_and_tie_order():
    params = params_for_reporting(
        battery_entities=[],
        grid_import_price_per_kwh=[-1.0, -1.0, -1.0, -1.0],
        optional_entities=[optional(duration_timeslots=2, energy_kwh=[0.5, 1.5], options=2)],
    )
    result = report(params, [])
    options = result["optional_entity_options"][0]["options"]
    assert [option["start_timeslot"] for option in options] == [0, 2]
    assert [option["end_timeslot"] for option in options] == [2, 4]
    assert [option["incremental_cost"] for option in options] == [-2.0, -2.0]
    assert all(option["delta_from_best"] == 0.0 for option in options)
    assert result["projections"]["projected_cost"] == -4.0


def test_optional_replay_with_multiple_batteries_curves_losses_and_pv():
    params = params_for_reporting(
        usage_kwh=[0.3, 0.9, 1.8, 0.1],
        solar_input_kwh=[2.3, 0.4, 0.0, 1.0],
        optional_entities=[optional()],
    )
    first = params.battery_entities[0]
    first.charge_efficiency = 0.8
    first.discharge_efficiency = 0.9
    first.charge_curve_kwh = [1.3, 0.2]
    second = first.model_copy(update={
        "name": "second", "initial_kwh": 0.4, "minimum_kwh": 0.2,
        "capacity_kwh": 2.0, "can_charge_from": 2,
    })
    params.battery_entities.append(second)
    policies = [
        ["grid_charge", "preserve", "self_consume", "grid_charge"],
        ["self_consume", "self_consume", "self_consume", "preserve"],
    ]
    evaluation = evaluate_schedule(params, policies)
    ranked = optional_entity_options(params, policies, evaluation)[0]["options"]
    for option in ranked:
        start = option["start_timeslot"]
        loaded = params.model_copy(update={
            "usage_kwh": [usage + (slot == start) for slot, usage in enumerate(params.usage_kwh)]
        })
        expected = evaluate_schedule(loaded, policies).tariff_cost - evaluation.tariff_cost
        assert option["incremental_cost"] == pytest.approx(expected)


def test_comfort_finalizer_can_restore_original_baseline():
    original = params_for_reporting(battery_entities=[])
    prepared = original.model_copy(update={"usage_kwh": [2.0] * 4})
    result = report(prepared, [])
    costs = [slot["projected_cost"] for slot in result["projections"]["per_slot"]]
    restored = cost_projection(original, costs)
    assert restored["baseline_cost"] == pytest.approx(2.0)
    assert restored["projected_cost"] == pytest.approx(4.0)
    assert restored["projected_savings_cost"] == pytest.approx(-2.0)
