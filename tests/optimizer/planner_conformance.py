"""Reusable battery planner checks; each implementation invokes these itself."""

import json

import pytest

from custom_components.wattplan.optimizer.models import OptimizationParams
from custom_components.wattplan.optimizer.planners import (
    create_planner,
    validate_battery_result,
)
from custom_components.wattplan.optimizer.planners.evaluation import evaluate_schedule


def conformance_params() -> list[OptimizationParams]:
    battery = {
        "name": "house",
        "initial_kwh": 1.0,
        "minimum_kwh": 0.2,
        "capacity_kwh": 3.0,
        "charge_curve_kwh": [1.0],
        "discharge_curve_kwh": [1.0],
        "can_charge_from": 3,
        "charge_efficiency": 0.9,
        "discharge_efficiency": 0.8,
    }
    common = {
        "plan_start": "2026-06-15T00:00:00+00:00",
        "slot_minutes": 15,
        "grid_import_price_per_kwh": [-0.2, 0.1, 0.8, 0.4],
        "grid_export_price_per_kwh": [0.0, -0.1, 0.2, 0.1],
        "usage_kwh": [0.3, 0.3, 1.0, 0.4],
        "solar_input_kwh": [0.0, 1.5, 0.0, 0.2],
        "comfort_entities": [],
    }
    return [
        OptimizationParams(**common, battery_entities=[]),
        OptimizationParams(**common, battery_entities=[battery]),
        OptimizationParams(
            **common,
            battery_entities=[
                {
                    **battery,
                    "initial_kwh": 0.1,
                    "target": {"timeslot": 2, "soc_kwh": 2.0},
                },
                {**battery, "name": "pv-only", "can_charge_from": 2},
            ],
        ),
    ]


def assert_conforms(factory: str, params: OptimizationParams, options=None) -> dict:
    """Check complete serializable output and physically executable projections.

    Targets may be infeasible; this helper does not demand a particular policy or
    impose the experimental objective on HiGHS. It compares physics for emitted
    policies independently and verifies input isolation in callers' test fixtures.
    """
    before = params.model_dump(mode="json")
    result = create_planner(factory, options)(params)
    assert params.model_dump(mode="json") == before, "Planner mutated its request"
    json.dumps(result, allow_nan=False)
    schedules = validate_battery_result(result, params)
    policies = [
        [a["state"] for a in schedules[b.name]] for b in params.battery_entities
    ]
    evaluation = evaluate_schedule(params, policies)
    for step in evaluation.steps:
        assert step.grid_import >= 0
        assert step.grid_export >= 0
        for battery, level in zip(params.battery_entities, step.levels, strict=True):
            assert 0 <= level <= battery.capacity_kwh + 1e-6
    # Candidate build_result uses policy projections. HiGHS retains its original
    # continuous-control projections, so only require this from the shared builder.
    if "evaluation" in result.get("diagnostics", {}):
        assert result["projections"]["projected_cost"] == pytest.approx(
            evaluation.tariff_cost
        )
    return result
