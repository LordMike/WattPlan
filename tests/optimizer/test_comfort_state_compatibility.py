"""Saved battery controls must match the comfort demand they were solved for."""

from datetime import UTC, datetime, timedelta
import json
from unittest.mock import patch

from custom_components.wattplan.optimizer import OptimizationParams, optimize
from custom_components.wattplan.optimizer import models, prefix_planner


def _payload(horizon=16):
    return {
        "grid_import_price_per_kwh": [0.2] * horizon,
        "grid_export_price_per_kwh": [0.0] * horizon,
        "solar_input_kwh": [0.0] * horizon,
        "usage_kwh": [0.2] * horizon,
        "lookahead_slots": 8,
        "rolling_window_slots": 8,
        "battery_entities": [],
        "comfort_entities": [{
            "name": "heat", "power_usage_kwh": 0.2,
            "target_on_slots_per_rolling_window": 1,
            "min_consecutive_on_slots": 3, "min_consecutive_off_slots": 2,
            "max_consecutive_off_slots": 4,
            "on_history": [False] * 7, "is_on_now": False,
            "off_streak_slots_now": 4,
        }],
    }


def _without_scheduler_version(calculate):
    original = json.dumps

    def legacy_payload(payload, *args, **kwargs):
        if isinstance(payload, dict):
            payload = dict(payload)
            payload.pop("comfort_scheduler_version", None)
        return original(payload, *args, **kwargs)

    with patch.object(json, "dumps", legacy_payload):
        return calculate()


def test_legacy_comfort_solver_state_does_not_reuse_old_battery_controls():
    request = _payload()
    params = OptimizationParams(**request)
    first = optimize(params)
    request["state"] = first["state"]
    matching = optimize(OptimizationParams(**request))
    assert matching["successful_solves"] == 0

    state = prefix_planner._decode(first["state"])
    legacy = _without_scheduler_version(
        lambda: models.normalize_calculation_input(params).fingerprint
    )
    assert state["entity_fingerprint"] != legacy
    state["entity_fingerprint"] = legacy
    request["state"] = models.encode_state_blob(state)
    result = optimize(OptimizationParams(**request))
    assert result["successful_solves"] == 16
    assert result["reused_steps"] == 0


def test_comfort_scheduler_upgrade_forces_full_but_preserves_active_lock():
    request = _payload()
    request["plan_start"] = datetime(2026, 9, 12, tzinfo=UTC)
    first = optimize(OptimizationParams(**request))
    state = prefix_planner._decode(first["state"])
    legacy = _without_scheduler_version(
        lambda: prefix_planner._signature(OptimizationParams(**request))
    )
    assert state["cadence_prefix"]["config_signature"] != legacy
    state["cadence_prefix"]["config_signature"] = legacy
    request["state"] = models.encode_state_blob(state)
    request["plan_start"] += timedelta(minutes=15)
    request["comfort_entities"][0].update(
        is_on_now=True, off_streak_slots_now=0, on_history=[False] * 6 + [True]
    )
    result = optimize(OptimizationParams(**request))
    assert result["cadence"]["mode"] == "fallback_full"
    assert result["cadence"]["reason"] == "configuration_changed"
    assert result["successful_solves"] == 16
    assert all(p["enabled"] for p in result["entities"][0]["schedule"][:2])


def test_changed_terminal_comfort_demand_invalidates_legacy_control_reuse():
    first = optimize(OptimizationParams(**_payload(16)))
    request = _payload(13)
    fresh = optimize(OptimizationParams(**request))
    old_on = [p["enabled"] for p in first["entities"][0]["schedule"][:13]]
    new_on = [p["enabled"] for p in fresh["entities"][0]["schedule"]]
    assert old_on != new_on
    request["state"] = first["state"]
    result = optimize(OptimizationParams(**request))
    assert result["successful_solves"] == 13
    assert result["reused_steps"] == 0
    assert result["entities"] == fresh["entities"]
    assert result["projections"] == fresh["projections"]


def test_unavoidable_comfort_deficit_is_reported_without_repeated_full_solves():
    request = _payload(32)
    request.update(plan_start=datetime(2026, 9, 12, tzinfo=UTC), rolling_window_slots=24)
    comfort = request["comfort_entities"][0]
    comfort.pop("on_history")
    comfort.update(target_on_slots_per_rolling_window=18, on_slots_last_rolling_window=1)
    first = optimize(OptimizationParams(**request))
    request.update(state=first["state"], plan_start=request["plan_start"] + timedelta(minutes=15))
    comfort.update(is_on_now=True, off_streak_slots_now=0, on_slots_last_rolling_window=2)
    result = optimize(OptimizationParams(**request))
    assert result["cadence"]["mode"] == "repair"
    assert result["successful_solves"] == 8
    assert result["suboptimal"]
    assert "comfort_target_unmet" in result["suboptimal_reasons"]
    assert "comfort_history_unavailable" in result["suboptimal_reasons"]
    assert all(point["freshness"] == "scheduled" for point in result["entities"][0]["schedule"])
