"""Independent user-visible acceptance tests across every published preset."""

import json

import pytest

from custom_components.wattplan.optimizer.models import OptimizationParams
from custom_components.wattplan.optimizer.planners.presets import PRESETS, run_preset


def request(*, battery=True, comfort=True, optional=True):
    return OptimizationParams(
        plan_start="2026-10-09T00:00:00+00:00", slot_minutes=15,
        grid_import_price_per_kwh=[1, 3, 1, 2, 1, 3, 1, 2],
        grid_export_price_per_kwh=[0.1] * 8,
        usage_kwh=[0.2] * 8, solar_input_kwh=[0, 0, 0.8, 0, 0, 0, 0.8, 0],
        battery_entities=[{
            "name": "house", "initial_kwh": 1.0, "minimum_kwh": 0.2,
            "capacity_kwh": 3.0, "charge_curve_kwh": [1.0],
            "discharge_curve_kwh": [1.0], "can_charge_from": 3,
            "charge_efficiency": 0.9, "discharge_efficiency": 0.9,
        }] if battery else [],
        comfort_entities=[{
            "name": "heater", "target_on_slots_per_rolling_window": 1,
            "max_consecutive_off_slots": 3, "power_usage_kwh": 0.4,
            "is_on_now": False, "off_streak_slots_now": 2,
            "on_history": [True, False, False],
        }] if comfort else [],
        rolling_window_slots=4,
        optional_entities=[{
            "name": "washer", "duration_timeslots": 2,
            "start_after_timeslot": 1, "start_before_timeslot": 8,
            "energy_kwh": [0.3, 0.5], "options": 2,
        }] if optional else [],
        lookahead_slots=8,
    )


@pytest.mark.parametrize("preset", PRESETS)
def test_all_presets_have_complete_outputs_and_account_for_comfort(preset):
    params = request()
    before = params.model_dump(mode="json")
    result = run_preset(params, preset)
    assert params.model_dump(mode="json") == before
    json.dumps(result, allow_nan=False)
    assert result["diagnostics"]["planner"]["preset"] == preset
    assert result["execution_time"] >= 0
    assert isinstance(result["suboptimal"], bool)
    assert isinstance(result["suboptimal_reasons"], list)
    assert {item["name"] for item in result["entities"]} == {"house", "heater"}
    assert all(len(item["schedule"]) == 8 for item in result["entities"])
    heater = next(item for item in result["entities"] if item["name"] == "heater")
    enabled = [item["enabled"] for item in heater["schedule"]]
    history = [True, False, False] + enabled
    assert all(sum(history[slot:slot + 4]) >= 1 for slot in range(8))
    assert all(any(history[slot:slot + 4]) for slot in range(8))
    costs = []
    for slot, flow in enumerate(result["energy_flows"]["per_slot"]):
        assert flow["base_load_kwh"] == pytest.approx(params.usage_kwh[slot])
        assert flow["comfort_load_kwh"] == pytest.approx(0.4 * enabled[slot])
        assert flow["grid_import_kwh"] - flow["grid_export_kwh"] == pytest.approx(
            flow["load_kwh"] - params.solar_input_kwh[slot], abs=1e-6
        )
        costs.append(flow["grid_import_kwh"] * params.grid_import_price_per_kwh[slot]
                     - flow["grid_export_kwh"] * params.grid_export_price_per_kwh[slot])
    projection = result["projections"]
    assert projection["projected_cost"] == pytest.approx(sum(costs))
    assert [slot["projected_cost"] for slot in projection["per_slot"]] == pytest.approx(costs)
    assert projection["projected_savings_cost"] == pytest.approx(
        projection["baseline_cost"] - projection["projected_cost"]
    )
    options = result["optional_entity_options"]
    assert options[0]["name"] == "washer" and len(options[0]["options"]) == 2
    assert options[0]["options"][0]["delta_from_best"] == pytest.approx(0)
    assert not result["suboptimal"], result["suboptimal_reasons"]


@pytest.mark.parametrize("preset", PRESETS)
def test_all_presets_report_unreachable_targets_and_reserves(preset):
    params = request(comfort=False, optional=False)
    payload = params.model_dump()
    payload["battery_entities"][0].update({
        "initial_kwh": 0.0, "minimum_kwh": 0.2, "can_charge_from": 0,
        "target": {"timeslot": 1, "soc_kwh": 1.0},
    })
    params = OptimizationParams(**payload)
    result = run_preset(params, preset)
    assert result["suboptimal"] and result["overconstrained"]
    assert {"battery_target_unmet", "battery_min_unmet"} <= set(result["suboptimal_reasons"])


@pytest.mark.parametrize("preset", [name for name in PRESETS if name != "self-consumption"])
def test_optimizing_presets_recover_reserve_before_waiting_for_cheaper_power(preset):
    payload = request(comfort=False, optional=False).model_dump()
    payload.update(usage_kwh=[0] * 8, solar_input_kwh=[0] * 8,
                   grid_import_price_per_kwh=[10, 9, 8, 7, 6, 5, 4, 1])
    payload["battery_entities"][0].update(initial_kwh=0, minimum_kwh=0.8)
    result = run_preset(OptimizationParams(**payload), preset)
    assert result["entities"][0]["schedule"][0]["state"] == "grid_charge"
    assert result["entities"][0]["schedule"][0]["level"] >= 0.8 - 1e-6
    assert "battery_min_unmet" not in result["suboptimal_reasons"]


@pytest.mark.parametrize("preset", [name for name in PRESETS if name != "self-consumption"])
def test_optimizing_presets_meet_a_reachable_immediate_target(preset):
    payload = request(comfort=False, optional=False).model_dump()
    payload.update(usage_kwh=[0] * 8, solar_input_kwh=[0] * 8,
                   grid_import_price_per_kwh=[10, 9, 8, 7, 6, 5, 4, 1])
    payload["battery_entities"][0].update(
        initial_kwh=0.2, target={"timeslot": 0, "soc_kwh": 1.0},
    )
    result = run_preset(OptimizationParams(**payload), preset)
    assert result["entities"][0]["schedule"][0]["level"] >= 1.0 - 1e-6
    assert "battery_target_unmet" not in result["suboptimal_reasons"]
