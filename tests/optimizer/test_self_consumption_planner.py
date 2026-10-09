"""Self-consumption retains its baseline policy while supplying live features."""

from datetime import timedelta

import pytest

from custom_components.wattplan.optimizer.models import OptimizationParams
from custom_components.wattplan.optimizer.planners import create_planner
from custom_components.wattplan.optimizer.planners.self_consumption import factory


def request():
    return OptimizationParams(
        plan_start="2026-10-09T00:00:00+00:00", slot_minutes=15,
        grid_import_price_per_kwh=[-1, 0.2, 1, 2],
        grid_export_price_per_kwh=[0.1] * 4,
        usage_kwh=[0.4] * 4, solar_input_kwh=[0, 2, 0, 0],
        battery_entities=[{
            "name": "pv", "initial_kwh": 0, "minimum_kwh": 0.5,
            "capacity_kwh": 2, "charge_curve_kwh": [1],
            "discharge_curve_kwh": [0.5], "can_charge_from": 3,
            "charge_efficiency": 0.9, "discharge_efficiency": 0.8,
        }],
        comfort_entities=[],
    )


@pytest.mark.parametrize("make_planner", [factory, lambda: create_planner("self-consumption")])
def test_pv_restores_reserve_without_ever_grid_charging(make_planner):
    params = request()
    result = make_planner()(params)
    schedule = result["entities"][0]["schedule"]
    assert [item["state"] for item in schedule] == ["self_consume"] * 4
    assert [item["level"] for item in schedule] == pytest.approx([0, 0.9, 0.5, 0.5])
    assert all(flow["grid_charge_kwh"] == 0 for flow in result["energy_flows"]["per_slot"])
    assert result["suboptimal_reasons"] == ["battery_min_unmet"]
    checks = result["diagnostics"]["evaluation"]["reserve_checks"]
    assert [item["timeslot"] for item in checks] == [0]
    assert result["diagnostics"]["evaluations"] == 1
    assert "successful_solves" not in result


def test_observed_target_remains_unmet_even_at_negative_import_price():
    params = request()
    battery = params.battery_entities[0].model_dump()
    battery["target"] = {"timeslot": 0, "soc_kwh": 1}
    payload = params.model_dump()
    payload["battery_entities"] = [battery]
    planner = factory()
    result = planner(OptimizationParams(**payload))
    assert planner.capabilities.targets == "observed"
    assert result["entities"][0]["schedule"][0]["state"] == "self_consume"
    assert "battery_target_unmet" in result["suboptimal_reasons"]
    assert any("not optimized" in note for note in result["diagnostics"]["capability_notes"])


def test_multiple_batteries_respect_ingress_efficiencies_and_conservation():
    payload = request().model_dump()
    payload["battery_entities"].append({
        **payload["battery_entities"][0], "name": "grid-only",
        "can_charge_from": 1, "initial_kwh": 1, "minimum_kwh": 0.2,
    })
    result = factory()(OptimizationParams(**payload))
    assert len(result["entities"]) == 2
    assert all(item["state"] == "self_consume" for entity in result["entities"] for item in entity["schedule"])
    # The grid-only battery cannot capture the PV surplus in slot 1.
    grid_only = result["entities"][1]["schedule"]
    assert grid_only[1]["level"] == pytest.approx(grid_only[0]["level"])
    for slot, flow in enumerate(result["energy_flows"]["per_slot"]):
        assert flow["grid_import_kwh"] - flow["grid_export_kwh"] == pytest.approx(
            flow["load_kwh"] - payload["solar_input_kwh"][slot]
        )
        assert flow["grid_charge_kwh"] == 0


def test_no_equipment_still_returns_complete_tariff_projections():
    params = request().model_copy(update={"battery_entities": []})
    result = factory()(params)
    assert result["entities"] == []
    assert len(result["projections"]["per_slot"]) == 4
    assert result["projections"]["projected_cost"] == pytest.approx(-0.4 - 0.16 + 0.4 + 0.8)
    assert not result["suboptimal"]


@pytest.mark.parametrize("registered", [False, True])
def test_comfort_commitment_and_optional_recommendations_survive_replanning(registered):
    payload = request().model_dump()
    payload.update(
        rolling_window_slots=4,
        comfort_entities=[{
            "name": "heat", "power_usage_kwh": 0.3,
            "target_on_slots_per_rolling_window": 1,
            "min_consecutive_on_slots": 3, "max_consecutive_off_slots": 3,
            "is_on_now": False, "off_streak_slots_now": 3,
            "on_history": [False, False, False],
        }],
        optional_entities=[{
            "name": "washer", "duration_timeslots": 1,
            "start_after_timeslot": 0, "start_before_timeslot": 4,
            "energy_kwh": 0.3, "options": 2,
        }],
    )
    params = OptimizationParams(**payload)
    before = params.model_dump(mode="json")
    planner = create_planner("self-consumption") if registered else factory()
    result = planner(params)
    assert params.model_dump(mode="json") == before
    assert planner.capabilities.comfort and planner.capabilities.optional_loads == "supported"
    heat = next(entity for entity in result["entities"] if entity["name"] == "heat")
    assert all(slot["enabled"] for slot in heat["schedule"][:3])
    assert len(result["optional_entity_options"][0]["options"]) == 2
    assert result["execution_time"] >= 0
    for slot, flow in enumerate(result["energy_flows"]["per_slot"]):
        assert flow["base_load_kwh"] == pytest.approx(0.4)
        assert flow["comfort_load_kwh"] == pytest.approx(0.3 * heat["schedule"][slot]["enabled"])
    next_comfort = params.comfort_entities[0].model_copy(update={
        "is_on_now": True, "on_history": [False, False, True], "off_streak_slots_now": 0,
    })
    following = params.model_copy(update={
        "plan_start": params.plan_start + timedelta(minutes=15),
        "state": result["state"], "comfort_entities": [next_comfort],
    })
    next_result = planner(following)
    assert next_result["diagnostics"]["comfort"]["continuity"] == "aligned"
    next_heat = next(entity for entity in next_result["entities"] if entity["name"] == "heat")
    assert all(slot["enabled"] for slot in next_heat["schedule"][:2])
    assert all(slot["state"] == "self_consume" for slot in next_result["entities"][0]["schedule"])
