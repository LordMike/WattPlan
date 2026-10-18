"""Independent user-visible acceptance tests across every published preset."""

import json
from datetime import timedelta

import pytest

from custom_components.wattplan.optimizer.models import OptimizationParams
from custom_components.wattplan.optimizer.planners.presets import PRESETS, run_preset
from custom_components.wattplan.optimizer.planners.registry import BUILTINS, create_planner
from custom_components.wattplan.optimizer.planners.comfort import prepare_comfort
from custom_components.wattplan.optimizer.planners.evaluation import evaluate_schedule
from tests.optimizer.planner_conformance import assert_public_result, battery_policies


def preset_capabilities(name):
    preset = PRESETS[name]
    return create_planner(preset.planner, dict(preset.options)).capabilities


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


def assert_live_result(params, result):
    """Public outputs must be internally consistent regardless of entry point."""
    json.dumps(result, allow_nan=False)
    assert result["execution_time"] >= 0
    assert isinstance(result["suboptimal"], bool)
    assert isinstance(result["suboptimal_reasons"], list)
    assert {item["name"] for item in result["entities"]} == {"house", "heater"}
    assert all(len(item["schedule"]) == 8 for item in result["entities"])
    heater = next(item for item in result["entities"] if item["name"] == "heater")
    enabled = [item["enabled"] for item in heater["schedule"]]
    history = [True, False, False] + enabled
    # In this fixture, one ON slot per four slots also enforces max-off == 3.
    assert all(sum(history[slot:slot + 4]) >= 1 for slot in range(8))
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
    baseline = sum(
        max(usage - pv, 0) * buy - max(pv - usage, 0) * sell
        for usage, pv, buy, sell in zip(
            params.usage_kwh, params.solar_input_kwh,
            params.grid_import_price_per_kwh, params.grid_export_price_per_kwh,
            strict=True,
        )
    )
    assert projection["baseline_cost"] == pytest.approx(baseline)
    assert projection["projected_cost"] == pytest.approx(sum(costs))
    assert [slot["projected_cost"] for slot in projection["per_slot"]] == pytest.approx(costs)
    assert projection["projected_savings_cost"] == pytest.approx(
        projection["baseline_cost"] - projection["projected_cost"]
    )
    options = result["optional_entity_options"]
    assert options[0]["name"] == "washer" and len(options[0]["options"]) == 2
    assert options[0]["options"][0]["delta_from_best"] == pytest.approx(0)
    assert result["state"]


@pytest.mark.parametrize("name", BUILTINS)
def test_replanning_uses_current_inputs_and_preserves_comfort_commitments(name):
    params = request()
    heater = params.comfort_entities[0]
    heater.on_history = [False, False, False]
    heater.off_streak_slots_now = 3
    heater.min_consecutive_on_slots = 3
    planner = create_planner(name)
    first = planner(params)
    first_heat = next(e for e in first["entities"] if e["type"] == "comfort")
    assert all(p["enabled"] for p in first_heat["schedule"][:3])
    following = params.model_copy(deep=True)
    following.state = first["state"]
    following.plan_start += timedelta(minutes=params.slot_minutes)
    following.usage_kwh = [0.7] * 8
    following.solar_input_kwh = [0.0] * 8
    following.grid_import_price_per_kwh = [2.0] * 8
    following.battery_entities[0].initial_kwh = 0.2
    following.comfort_entities[0].on_history = [False, False, True]
    following.comfort_entities[0].is_on_now = True
    following.comfort_entities[0].off_streak_slots_now = 0
    before = following.model_dump(mode="json")
    second = planner(following)
    assert_public_result(following, second)
    assert following.model_dump(mode="json") == before
    second_heat = next(e for e in second["entities"] if e["type"] == "comfort")
    assert all(p["enabled"] for p in second_heat["schedule"][:2])
    # No solar or charging incentive: observed inventory, not the previous plan,
    # bounds every future battery level. Comfort demand must still be included.
    assert all(p["level"] <= 0.2 + 1e-6 for e in second["entities"]
               if e["type"] == "battery" for p in e["schedule"])


@pytest.mark.parametrize("preset", PRESETS)
def test_all_presets_have_complete_outputs_and_account_for_comfort(preset):
    params = request()
    before = params.model_dump(mode="json")
    result = run_preset(params, preset)
    assert params.model_dump(mode="json") == before
    assert_public_result(params, result)
    assert result["diagnostics"]["planner"]["preset"] == preset
    assert_live_result(params, result)
    assert not result["suboptimal"], result["suboptimal_reasons"]


@pytest.mark.parametrize("name", BUILTINS)
@pytest.mark.parametrize("constrained", [False, True], ids=["healthy", "target-and-reserve"])
def test_direct_factories_and_registry_have_the_same_live_features(name, constrained):
    params = request()
    if constrained:
        payload = params.model_dump()
        payload["battery_entities"][0].update(
            initial_kwh=0.1, minimum_kwh=0.5,
            target={"timeslot": 3, "soc_kwh": 1.0, "mode": "at_least"},
        )
        params = OptimizationParams(**payload)
    before = params.model_dump(mode="json")
    planner = create_planner(name)
    capabilities = planner.capabilities
    assert capabilities.comfort
    assert capabilities.optional_loads == "supported"
    assert "rolling_window_slots" not in capabilities.ignored_preferences
    direct = planner(params)
    assert params.model_dump(mode="json") == before
    assert_public_result(params, direct)
    assert_live_result(params, direct)
    if not constrained or (capabilities.targets == "optimized" and capabilities.reserve_recovery):
        assert not direct["suboptimal"], direct["suboptimal_reasons"]
    else:
        assert direct["suboptimal"]
        assert "battery_target_unmet" in direct["suboptimal_reasons"]
        assert any("targets are measured but not optimized" in note
                   for note in direct["diagnostics"]["capability_notes"])
    if name != "highs":
        replay = evaluate_schedule(prepare_comfort(params).params, battery_policies(direct))
        assert direct["projections"]["projected_cost"] == pytest.approx(replay.tariff_cost)
        assert direct["diagnostics"]["evaluation"]["score"] == pytest.approx(replay.score)
        assert direct["execution_time"] >= direct["diagnostics"]["reporting"]["reporting_seconds"]
        assert "successful_solves" not in direct


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


@pytest.mark.parametrize("preset", [name for name in PRESETS if preset_capabilities(name).reserve_recovery])
def test_optimizing_presets_recover_reserve_before_waiting_for_cheaper_power(preset):
    payload = request(comfort=False, optional=False).model_dump()
    payload.update(usage_kwh=[0] * 8, solar_input_kwh=[0] * 8,
                   grid_import_price_per_kwh=[10, 9, 8, 7, 6, 5, 4, 1])
    payload["battery_entities"][0].update(initial_kwh=0, minimum_kwh=0.8)
    result = run_preset(OptimizationParams(**payload), preset)
    assert result["entities"][0]["schedule"][0]["state"] == "grid_charge"
    assert result["entities"][0]["schedule"][0]["level"] >= 0.8 - 1e-6
    assert "battery_min_unmet" not in result["suboptimal_reasons"]


@pytest.mark.parametrize("preset", [name for name in PRESETS if preset_capabilities(name).targets == "optimized"])
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


@pytest.mark.parametrize("preset", [name for name in PRESETS if not preset_capabilities(name).reserve_recovery])
def test_passive_household_controls_observe_initial_reserve_without_forced_grid_recovery(preset):
    payload = request(comfort=False, optional=False).model_dump()
    payload.update(usage_kwh=[0] * 8, solar_input_kwh=[0] * 8,
                   grid_import_price_per_kwh=[10, 9, 8, 7, 6, 5, 4, 1])
    payload["battery_entities"][0].update(initial_kwh=0, minimum_kwh=0.8)
    result = run_preset(OptimizationParams(**payload), preset)
    assert all(action["state"] == "self_consume" for action in result["entities"][0]["schedule"])
    assert "battery_min_unmet" in result["suboptimal_reasons"]


@pytest.mark.parametrize("preset", [name for name in PRESETS if preset_capabilities(name).targets == "observed"])
def test_target_observers_report_reachable_household_target_without_claiming_to_pursue_it(preset):
    payload = request(comfort=False, optional=False).model_dump()
    payload.update(usage_kwh=[0] * 8, solar_input_kwh=[0] * 8)
    payload["battery_entities"][0].update(initial_kwh=0.2,target={"timeslot":0,"soc_kwh":1.0})
    result = run_preset(OptimizationParams(**payload), preset)
    assert result["entities"][0]["schedule"][0]["level"] == pytest.approx(0.2)
    assert "battery_target_unmet" in result["suboptimal_reasons"]
    assert any("targets are measured but not optimized" in note
               for note in result["diagnostics"]["capability_notes"])
