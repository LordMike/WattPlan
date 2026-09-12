import base64
import json

import numpy as np
import pytest

pytest.importorskip("highspy")

from custom_components.wattplan.optimizer import mpc_power_optimizer as optimizer


def _payload(*, usage=None, batteries=None, comfort=None, deadband=0.0):
    steps = 12
    return {
        "grid_import_price_per_kwh": [0.2] * steps,
        "grid_export_price_per_kwh": [0.0] * steps,
        "solar_input_kwh": [0.0] * steps,
        "usage_kwh": usage or [0.0] * steps,
        "lookahead_slots": 12,
        "rolling_window_slots": 4,
        "infer_battery_preserve_policy": False,
        "action_deadband_kwh": deadband,
        "battery_entities": batteries or [],
        "comfort_entities": comfort or [],
    }


def _normalized(payload):
    return optimizer.normalize_calculation_input(optimizer.OptimizationParams(**payload))


def _reuse_plan(normalized, *, comfort_on=None, reused_tail=0):
    num_comfort = len(normalized.comfort_entities)
    return {
        "overlap_steps": 0,
        "initial_comfort_lock_mode": np.zeros(num_comfort, dtype=np.float64),
        "initial_comfort_lock_remaining": np.zeros(num_comfort, dtype=np.float64),
        "comfort_on": (
            np.asarray(comfort_on, dtype=np.float64)
            if comfort_on is not None
            else np.zeros((num_comfort, normalized.total_steps), dtype=np.float64)
        ),
        "policy_reused_tail_steps": reused_tail,
    }


def _battery(name, *, initial=1.0, capacity=1.0, sources=1):
    return {
        "name": name,
        "initial_kwh": initial,
        "minimum_kwh": 0.0,
        "capacity_kwh": capacity,
        "charge_curve_kwh": [1.0],
        "discharge_curve_kwh": [1.0],
        "can_charge_from": sources,
    }


def test_policy_tail_solves_exact_prefix_and_reacts_to_current_load(monkeypatch):
    usage = [0.0] * 12
    usage[8] = 0.25
    normalized = _normalized(_payload(usage=usage, batteries=[_battery("battery")]))
    reuse_plan = _reuse_plan(normalized, reused_tail=3)
    policies = [[None] * 8 + ["self_consume"] * 4]
    calls = 0
    original = optimizer._solve_mpc_step

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(optimizer, "_solve_mpc_step", counted)
    result = optimizer.optimize_internal(
        normalized,
        reuse_plan_override=reuse_plan,
        policy_tail_start=8,
        battery_policy_override=policies,
    )

    schedule = result["entities"][0]["schedule"]
    assert calls == result["successful_solves"] == 8
    assert result["reused_steps"] == 3
    assert schedule[8]["state"] == "self_consume"
    assert schedule[8]["level"] == pytest.approx(schedule[7]["level"] - 0.25)


def test_zero_flow_tail_keeps_published_grid_charge_and_preserve_labels():
    normalized = _normalized(
        _payload(
            batteries=[
                _battery("grid", initial=1.0, capacity=1.0),
                _battery("reserve", initial=1.0, capacity=1.0),
            ]
        )
    )
    policies = [
        [None] * 8 + ["grid_charge"] * 4,
        [None] * 8 + ["preserve"] * 4,
    ]

    result = optimizer.optimize_internal(
        normalized,
        reuse_plan_override=_reuse_plan(normalized),
        policy_tail_start=8,
        battery_policy_override=policies,
    )

    grid, reserve = [entity["schedule"] for entity in result["entities"]]
    assert [point["state"] for point in grid[8:]] == ["grid_charge"] * 4
    assert [point["state"] for point in reserve[8:]] == ["preserve"] * 4
    assert [point["level"] for point in grid[8:]] == pytest.approx([1.0] * 4)
    assert [point["level"] for point in reserve[8:]] == pytest.approx([1.0] * 4)


def test_tail_policy_honors_sources_and_does_not_cross_charge_batteries():
    usage = [0.0] * 12
    usage[8] = 0.4
    payload = _payload(
        usage=usage,
        batteries=[
            _battery("self", initial=1.0, capacity=1.0, sources=0),
            _battery("grid", initial=0.0, capacity=1.0, sources=1),
        ],
    )
    normalized = _normalized(payload)
    policies = [
        [None] * 8 + ["self_consume"] * 4,
        [None] * 8 + ["grid_charge"] * 4,
    ]

    result = optimizer.optimize_internal(
        normalized,
        reuse_plan_override=_reuse_plan(normalized),
        policy_tail_start=8,
        battery_policy_override=policies,
    )

    self_schedule, grid_schedule = [
        entity["schedule"] for entity in result["entities"]
    ]
    assert self_schedule[8]["level"] == pytest.approx(0.6)
    assert grid_schedule[8]["level"] == pytest.approx(1.0)
    assert result["projections"]["per_slot"][8]["projected_cost"] == pytest.approx(
        0.2
    )


def test_tail_policy_allocates_pv_stably_and_self_consumption_proportionally():
    usage = [0.0] * 12
    usage[8] = 0.5
    solar = [0.0] * 12
    solar[9] = 1.5
    payload = _payload(
        usage=usage,
        batteries=[
            _battery("first", initial=1.0, capacity=1.0, sources=2),
            _battery("second", initial=1.0, capacity=1.0, sources=2),
            _battery("no_pv", initial=0.0, capacity=1.0, sources=0),
        ],
    )
    payload["solar_input_kwh"] = solar
    normalized = _normalized(payload)
    policies = [[None] * 8 + ["self_consume"] * 4 for _ in range(3)]

    result = optimizer.optimize_internal(
        normalized,
        reuse_plan_override=_reuse_plan(normalized),
        policy_tail_start=8,
        battery_policy_override=policies,
    )

    first, second, no_pv = [entity["schedule"] for entity in result["entities"]]
    assert first[8]["level"] == pytest.approx(0.75)
    assert second[8]["level"] == pytest.approx(0.75)
    assert first[9]["level"] == pytest.approx(1.0)
    assert second[9]["level"] == pytest.approx(1.0)
    assert no_pv[9]["level"] == pytest.approx(0.0)


def test_tail_uses_deterministic_comfort_instead_of_stale_replay(monkeypatch):
    comfort = [
        {
            "name": "heatpump",
            "target_on_slots_per_rolling_window": 1,
            "min_consecutive_on_slots": 3,
            "min_consecutive_off_slots": 1,
            "max_consecutive_off_slots": 12,
            "power_usage_kwh": 0.1,
            "is_on_now": False,
            "on_slots_last_rolling_window": 0,
            "on_history": [False, False, False],
            "off_streak_slots_now": 1,
        }
    ]
    normalized = _normalized(_payload(comfort=comfort))
    comfort_tail = np.zeros((1, 12), dtype=np.float64)

    def fixed_solve(**kwargs):
        command = 1.0 if kwargs["base_timeslot"] == 7 else 0.0
        return {
            "charge": np.zeros(0),
            "charge_grid": np.zeros(0),
            "charge_pv": np.zeros(0),
            "discharge": np.zeros(0),
            "comfort_on": np.asarray([command]),
            "objective_value": 0.0,
        }

    monkeypatch.setattr(optimizer, "_solve_mpc_step", fixed_solve)
    result = optimizer.optimize_internal(
        normalized,
        reuse_plan_override=_reuse_plan(normalized, comfort_on=comfort_tail),
        policy_tail_start=8,
        battery_policy_override=[],
    )

    schedule = result["entities"][0]["schedule"]
    assert [point["enabled"] for point in schedule[6:9]] == [True, True, True]
    state = json.loads(base64.urlsafe_b64decode(result["state"]).decode("utf-8"))
    assert state["comfort_history"][0][9] == [1, 1, 1]


def test_self_consume_policy_replays_below_command_deadband():
    usage = [0.0] * 12
    usage[8] = 0.1
    normalized = _normalized(
        _payload(
            usage=usage,
                batteries=[_battery("battery")],
                deadband=0.25,
        )
    )

    result = optimizer.optimize_internal(
        normalized,
        reuse_plan_override=_reuse_plan(normalized),
        policy_tail_start=8,
        battery_policy_override=[[None] * 8 + ["self_consume"] * 4],
    )

    schedule = result["entities"][0]["schedule"]
    assert schedule[8]["level"] == pytest.approx(schedule[7]["level"] - 0.1)
