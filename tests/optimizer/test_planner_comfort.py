"""Shared comfort constraints, accounting, and measured continuity."""

import base64
from datetime import timedelta
import json

import pytest

from custom_components.wattplan.optimizer.comfort_placement import check_comfort_schedule
from custom_components.wattplan.optimizer.models import OptimizationParams, encode_state_blob
from custom_components.wattplan.optimizer.planners.comfort import prepare_comfort
from custom_components.wattplan.optimizer.planners.evaluation import build_result


def request(**changes):
    entity = {
        "name": "heater", "target_on_slots_per_rolling_window": 1,
        "max_consecutive_off_slots": 3, "power_usage_kwh": 0.5,
        "is_on_now": False, "off_streak_slots_now": 2,
        "on_history": [True, False, False],
    }
    entity.update(changes)
    return OptimizationParams(
        plan_start="2026-10-09T00:00:00+00:00", slot_minutes=15,
        grid_import_price_per_kwh=[1, 10, 1, 1, 1, 10, 1, 1],
        grid_export_price_per_kwh=[0] * 8,
        usage_kwh=[0.2] * 8, solar_input_kwh=[0] * 8,
        battery_entities=[], comfort_entities=[entity], rolling_window_slots=4,
    )


def result(prepared, state=None):
    return prepared.finalize(build_result(prepared.params, [], state=state))


def test_modeled_consumption_and_public_reporting_include_comfort_once():
    params = request()
    before = params.model_dump()
    prepared = prepare_comfort(params)
    actual = result(prepared)
    schedule = actual["entities"][0]["schedule"]
    assert not prepared.params.comfort_entities
    assert params.model_dump() == before
    assert prepared.params.usage_kwh == [0.2 + 0.5 * slot["enabled"] for slot in schedule]
    assert actual["projections"]["projected_cost"] == pytest.approx(sum(
        price * usage for price, usage in zip(params.grid_import_price_per_kwh, prepared.params.usage_kwh, strict=True)
    ))
    assert actual["projections"]["baseline_cost"] == pytest.approx(sum(params.grid_import_price_per_kwh) * 0.2)
    for flow, slot in zip(actual["energy_flows"]["per_slot"], schedule, strict=True):
        assert flow["base_load_kwh"] == 0.2
        assert flow["comfort_load_kwh"] == pytest.approx(0.5 * slot["enabled"])
        assert flow["load_kwh"] == pytest.approx(0.2 + 0.5 * slot["enabled"])
    assert not actual["suboptimal"]


def test_price_placement_moves_load_and_keeps_constraints():
    prepared = prepare_comfort(request())
    assert prepared.placement.accepted_moves > 0
    assert prepared.placement.final_cost < prepared.placement.baseline_cost
    assert not check_comfort_schedule(prepared.inputs[0], prepared.placement.schedules[0])


@pytest.mark.parametrize("history,minimum_on,minimum_off", [
    ((False, False, False), 1, 1),
    ((True, True, False), 3, 1),
    ((True, False, True), 2, 2),
])
def test_constraints_or_explicit_degradation_for_varied_history(history, minimum_on, minimum_off):
    current = history[-1]
    off = 0
    if not current:
        for value in reversed(history):
            if value:
                break
            off += 1
    prepared = prepare_comfort(request(on_history=list(history), is_on_now=current,
        off_streak_slots_now=off, min_consecutive_on_slots=minimum_on,
        min_consecutive_off_slots=minimum_off, target_on_slots_per_rolling_window=2))
    actual = result(prepared)
    violations = check_comfort_schedule(prepared.inputs[0], prepared.placement.schedules[0])
    assert bool(actual["suboptimal"]) == bool(violations)
    assert actual["diagnostics"]["comfort"]["violations"] == [
        {"entity": "heater", "code": item.code, "slot": item.slot} for item in violations
    ]


def test_missing_history_is_conservative_and_reported():
    prepared = prepare_comfort(request(on_history=None, on_slots_last_rolling_window=2))
    assert prepared.inputs[0].on_history == (True, False, False)
    actual = result(prepared)
    assert "comfort_history_unavailable" in actual["suboptimal_reasons"]
    assert actual["suboptimal"]


def test_impossible_early_target_is_reported_without_solver_failure():
    prepared = prepare_comfort(request(on_history=[False] * 3,
        target_on_slots_per_rolling_window=4, off_streak_slots_now=3))
    actual = result(prepared)
    assert all(slot["enabled"] for slot in actual["entities"][0]["schedule"])
    assert "comfort_target_unmet" in actual["suboptimal_reasons"]
    assert actual["comfort_placement"]["status"] == "infeasible"


def test_lock_and_battery_state_roundtrip_across_aligned_replanning():
    params = request(on_history=[False, False, False], off_streak_slots_now=3,
                     min_consecutive_on_slots=3)
    prepared = prepare_comfort(params)
    battery_state = encode_state_blob({"v": 1, "battery_marker": "preserved"})
    actual = result(prepared, battery_state)
    next_entity = params.comfort_entities[0].model_copy(update={
        "on_history": [False, False, True], "is_on_now": True,
        "off_streak_slots_now": 0,
    })
    following = params.model_copy(update={
        "plan_start": params.plan_start + timedelta(minutes=15),
        "state": actual["state"], "comfort_entities": [next_entity],
    })
    replanned = prepare_comfort(following)
    assert replanned.params.state == battery_state
    assert replanned.continuity == "aligned"
    assert replanned.inputs[0].lock_remaining == 2
    assert replanned.placement.schedules[0][:2] == (True, True)
    blob = json.loads(base64.urlsafe_b64decode(result(replanned)["state"]))
    assert blob["comfort_state"]["entities"][0]["boundaries"][0][1] == 2
    removed = prepare_comfort(following.model_copy(update={"comfort_entities": []}))
    assert removed.params.state == battery_state
    assert removed.finalize({"state": battery_state}) == {"state": battery_state}


def test_actual_runtime_changes_reject_predicted_continuity():
    params = request()
    actual = result(prepare_comfort(params))
    altered = params.comfort_entities[0].model_copy(update={"is_on_now": True})
    prepared = prepare_comfort(params.model_copy(update={"state": actual["state"], "comfort_entities": [altered]}))
    assert prepared.continuity == "runtime_changed"


def test_no_comfort_is_exact_identity():
    params = request().model_copy(update={"comfort_entities": []})
    prepared = prepare_comfort(params)
    assert prepared.params is params
    original = {"state": None, "arbitrary": "unchanged"}
    assert prepared.finalize(original) is original


def test_two_comforts_have_independent_schedules_and_added_load():
    params = request()
    second = params.comfort_entities[0].model_copy(update={"name": "water", "power_usage_kwh": 1.0})
    prepared = prepare_comfort(params.model_copy(update={"comfort_entities": [params.comfort_entities[0], second]}))
    actual = result(prepared)
    assert [entity["name"] for entity in actual["entities"]] == ["heater", "water"]
    assert prepared.params.usage_kwh == [0.2 + 0.5 * left + right for left, right in zip(*prepared.placement.schedules, strict=True)]
