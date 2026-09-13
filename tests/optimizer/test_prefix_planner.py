"""Production cadence uses forecast timestamps, never call counts."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from custom_components.wattplan.optimizer import OptimizationParams, optimize
from custom_components.wattplan.optimizer import prefix_planner as planner
from custom_components.wattplan.optimizer.models import encode_state_blob


START = datetime(2026, 9, 11, tzinfo=UTC)


def payload(tick=0, state=None, *, horizon=16):
    return {
        "plan_start": START + timedelta(minutes=15 * tick),
        "slot_minutes": 15,
        "grid_import_price_per_kwh": [0.2] * horizon,
        "grid_export_price_per_kwh": [0.0] * horizon,
        "solar_input_kwh": [0.0] * horizon,
        "usage_kwh": [0.2] * horizon,
        "lookahead_slots": min(48, horizon),
        "battery_entities": [],
        "comfort_entities": [],
        "state": state,
    }


@pytest.fixture
def stub_core(monkeypatch):
    """Keep clock/state branching tests independent of native solver time."""
    calls = []

    def run(normalized, **kwargs):
        calls.append(kwargs)
        count = normalized.total_steps
        fresh = kwargs.get("policy_tail_start")
        state = {"v": 1, "num_steps": count, "entity_fingerprint": normalized.fingerprint}
        for key, values in (
            ("grid_import_price_per_kwh", normalized.grid_import_prices),
            ("grid_export_price_per_kwh", normalized.grid_export_prices),
            ("solar_input_kwh", normalized.solar_input),
            ("usage_kwh", normalized.usage),
        ):
            state[key] = values.tolist()
        for key in (
            "battery_levels", "battery_charge", "battery_charge_grid", "battery_charge_pv",
            "battery_discharge", "battery_preserve", "comfort_on", "comfort_levels",
            "comfort_off_streaks", "comfort_is_on", "comfort_history",
            "comfort_lock_mode", "comfort_lock_remaining",
        ):
            state[key] = []
        return {"state": encode_state_blob(state), "entities": [], "suboptimal_reasons": [],
                "execution_time": 0.1, "successful_solves": count if fresh is None else fresh,
                "reused_steps": 0 if fresh is None else count - fresh}

    monkeypatch.setattr(planner.core, "optimize_internal", run)
    return calls


def test_clock_cadence_and_duplicate_refresh_do_not_advance_phase(stub_core):
    state = None
    modes = []
    phases = []
    for tick in (0, 1, 1, 2, 3, 4):
        result = planner.optimize(OptimizationParams(**payload(tick, state)))
        state = result["state"]
        modes.append(result["cadence"]["mode"])
        phases.append(result["cadence"]["phase"])
    assert modes == ["full", "repair", "repair", "repair", "repair", "full"]
    assert phases == [0, 1, 1, 2, 3, 0]
    assert [c.get("policy_tail_start") for c in stub_core] == [None, 8, 8, 8, 8, None]


@pytest.mark.parametrize("tick", [-1, 0.5, 2])
def test_reversed_fractional_and_skipped_slots_force_full(stub_core, tick):
    first = planner.optimize(OptimizationParams(**payload()))
    result = planner.optimize(OptimizationParams(**payload(tick, first["state"])))
    assert result["cadence"]["mode"] == "fallback_full"
    assert result["cadence"]["reason"] == "nonconsecutive_window"
    assert stub_core[-1]["reuse_plan_override"] is None


def test_equivalent_timezone_represents_same_slot(stub_core):
    first = planner.optimize(OptimizationParams(**payload()))
    request = payload(state=first["state"])
    request["plan_start"] = "2026-09-11T02:00:00+02:00"
    result = planner.optimize(OptimizationParams(**request))
    assert result["cadence"]["phase"] == 0
    assert result["cadence"]["reason"] == "same_window_refresh"


def test_untimed_api_uses_legacy_path_and_never_assumes_a_tick(stub_core):
    request = payload()
    request.pop("plan_start")
    result = planner.optimize(OptimizationParams(**request))
    assert "cadence" not in result
    assert stub_core == [{}]
    timed = planner.optimize(OptimizationParams(**payload()))
    request["state"] = timed["state"]
    planner.optimize(OptimizationParams(**request))
    assert stub_core[-1] == {"reuse_plan_override": None}


def test_changed_horizon_or_resolution_falls_back(stub_core):
    first = planner.optimize(OptimizationParams(**payload()))
    changed = payload(1, first["state"], horizon=20)
    result = planner.optimize(OptimizationParams(**changed))
    assert result["cadence"]["reason"] == "configuration_changed"
    changed = payload(1, first["state"])
    changed["slot_minutes"] = 30
    result = planner.optimize(OptimizationParams(**changed))
    assert result["cadence"]["reason"] == "slot_duration_changed"


def test_updated_forecasts_and_advisory_windows_do_not_invalidate_cadence(stub_core):
    first = planner.optimize(OptimizationParams(**payload()))
    changed = payload(1, first["state"])
    changed["grid_import_price_per_kwh"][0] = -0.5
    changed["usage_kwh"][4] = 3
    changed["optional_entities"] = [{
        "name": "dryer", "duration_timeslots": 2, "start_before_timeslot": 16,
        "energy_kwh": 1, "options": 2,
    }]
    result = planner.optimize(OptimizationParams(**changed))
    assert result["cadence"]["mode"] == "repair"


@pytest.mark.parametrize("patch", [
    {"v": 1}, {"last_full_start": "bad"}, {"plan_start": "bad"},
    {"last_full_start": "2026-09-12T00:00:00+00:00"},
])
def test_untrusted_cadence_metadata_falls_back(stub_core, patch):
    first = planner.optimize(OptimizationParams(**payload()))
    state = planner._decode(first["state"])
    state["cadence_prefix"].update(patch)
    result = planner.optimize(OptimizationParams(**payload(1, encode_state_blob(state))))
    assert result["cadence"]["mode"] == "fallback_full"


def test_validation_rejects_naive_time_and_invalid_resolution():
    request = payload()
    request["plan_start"] = "2026-09-11T00:00:00"
    with pytest.raises(ValidationError, match="timezone"):
        OptimizationParams(**request)
    request = payload()
    request["slot_minutes"] = 0
    with pytest.raises(ValidationError, match="slot_minutes"):
        OptimizationParams(**request)


def test_clock_normalization_and_input_do_not_mutate(stub_core):
    request = payload()
    original = deepcopy(request)
    first = planner.optimize(OptimizationParams(**request))
    assert request == original
    assert "state" not in planner._decode(first["state"])


def test_public_api_runs_eight_steps_and_retains_full_forecast(monkeypatch):
    request = payload(horizon=196)
    first = optimize(OptimizationParams(**request))
    request = payload(1, first["state"], horizon=196)
    horizons = []
    original = planner.core._solve_mpc_step

    def count(*args, **kwargs):
        horizons.append(len(kwargs["prices_h"]))
        return original(*args, **kwargs)

    monkeypatch.setattr(planner.core, "_solve_mpc_step", count)
    result = optimize(OptimizationParams(**request))
    assert result["cadence"]["mode"] == "repair"
    assert result["successful_solves"] == 8
    assert horizons == [48] * 8
    assert len(result["projections"]["per_slot"]) == 196
    assert result["cadence"]["new_tail_steps"] == 1
    for tick, expected_solves in ((2, 8), (3, 8), (4, 196)):
        horizons.clear()
        result = optimize(OptimizationParams(**payload(tick, result["state"], horizon=196)))
        assert len(horizons) == result["successful_solves"] == expected_solves
        assert result["cadence"]["mode"] == ("full" if tick == 4 else "repair")
        assert result["cadence"]["phase"] == (0 if tick == 4 else tick)


def test_prefix_refresh_skips_mip_start_completion_overhead(monkeypatch):
    request = payload(horizon=16)
    request["infer_battery_preserve_policy"] = False
    request["action_deadband_kwh"] = 0.05
    request["battery_entities"] = [{
        "name": "battery", "initial_kwh": 1.0, "minimum_kwh": 0.0,
        "capacity_kwh": 2.0, "charge_curve_kwh": [0.5],
        "discharge_curve_kwh": [0.5], "can_charge_from": 3,
    }]
    result = optimize(OptimizationParams(**request))
    original = planner.core._solve_lp
    starts = []

    def capture(*args, **kwargs):
        starts.append(kwargs.get("mip_start"))
        return original(*args, **kwargs)

    monkeypatch.setattr(planner.core, "_solve_lp", capture)
    for tick in range(1, 5):
        request = payload(tick, result["state"], horizon=16)
        request["infer_battery_preserve_policy"] = False
        request["action_deadband_kwh"] = 0.05
        request["battery_entities"] = [{
            "name": "battery",
            "initial_kwh": result["entities"][0]["schedule"][0]["level"],
            "minimum_kwh": 0.0,
            "capacity_kwh": 2.0,
            "charge_curve_kwh": [0.5],
            "discharge_curve_kwh": [0.5],
            "can_charge_from": 3,
        }]
        starts.clear()
        result = optimize(OptimizationParams(**request))
        if tick < 4:
            assert result["cadence"]["mode"] == "repair"
            assert not any(start is not None for start in starts)
        else:
            assert result["cadence"]["mode"] == "full"
            assert any(start is not None for start in starts)


def test_issued_comfort_lock_is_not_advanced_by_duplicate_call(stub_core):
    request = payload(1)
    request["comfort_entities"] = [{
        "name": "heater", "target_on_slots_per_rolling_window": 1,
        "min_consecutive_on_slots": 3, "min_consecutive_off_slots": 2,
        "max_consecutive_off_slots": 4, "power_usage_kwh": 0.2,
        "is_on_now": True, "off_streak_slots_now": 0,
    }]
    receipt = {
        "v": planner.STATE_VERSION, "plan_start": START.isoformat(),
        "comfort_locks": {"heater": {
            "mode": True, "until": (START + timedelta(minutes=45)).isoformat(),
            "min_on_minutes": 45, "min_off_minutes": 30,
        }},
    }
    params = OptimizationParams(**request)
    for _ in range(2):
        locks = planner._initial_locks(params, receipt)
        assert locks["initial_comfort_lock_remaining"].tolist() == [2]
    request["plan_start"] = START + timedelta(minutes=30)
    assert planner._initial_locks(OptimizationParams(**request), receipt)[
        "initial_comfort_lock_remaining"
    ].tolist() == [1]
    request["comfort_entities"][0]["is_on_now"] = False
    changed = planner._initial_locks(OptimizationParams(**request), receipt)
    # The old ON commitment is discarded; the newly observed OFF state instead
    # has its own minimum-off requirement (off_streak_slots_now is zero).
    assert changed["initial_comfort_lock_mode"].tolist() == [0]
    assert changed["initial_comfort_lock_remaining"].tolist() == [2]


def test_absolute_target_deadline_signature_does_not_move_with_window():
    request = payload()
    request["battery_entities"] = [{
        "name": "battery", "initial_kwh": 1, "minimum_kwh": 0, "capacity_kwh": 2,
        "charge_curve_kwh": [0.5], "discharge_curve_kwh": [0.5],
        "target": {"timeslot": 12, "soc_kwh": 1},
    }]
    original = planner._signature(OptimizationParams(**request))
    request["plan_start"] += timedelta(minutes=15)
    request["battery_entities"][0]["target"]["timeslot"] -= 1
    request["battery_entities"][0]["initial_kwh"] = 0.5
    assert planner._signature(OptimizationParams(**request)) == original
    request["battery_entities"][0]["target"]["soc_kwh"] = 1.5
    assert planner._signature(OptimizationParams(**request)) != original


def test_infeasible_cached_target_triggers_full_replan():
    request = payload()
    request["grid_import_price_per_kwh"] = [1.0] * 10 + [0.1] * 3 + [1.0] * 3
    request["infer_battery_preserve_policy"] = False
    request["battery_entities"] = [{
        "name": "battery", "initial_kwh": 0.4, "minimum_kwh": 0.0,
        "capacity_kwh": 2.0, "charge_curve_kwh": [0.8],
        "discharge_curve_kwh": [0.8], "can_charge_from": 1,
        "target": {"timeslot": 12, "soc_kwh": 1.8},
    }]
    first = optimize(OptimizationParams(**request))
    state = planner._decode(first["state"])
    # A physically executable stale policy that cannot meet the deadline.
    state["battery_policy_states"] = [["self_consume"] * 16]
    request["state"] = encode_state_blob(state)
    request["plan_start"] += timedelta(minutes=15)
    request["grid_import_price_per_kwh"] = request["grid_import_price_per_kwh"][1:] + [1.0]
    request["battery_entities"][0]["initial_kwh"] = first["entities"][0]["schedule"][0]["level"]
    request["battery_entities"][0]["target"]["timeslot"] = 11
    result = optimize(OptimizationParams(**request))
    assert result["cadence"]["mode"] == "fallback_full"
    assert result["cadence"]["reason"] == "battery_target_unmet"
    assert result["successful_solves"] == 16
    assert result["cadence"]["retained_fresh_prefix_steps"] == 8
    assert result["reused_steps"] == 0
    assert result["entities"][0]["schedule"][11]["level"] >= 1.8 - 1e-6
    assert all(p["freshness"] == "optimized" for p in result["entities"][0]["schedule"])


def test_newly_issued_lock_survives_duplicate_and_skipped_refresh():
    request = payload()
    request["rolling_window_slots"] = 8
    request["lookahead_slots"] = 8
    request["comfort_entities"] = [{
        "name": "heater", "target_on_slots_per_rolling_window": 1,
        "min_consecutive_on_slots": 3, "min_consecutive_off_slots": 2,
        "max_consecutive_off_slots": 4, "power_usage_kwh": 0.2,
        "is_on_now": False, "off_streak_slots_now": 4,
        "on_history": [False] * 7,
    }]
    first = optimize(OptimizationParams(**request))
    assert first["entities"][0]["schedule"][0]["enabled"]
    until = (START + timedelta(minutes=45)).isoformat()
    assert planner._decode(first["state"])["cadence_prefix"]["comfort_locks"]["heater"]["until"] == until
    request["state"] = first["state"]
    request["comfort_entities"][0]["is_on_now"] = True
    request["comfort_entities"][0]["off_streak_slots_now"] = 0
    repeated = optimize(OptimizationParams(**request))
    assert repeated["entities"][0]["schedule"][0]["enabled"]
    assert planner._decode(repeated["state"])["cadence_prefix"]["comfort_locks"]["heater"]["until"] == until
    request["plan_start"] += timedelta(minutes=30)
    request["state"] = repeated["state"]
    request["comfort_entities"][0]["on_history"] = [False] * 5 + [True, True]
    skipped = optimize(OptimizationParams(**request))
    assert skipped["cadence"]["reason"] == "nonconsecutive_window"
    assert skipped["entities"][0]["schedule"][0]["enabled"]
    assert planner._decode(skipped["state"])["cadence_prefix"]["comfort_locks"]["heater"]["until"] == until


def test_prefix_refresh_keeps_deterministic_comfort_feasible_without_fallback(monkeypatch):
    request = payload()
    request["rolling_window_slots"] = 8
    request["lookahead_slots"] = 8
    request["comfort_entities"] = [{
        "name": "heater", "target_on_slots_per_rolling_window": 1,
        "min_consecutive_on_slots": 3, "min_consecutive_off_slots": 2,
        "max_consecutive_off_slots": 4, "power_usage_kwh": 0.2,
        "is_on_now": False, "off_streak_slots_now": 4,
        "on_history": [False] * 7,
    }]
    first = optimize(OptimizationParams(**request))
    request["state"] = first["state"]
    request["plan_start"] += timedelta(minutes=15)
    request["comfort_entities"][0].update(
        is_on_now=True, off_streak_slots_now=0, on_history=[False] * 6 + [True]
    )
    calls = 0
    original = planner.core._solve_mpc_step

    def count(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(planner.core, "_solve_mpc_step", count)
    result = optimize(OptimizationParams(**request))
    assert result["cadence"]["mode"] == "repair"
    assert calls == result["successful_solves"] == 8
    assert not result["suboptimal"]
    schedule = [p["enabled"] for p in result["entities"][0]["schedule"]]
    combined = request["comfort_entities"][0]["on_history"] + schedule
    assert all(sum(combined[i:i + 8]) >= 1 for i in range(len(combined) - 7))
    for i in range(1, len(schedule)):
        if schedule[i] and not schedule[i - 1]:
            assert schedule[i:i + 3] == [True] * min(3, len(schedule) - i)


def test_observed_history_recovers_an_ongoing_lock_without_a_receipt():
    request = payload()
    request["rolling_window_slots"] = 8
    request["comfort_entities"] = [{
        "name": "heater", "target_on_slots_per_rolling_window": 1,
        "min_consecutive_on_slots": 3, "min_consecutive_off_slots": 2,
        "max_consecutive_off_slots": 4, "power_usage_kwh": 0.2,
        "is_on_now": True, "off_streak_slots_now": 0,
        "on_history": [False] * 5 + [True, True],
    }]
    locks = planner._initial_locks(OptimizationParams(**request), None)
    assert locks["initial_comfort_lock_remaining"].tolist() == [1]
    result = optimize(OptimizationParams(**request))
    assert result["entities"][0]["schedule"][0]["enabled"]


def test_short_uniform_history_does_not_restart_an_unknown_long_lock():
    request = payload()
    request["rolling_window_slots"] = 2
    request["comfort_entities"] = [{
        "name": "heater", "target_on_slots_per_rolling_window": 1,
        "min_consecutive_on_slots": 3, "min_consecutive_off_slots": 2,
        "max_consecutive_off_slots": 4, "power_usage_kwh": 0.2,
        "is_on_now": True, "off_streak_slots_now": 0, "on_history": [True],
    }]
    assert planner._initial_locks(OptimizationParams(**request), None)[
        "initial_comfort_lock_remaining"
    ].tolist() == [0]


def test_forecast_datetime_range_is_validated():
    request = payload()
    request["plan_start"] = datetime(9999, 12, 31, 23, 59, tzinfo=UTC)
    with pytest.raises(ValidationError, match="datetime range"):
        OptimizationParams(**request)
