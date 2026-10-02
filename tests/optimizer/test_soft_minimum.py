"""A battery below its minimum SoC yields a recovering plan, not a failure."""

from datetime import UTC, datetime

import pytest

from custom_components.wattplan.optimizer import OptimizationParams, optimize


def _params(*, initial_kwh: float, solar: list[float]) -> OptimizationParams:
    horizon = len(solar)
    return OptimizationParams(
        plan_start=datetime(2026, 9, 23, tzinfo=UTC),
        slot_minutes=60,
        grid_import_price_per_kwh=[0.3] * horizon,
        grid_export_price_per_kwh=[0.05] * horizon,
        solar_input_kwh=solar,
        usage_kwh=[0.2] * horizon,
        lookahead_slots=horizon,
        battery_entities=[
            {
                "name": "house",
                "initial_kwh": initial_kwh,
                "minimum_kwh": 2.0,
                "capacity_kwh": 10.0,
                "charge_curve_kwh": [2.0],
                "discharge_curve_kwh": [2.0],
                "can_charge_from": 2,
            }
        ],
        comfort_entities=[],
    )


def test_battery_below_minimum_at_night_plans_and_reports_unmet() -> None:
    result = optimize(_params(initial_kwh=1.0, solar=[0.0, 0.0, 0.0, 3.0, 3.0, 0.0]))

    schedule = result["entities"][0]["schedule"]
    levels = [slot["level"] for slot in schedule]
    assert "battery_min_unmet" in result["suboptimal_reasons"]
    # No energy is drawn from the battery while it is below its reserve.
    assert levels[2] == pytest.approx(1.0, abs=1e-6)
    # Once PV is available the battery recovers as fast as the charge limit allows.
    assert levels[3] > levels[2]
    assert levels[4] >= 2.0 - 1e-6


def test_battery_at_minimum_is_not_reported_unmet() -> None:
    result = optimize(_params(initial_kwh=2.0, solar=[0.0, 0.0, 0.0, 3.0, 3.0, 0.0]))

    assert "battery_min_unmet" not in result["suboptimal_reasons"]
    assert all(
        slot["level"] >= 2.0 - 1e-6 for slot in result["entities"][0]["schedule"]
    )
