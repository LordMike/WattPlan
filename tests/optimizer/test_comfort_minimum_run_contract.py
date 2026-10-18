"""Live timed comfort commitments hold across every selectable preset."""

from datetime import UTC, datetime

import pytest

from custom_components.wattplan.optimizer.models import OptimizationParams
from custom_components.wattplan.optimizer.planners.presets import PRESETS, run_preset
from custom_components.wattplan.optimizer.planners.registry import BUILTINS, create_planner


@pytest.mark.parametrize(
    "kind,name",
    [("preset", name) for name in sorted(PRESETS)]
    + [("algorithm", name) for name in sorted(BUILTINS)],
)
def test_all_planners_preserve_observed_runtime_minimums(kind, name):
    """A recent ON/OFF transition remains committed on the next forecast."""
    params = OptimizationParams(
        plan_start=datetime(2026, 10, 13, tzinfo=UTC),
        slot_minutes=15,
        grid_import_price_per_kwh=[100.0, 0.0, 1.0, 0.0, 100.0, 0.0, 1.0, 0.0],
        grid_export_price_per_kwh=[0.0] * 8,
        solar_input_kwh=[0.0] * 8,
        usage_kwh=[0.0] * 8,
        battery_entities=[],
        comfort_entities=[
            {
                "name": "recently_started",
                "target_on_slots_per_rolling_window": 1,
                "min_consecutive_on_slots": 3,
                "min_consecutive_off_slots": 1,
                "max_consecutive_off_slots": 4,
                "power_usage_kwh": 1.0,
                "is_on_now": True,
                "on_history": [True, True, False],
                "off_streak_slots_now": 0,
            },
            {
                "name": "recently_stopped",
                "target_on_slots_per_rolling_window": 1,
                "min_consecutive_on_slots": 1,
                "min_consecutive_off_slots": 3,
                "max_consecutive_off_slots": 4,
                "power_usage_kwh": 1.0,
                "is_on_now": False,
                "on_history": [True, True, True],
                "off_streak_slots_now": 0,
            },
        ],
        rolling_window_slots=4,
        lookahead_slots=8,
    )

    result = (
        run_preset(params, name) if kind == "preset" else create_planner(name)(params)
    )
    comfort = {
        entity["name"]: entity["schedule"]
        for entity in result["entities"]
        if entity["type"] == "comfort"
    }

    assert [point["enabled"] for point in comfort["recently_started"][:3]] == [
        True,
        True,
        True,
    ], name
    assert [point["enabled"] for point in comfort["recently_stopped"][:3]] == [
        False,
        False,
        False,
    ], name


def test_current_on_with_unknown_history_keeps_legacy_lock_behavior():
    """Missing ordered history cannot safely infer how long ON has run."""
    params = OptimizationParams(
        plan_start=datetime(2026, 10, 13, tzinfo=UTC),
        grid_import_price_per_kwh=[1.0] * 8,
        solar_input_kwh=[0.0] * 8,
        usage_kwh=[0.0] * 8,
        battery_entities=[],
        comfort_entities=[
            {
                "name": "heater",
                "target_on_slots_per_rolling_window": 1,
                "min_consecutive_on_slots": 3,
                "min_consecutive_off_slots": 1,
                "max_consecutive_off_slots": 4,
                "power_usage_kwh": 1.0,
                "is_on_now": True,
                "on_history": None,
                "off_streak_slots_now": 0,
            },
        ],
        rolling_window_slots=4,
    )

    schedule = create_planner("greedy")(params)["entities"][0]["schedule"]
    # No lock is inferred, so the heater may stop right after the current slot.
    assert [point["enabled"] for point in schedule[:2]] == [True, False]
