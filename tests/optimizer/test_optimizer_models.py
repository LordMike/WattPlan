"""Shared optimizer request validation, independent of planner selection."""

import pytest
from pydantic import ValidationError

from custom_components.wattplan.optimizer.models import OptimizationParams
from tests.optimizer.planner_conformance import economic_params


def payload(battery=None, **top):
    """Valid four-slot request as a dict, with top-level and battery overrides."""
    raw = economic_params(prices=(0.3, 0.2, 0.1, 0.2), usage=(1.0,) * 4).model_dump()
    raw["battery_entities"][0].update(initial_kwh=1.0, **(battery or {}))
    raw.update(top)
    return raw


def optional(**fields):
    return {
        "name": "wash", "duration_timeslots": 2, "start_after_timeslot": 0,
        "start_before_timeslot": 4, "energy_kwh": 1.0, "options": 1, **fields,
    }


@pytest.mark.parametrize("change", [
    {"plan_start": "2026-09-11T00:00:00"},
    {"slot_minutes": 0},
])
def test_forecast_clock_and_resolution_are_validated(change):
    with pytest.raises(ValidationError):
        OptimizationParams(**payload(**change))


def test_validation_rejects_series_length_mismatch():
    with pytest.raises(ValidationError):
        OptimizationParams(**payload(solar_input_kwh=[0.0] * 3))


def test_validation_rejects_too_short_horizon():
    short = [0.1] * 3
    with pytest.raises(ValidationError):
        OptimizationParams(**payload(
            grid_import_price_per_kwh=short, grid_export_price_per_kwh=[0.0] * 3,
            solar_input_kwh=[0.0] * 3, usage_kwh=[1.0] * 3,
        ))


def test_validation_rejects_comfort_min_on_not_less_than_solve_horizon():
    comfort = {
        "name": "hvac",
        "target_on_slots_per_rolling_window": 1,
        "min_consecutive_on_slots": 4,
        "max_consecutive_off_slots": 4,
        "power_usage_kwh": 0.5,
        "is_on_now": False,
        "on_slots_last_rolling_window": 0,
        "off_streak_slots_now": 0,
    }
    with pytest.raises(ValidationError):
        OptimizationParams(**payload(comfort_entities=[comfort]))


def test_validation_rejects_infeasible_optional_options():
    infeasible = optional(
        energy_kwh=2.0, options=3, min_option_gap_timeslots=2,
        allow_overlapping_options=False,
    )
    with pytest.raises(ValidationError):
        OptimizationParams(**payload(optional_entities=[infeasible]))


def test_validation_rejects_optional_curve_longer_than_duration():
    longer = optional(
        energy_kwh=[1.0, 2.0, 3.0], min_option_gap_timeslots=0,
        allow_overlapping_options=True,
    )
    with pytest.raises(ValidationError):
        OptimizationParams(**payload(optional_entities=[longer]))


def test_validation_normalizes_optional_energy_to_full_profile_list():
    params = OptimizationParams(**payload(
        battery={"charge_curve_kwh": [1], "discharge_curve_kwh": [1], "can_charge_from": 1},
        rolling_window_slots=24,
        optional_entities=[
            optional(name="one", duration_timeslots=4, energy_kwh=2.0),
            optional(name="two", duration_timeslots=4, energy_kwh=[1.0, 2.0]),
        ],
    ))

    assert params.optional_entities[0].energy_kwh == [0.5, 0.5, 0.5, 0.5]
    assert params.optional_entities[1].energy_kwh == [1.0, 1.0, 2.0, 2.0]
    assert params.battery_entities[0].charge_curve_kwh == [1.0]
    assert params.battery_entities[0].charge_efficiency == pytest.approx(1.0)
    assert params.battery_entities[0].discharge_efficiency == pytest.approx(1.0)


def test_validation_rejects_out_of_range_efficiency():
    with pytest.raises(ValidationError):
        OptimizationParams(**payload(battery={"charge_efficiency": 1.1}))


def test_validation_rejects_battery_target_slot_outside_horizon():
    target = {"timeslot": 10, "soc_kwh": 8.0, "mode": "at_least", "tolerance_kwh": 0.1}
    with pytest.raises(ValidationError):
        OptimizationParams(**payload(battery={"target": target}))


def test_validation_rejects_unknown_charge_ingress_bits():
    # Any bits outside GRID(1) and PV(2) must be rejected at validation time.
    with pytest.raises(ValidationError):
        OptimizationParams(**payload(battery={"can_charge_from": 4}))
