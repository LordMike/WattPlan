"""Contracts the planner integration must preserve around comfort placement."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import json

import numpy as np
import pytest

from custom_components.wattplan.optimizer import optimize
from custom_components.wattplan.optimizer import mpc_power_optimizer as core
from custom_components.wattplan.optimizer.comfort_placement import (
    ComfortPlacementInput,
    place_comfort_schedules,
)
from custom_components.wattplan.optimizer.models import (
    OptimizationParams,
    normalize_calculation_input,
)
from tests.optimizer.benchmark_cases import build_case


def _comfort(
    schedule, *, name="comfort", history=None, window=None, target=1,
    min_on=1, min_off=1, max_off=None, current=True, off_streak=0,
    lock_remaining=0, lock_mode=None,
):
    schedule = tuple(schedule)
    window = window or len(schedule)
    return ComfortPlacementInput(
        name=name,
        schedule=schedule,
        on_history=tuple([True] * (window - 1) if history is None else history),
        rolling_window_slots=window,
        target_on_slots_per_rolling_window=target,
        min_consecutive_on_slots=min_on,
        min_consecutive_off_slots=min_off,
        max_consecutive_off_slots=max_off or len(schedule),
        is_on_now=current,
        off_streak_slots_now=off_streak,
        lock_remaining=lock_remaining,
        lock_mode=lock_mode,
    )


def _net_site_cost(*, powers, usage, solar, import_prices, export_prices):
    def evaluate(schedules):
        cost = 0.0
        for slot in range(len(usage)):
            demand = usage[slot] + sum(
                power * float(schedule[slot])
                for power, schedule in zip(powers, schedules, strict=True)
            )
            cost += max(demand - solar[slot], 0.0) * import_prices[slot]
            cost -= max(solar[slot] - demand, 0.0) * export_prices[slot]
        return cost

    return evaluate


def test_no_comfort_does_not_enter_search_or_invoke_cost_callback():
    def unexpected(_schedules):
        raise AssertionError("the no-comfort path must not price placement")

    result = place_comfort_schedules([], unexpected)

    assert result.status == "no_comfort"
    assert result.candidates_considered == result.candidates_evaluated == 0
    assert result.accepted_moves == 0


def test_multiple_comforts_share_pv_and_signed_tariffs_in_one_callback_plan():
    comforts = [
        _comfort([True, False, False, False], name="first"),
        _comfort([False, False, False, True], name="second"),
    ]
    evaluate = _net_site_cost(
        powers=[1.0, 1.0],
        usage=[0.0] * 4,
        solar=[0.0, 2.0, 0.0, 0.0],
        import_prices=[5.0, 3.0, -2.0, 4.0],
        export_prices=[0.0, 1.0, 0.0, 0.0],
    )

    result = place_comfort_schedules(comforts, evaluate)

    assert result.status == "improved"
    assert result.schedules == (
        (False, False, True, False),
        (False, False, True, False),
    )
    assert result.baseline_cost == pytest.approx(7.0)
    assert result.final_cost == pytest.approx(-6.0)
    assert result.accepted_moves == 2


def test_locked_history_constrained_baseline_is_not_priced_or_moved():
    comfort = _comfort(
        [False, False, True, True],
        history=[True] * 9,
        window=10,
        min_on=3,
        min_off=2,
        max_off=2,
        current=False,
        lock_remaining=2,
        lock_mode=False,
    )

    def unexpected(_schedules):
        raise AssertionError("infeasible placement must fall back without pricing")

    result = place_comfort_schedules([comfort], unexpected)

    assert result.status == "infeasible"
    assert result.schedules == (comfort.schedule,)
    assert result.candidates_considered == result.candidates_evaluated == 0


def test_candidate_budget_is_global_and_callback_work_stays_bounded():
    comforts = [
        _comfort([True, False, False, False, False], name="first"),
        _comfort([False, False, False, False, True], name="second"),
    ]
    calls = 0

    def flat_cost(_schedules):
        nonlocal calls
        calls += 1
        return 1.0

    result = place_comfort_schedules(
        comforts,
        flat_cost,
        max_candidates_per_comfort=3,
        max_total_candidates=4,
    )

    assert result.status == "unchanged"
    assert result.candidates_considered == result.candidates_evaluated == 4
    assert result.candidates_rejected == result.accepted_moves == 0
    assert calls == result.candidates_evaluated + 1


def test_cancellation_keeps_an_already_accepted_move_as_safe_fallback():
    comforts = [
        _comfort([True, False, False], name="first"),
        _comfort([True, False, False], name="second"),
    ]
    checks = 0

    def cancel():
        nonlocal checks
        checks += 1
        return checks >= 4

    def cost(schedules):
        return -float(
            sum(next(index for index, enabled in enumerate(schedule) if enabled)
                for schedule in schedules)
        )

    result = place_comfort_schedules(comforts, cost, should_cancel=cancel)

    assert result.status == "cancelled"
    assert result.schedules[0] == (False, False, True)
    assert result.accepted_moves == 1
    assert not result.violations[0]
    assert not result.violations[1]


def _optional_options(comfort_enabled):
    params = OptimizationParams(
        grid_import_price_per_kwh=[5.0, 1.0, 3.0, 4.0],
        grid_export_price_per_kwh=[0.0] * 4,
        solar_input_kwh=[1.0, 0.0, 0.0, 0.0],
        usage_kwh=[0.0] * 4,
        battery_entities=[],
        comfort_entities=[],
        optional_entities=[{
            "name": "washer",
            "duration_timeslots": 1,
            "start_before_timeslot": 4,
            "energy_kwh": 1.0,
            "options": 4,
        }],
    )
    normalized = normalize_calculation_input(params)
    # The production suggestion seam receives normalized comforts and their final
    # accepted ON matrix, rather than the original placement candidate.
    class OneComfort:
        power_usage_kwh = 1.0

    comfort = [OneComfort()]
    modes = []
    base = core._replay_policy_cost(
        normalized.grid_import_prices,
        normalized.grid_export_prices,
        normalized.usage,
        normalized.solar_input,
        [],
        modes,
        comfort_enabled,
        comfort,
        np.zeros(4),
    )
    return core._optional_entity_options(
        normalized.optional_entities[0],
        normalized.grid_import_prices,
        normalized.grid_export_prices,
        normalized.usage,
        normalized.solar_input,
        [],
        modes,
        comfort_enabled,
        comfort,
        base,
    )


def test_optional_suggestions_are_scored_against_final_accepted_comfort_plan():
    comfort = _comfort([False, True, False, False], current=True)
    placement = place_comfort_schedules(
        [comfort],
        _net_site_cost(
            powers=[1.0], usage=[0.0] * 4, solar=[1.0, 0.0, 0.0, 0.0],
            import_prices=[5.0, 1.0, 3.0, 4.0], export_prices=[0.0] * 4,
        ),
    )

    original = _optional_options(
        np.asarray([[False, True, False, False]], dtype=np.float64)
    )
    accepted = _optional_options(
        np.asarray(placement.schedules, dtype=np.float64)
    )

    assert placement.schedules == ((True, False, False, False),)
    assert [option["start_timeslot"] for option in original] == [0, 1, 2, 3]
    assert [option["start_timeslot"] for option in accepted] == [1, 2, 3, 0]


def _production_payload(*, timed=False, battery=False):
    payload = {
        "grid_import_price_per_kwh": [5.0, 4.0, -1.0, 3.0],
        "grid_export_price_per_kwh": [0.0] * 4,
        "solar_input_kwh": [0.0, 1.0, 0.0, 0.0],
        "usage_kwh": [0.0] * 4,
        "rolling_window_slots": 4,
        "lookahead_slots": 4,
        "infer_battery_preserve_policy": False,
        "battery_entities": [],
        "comfort_entities": [{
            "name": "heat",
            "target_on_slots_per_rolling_window": 1,
            "min_consecutive_on_slots": 1,
            "min_consecutive_off_slots": 1,
            "max_consecutive_off_slots": 4,
            "power_usage_kwh": 1.0,
            "is_on_now": True,
            "on_history": [False, False, True],
            "off_streak_slots_now": 0,
        }],
    }
    if timed:
        payload.update(
            plan_start=datetime(2026, 9, 14, tzinfo=UTC),
            slot_minutes=15,
        )
    if battery:
        payload["battery_entities"] = [{
            "name": "battery",
            "initial_kwh": 1.0,
            "minimum_kwh": 0.0,
            "capacity_kwh": 2.0,
            "charge_curve_kwh": [1.0],
            "discharge_curve_kwh": [1.0],
            "can_charge_from": 3,
        }]
    return payload


def test_production_no_comfort_bypasses_placement_and_extra_pass(monkeypatch):
    payload = _production_payload()
    payload["comfort_entities"] = []
    calls = 0
    original = core._run_mpc

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("no-comfort planning must bypass placement")

    monkeypatch.setattr(core, "_run_mpc", counted)
    monkeypatch.setattr(core, "place_comfort_schedules", unexpected)

    result = optimize(OptimizationParams(**payload))

    assert calls == 1
    assert result["successful_solves"] == 0
    assert "comfort_placement" not in result


def test_production_changed_comfort_demand_adds_at_most_one_battery_pass(
    monkeypatch,
):
    payload = _production_payload(battery=True)
    calls = []
    original = core._run_mpc

    def counted(*args, **kwargs):
        calls.append(kwargs.get("fixed_comfort_override"))
        return original(*args, **kwargs)

    monkeypatch.setattr(core, "_run_mpc", counted)

    result = optimize(OptimizationParams(**payload))
    placement = result["comfort_placement"]

    assert placement["demand_changed"] is True
    assert placement["additional_planning_passes"] == 1
    assert len(calls) == 2
    assert calls[0] is None
    assert calls[1] is not None
    assert placement["additional_successful_solves"] <= 4
    assert result["successful_solves"] <= 8


def test_actual_plan_cost_regression_retains_baseline(monkeypatch):
    payload = _production_payload()
    score_calls = 0
    original = core._score_result

    def worsen_second(*args, **kwargs):
        nonlocal score_calls
        score_calls += 1
        score = original(*args, **kwargs)
        if score_calls == 2:
            return (*score[:3], score[3] + 100.0, score[4])
        return score

    monkeypatch.setattr(core, "_score_result", worsen_second)

    result = optimize(OptimizationParams(**payload))
    enabled = [point["enabled"] for point in result["entities"][0]["schedule"]]

    assert enabled == [False, False, False, True]
    assert result["comfort_placement"]["fallback_reason"] == "final_cost_worsened"
    assert result["comfort_placement"]["final_projected_cost"] == pytest.approx(
        result["comfort_placement"]["baseline_projected_cost"]
    )


def test_invalid_placement_cost_falls_back_to_baseline(monkeypatch):
    payload = _production_payload()

    def invalid(*_args, **_kwargs):
        raise ValueError("non-finite candidate cost")

    monkeypatch.setattr(core, "place_comfort_schedules", invalid)
    result = optimize(OptimizationParams(**payload))

    assert result["comfort_placement"]["status"] == "invalid_cost"
    assert result["comfort_placement"]["fallback_reason"] == (
        "invalid_cost_evaluation"
    )
    assert result["comfort_placement"]["additional_planning_passes"] == 0


def test_prefix_state_and_receipt_use_the_final_comfort_schedule():
    payload = _production_payload(timed=True)
    for key in (
        "grid_import_price_per_kwh",
        "grid_export_price_per_kwh",
        "solar_input_kwh",
        "usage_kwh",
    ):
        payload[key] *= 3
    payload["lookahead_slots"] = 12
    first = optimize(OptimizationParams(**payload))
    state = json.loads(base64.urlsafe_b64decode(first["state"]).decode("utf-8"))
    enabled = [point["enabled"] for point in first["entities"][0]["schedule"]]

    assert state["comfort_on"][0] == [float(value) for value in enabled]
    assert state["cadence_prefix"]["comfort_locks"]["heat"]["mode"] == enabled[0]

    payload["state"] = first["state"]
    payload["plan_start"] += timedelta(minutes=15)
    refreshed = optimize(OptimizationParams(**payload))
    refreshed_state = json.loads(
        base64.urlsafe_b64decode(refreshed["state"]).decode("utf-8")
    )
    refreshed_enabled = [
        point["enabled"] for point in refreshed["entities"][0]["schedule"]
    ]

    assert refreshed["cadence"]["mode"] == "repair"
    assert refreshed_state["comfort_on"][0] == [
        float(value) for value in refreshed_enabled
    ]
    assert refreshed_state["cadence_prefix"]["comfort_locks"]["heat"][
        "mode"
    ] == refreshed_enabled[0]


def test_benchmark_comfort_plan_reports_bounded_work_and_actual_quality():
    payload = build_case("comfort-flexible", slots=96, lookahead=48)

    result = optimize(OptimizationParams(**payload))
    placement = result["comfort_placement"]

    assert placement["candidates_considered"] <= 48
    assert placement["candidate_cost_calls"] <= 49
    assert placement["candidates_generated"] >= placement["candidates_considered"]
    assert placement["candidates_unvisited"] == (
        placement["candidates_generated"] - placement["candidates_considered"]
    )
    assert placement["candidates_not_improving"] == (
        placement["candidates_evaluated"] - placement["accepted_moves"]
    )
    assert placement["additional_planning_passes"] <= 1
    assert (
        placement["final_projected_cost"]
        <= placement["baseline_projected_cost"] + 1e-6
    )


def test_prefix_fallback_shares_one_comfort_replan_budget(monkeypatch):
    payload = _production_payload(timed=True, battery=True)
    for key in (
        "grid_import_price_per_kwh",
        "grid_export_price_per_kwh",
        "solar_input_kwh",
        "usage_kwh",
    ):
        payload[key] *= 3
    payload["lookahead_slots"] = 12
    first = optimize(OptimizationParams(**payload))

    payload["state"] = first["state"]
    payload["plan_start"] += timedelta(minutes=15)
    monkeypatch.setattr(
        "custom_components.wattplan.optimizer.prefix_planner._tail_violation",
        lambda *_args: "forced_test_fallback",
    )
    calls = 0
    original = core._run_mpc

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(core, "_run_mpc", counted)
    result = optimize(OptimizationParams(**payload))
    placement = result["comfort_placement"]

    assert result["cadence"]["mode"] == "fallback_full"
    assert placement["additional_planning_passes"] <= 1
    assert calls <= 3
    assert "discarded_prefix_work" in placement
