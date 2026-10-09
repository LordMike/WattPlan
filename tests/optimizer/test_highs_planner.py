"""The HiGHS adapter keeps production scheduling, state and reporting behavior."""

import base64
from datetime import timedelta
import json

import pytest

from custom_components.wattplan.optimizer import OptimizationParams, optimize
from custom_components.wattplan.optimizer.planners.presets import run_preset
from custom_components.wattplan.optimizer.planners.registry import create_planner


def _params():
    return OptimizationParams(
        plan_start="2026-10-09T00:00:00+00:00", slot_minutes=15,
        grid_import_price_per_kwh=[0.2, 0.4, 0.8, 0.3] * 4,
        grid_export_price_per_kwh=[0.1] * 16,
        usage_kwh=[0.2] * 16, solar_input_kwh=[0.0, 0.0, 0.5, 0.0] * 4,
        lookahead_slots=8, rolling_window_slots=8,
        battery_entities=[{
            "name": "battery", "initial_kwh": 1.0, "minimum_kwh": 0.2,
            "capacity_kwh": 2.0, "charge_curve_kwh": [0.5],
            "discharge_curve_kwh": [0.5], "can_charge_from": 3,
        }],
        comfort_entities=[{
            "name": "heat", "power_usage_kwh": 0.2,
            "target_on_slots_per_rolling_window": 2,
            "min_consecutive_on_slots": 2, "min_consecutive_off_slots": 1,
            "max_consecutive_off_slots": 4, "is_on_now": False,
            "off_streak_slots_now": 2,
            "on_history": [True, True, False, False, True, False, False],
        }],
        optional_entities=[{
            "name": "washer", "duration_timeslots": 2,
            "start_after_timeslot": 0, "start_before_timeslot": 16,
            "energy_kwh": 0.5, "options": 2,
        }],
    )


@pytest.mark.parametrize("preset", ["highs", "highs-8", "highs-16", "highs-24"])
def test_highs_preset_matches_direct_api_equipment_and_reporting(preset):
    params = _params()
    lookahead = 8 if preset == "highs" else int(preset.split("-")[1])
    expected = optimize(params.model_copy(update={"lookahead_slots": lookahead}))
    actual = run_preset(params, preset)
    for field in (
        "entities", "optional_entity_options", "projections", "energy_flows",
        "suboptimal", "suboptimal_reasons", "problems", "successful_solves",
        "reused_steps", "cadence",
    ):
        assert actual[field] == expected[field]
    assert "evaluation" not in actual["diagnostics"]
    assert "search" not in actual["diagnostics"]
    assert actual["execution_time"] >= 0
    assert params.lookahead_slots == 8


def test_highs_accepts_legacy_state_and_preserves_same_slot_cadence():
    params = _params()
    initial = optimize(params)
    wrapped = create_planner("highs")(
        params.model_copy(update={"state": initial["state"]})
    )
    assert wrapped["diagnostics"]["planner"]["state_reset_reason"] is None
    assert wrapped["cadence"]["reason"] == "same_window_refresh"
    assert wrapped["cadence"]["phase"] == initial["cadence"]["phase"]
    payload = json.loads(base64.urlsafe_b64decode(wrapped["state"]))["planner_state"]["payload"]
    assert payload
    later = params.model_copy(update={
        "state": wrapped["state"], "plan_start": params.plan_start + timedelta(minutes=15)
    })
    next_result = create_planner("highs")(later)
    assert next_result["diagnostics"]["planner"]["state_reset_reason"] is None
    assert next_result["cadence"]["phase"] == 1
    assert len(next_result["entities"][0]["schedule"]) == 16
