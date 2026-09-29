"""Diagnostic records must not interfere with successful plans."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

from custom_components.wattplan.coordinator_logic.projection import (
    PlannerProjectionBuilder,
)
from custom_components.wattplan.optimizer import OptimizationParams, optimize
from custom_components.wattplan.optimizer.reproduction_codec import decode_reproduction


def _plan() -> tuple[dict, dict]:
    start = datetime(2026, 9, 29, tzinfo=UTC)
    params = OptimizationParams(
        plan_start=start,
        slot_minutes=15,
        grid_import_price_per_kwh=[1.123456789, 2.0, 3.0, 4.0],
        grid_export_price_per_kwh=[0.1, 0.2, 0.3, 0.4],
        usage_kwh=[0.123456789, 0.2, 0.3, 0.4],
        solar_input_kwh=[0.0, 0.1, 0.2, 0.3],
        battery_entities=[],
        comfort_entities=[],
    )
    request = {
        "window": SimpleNamespace(start_at=start, slot_minutes=15, slots=4),
        "slot_minutes": 15,
        "optimizer_params": params.model_dump(mode="json"),
        "_reproduction_optimizer_params": params.model_dump(mode="json"),
        "name_to_subentry": {"batteries": {}, "comforts": {}, "optionals": {}},
    }
    return request, optimize(params)


def test_reproduction_is_lossless_and_only_built_when_enabled() -> None:
    request, result = _plan()
    builder = PlannerProjectionBuilder(None, entry_id="test", integration_version="test")
    with (
        patch.object(builder, "_plan_details_enabled", return_value=False),
        patch(
            "custom_components.wattplan.coordinator_logic.projection.build_plan_outlook",
            return_value={},
        ),
    ):
        disabled = builder.planner_output_from_result(request, result, timings=[])
    assert "planner_reproduction" not in disabled["diagnostics"]

    with (
        patch.object(
            builder,
            "_plan_details_enabled",
            side_effect=lambda name: name == "planner_reproduction",
        ),
        patch(
            "custom_components.wattplan.coordinator_logic.projection.build_plan_outlook",
            return_value={},
        ),
    ):
        enabled = builder.planner_output_from_result(request, result, timings=[])
    archived = decode_reproduction(enabled["diagnostics"]["planner_reproduction"]["payload"])
    assert archived["request"]["optimizer_params"] == request["_reproduction_optimizer_params"]
    assert archived["result"] == result
    assert archived["request"]["optimizer_params"]["usage_kwh"][0] == 0.123456789
    assert archived["request"]["window"]["end_at"] == "2026-09-29T01:00:00+00:00"


def test_oversized_or_failed_reproduction_does_not_invalidate_plan() -> None:
    request, result = _plan()
    builder = PlannerProjectionBuilder(None, entry_id="test")
    for side_effect in (None, RuntimeError("encoder broke")):
        with (
            patch.object(
                builder,
                "_plan_details_enabled",
                side_effect=lambda name: name == "planner_reproduction",
            ),
            patch(
                "custom_components.wattplan.coordinator_logic.projection.encode_reproduction",
                return_value="x" * 16_000 if side_effect is None else None,
                side_effect=side_effect,
            ),
            patch(
                "custom_components.wattplan.coordinator_logic.projection.build_plan_outlook",
                return_value={},
            ),
        ):
            output = builder.planner_output_from_result(request, result, timings=[])
        assert output["status"] == "ok"
        assert "error" in output["diagnostics"]["planner_reproduction"]
