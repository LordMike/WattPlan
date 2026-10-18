"""Shared optimizer request validation, independent of planner selection."""

import pytest
from pydantic import ValidationError

from custom_components.wattplan.optimizer.models import OptimizationParams
from tests.optimizer.planner_conformance import economic_params


@pytest.mark.parametrize("change,message", [
    ({"plan_start": "2026-09-11T00:00:00"}, "timezone"),
    ({"slot_minutes": 0}, "slot_minutes"),
    ({"plan_start": "9999-12-31T23:59:00+00:00"}, "datetime range"),
])
def test_forecast_clock_and_resolution_are_validated(change, message):
    payload = economic_params().model_dump()
    with pytest.raises(ValidationError, match=message):
        OptimizationParams(**dict(payload, **change))


def test_validation_rejects_series_length_mismatch():
    payload = {
        "grid_import_price_per_kwh": [0.3, 0.2, 0.1, 0.2],
        "solar_input_kwh": [0.0, 0.0, 0.0],
        "usage_kwh": [1.0, 1.0, 1.0, 1.0],
        "battery_entities": [
            {
                "name": "b",
                "initial_kwh": 1.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 2.0,
                "charge_curve_kwh": [0.05],
                "discharge_curve_kwh": [0.05],
                "can_charge_from": 1,
            }
        ],
        "comfort_entities": [],
    }

    with pytest.raises(ValidationError):
        OptimizationParams(**payload)


def test_validation_rejects_too_short_horizon():
    payload = {
        "grid_import_price_per_kwh": [0.3, 0.2, 0.1],
        "solar_input_kwh": [0.0, 0.0, 0.0],
        "usage_kwh": [1.0, 1.0, 1.0],
        "battery_entities": [
            {
                "name": "b",
                "initial_kwh": 1.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 2.0,
                "charge_curve_kwh": [1.0],
                "discharge_curve_kwh": [1.0],
                "can_charge_from": 1,
            }
        ],
        "comfort_entities": [],
    }

    with pytest.raises(ValidationError):
        OptimizationParams(**payload)


def test_validation_rejects_comfort_min_on_not_less_than_solve_horizon():
    payload = {
        "grid_import_price_per_kwh": [0.3, 0.2, 0.1, 0.2],
        "solar_input_kwh": [0.0, 0.0, 0.0, 0.0],
        "usage_kwh": [1.0, 1.0, 1.0, 1.0],
        "battery_entities": [
            {
                "name": "b",
                "initial_kwh": 1.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 2.0,
                "charge_curve_kwh": [1.0],
                "discharge_curve_kwh": [1.0],
                "can_charge_from": 1,
            }
        ],
        "comfort_entities": [
            {
                "name": "hvac",
                "target_on_slots_per_rolling_window": 1,
                "min_consecutive_on_slots": 4,
                "max_consecutive_off_slots": 4,
                "power_usage_kwh": 0.5,
                "is_on_now": False,
                "on_slots_last_rolling_window": 0,
                "off_streak_slots_now": 0,
            }
        ],
    }

    with pytest.raises(ValidationError):
        OptimizationParams(**payload)


def test_validation_rejects_infeasible_optional_options():
    payload = {
        "grid_import_price_per_kwh": [0.3, 0.2, 0.1, 0.2],
        "solar_input_kwh": [0.0, 0.0, 0.0, 0.0],
        "usage_kwh": [1.0, 1.0, 1.0, 1.0],
        "battery_entities": [
            {
                "name": "b",
                "initial_kwh": 1.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 2.0,
                "charge_curve_kwh": [1.0],
                "discharge_curve_kwh": [1.0],
                "can_charge_from": 1,
            }
        ],
        "comfort_entities": [],
        "optional_entities": [
            {
                "name": "wash",
                "duration_timeslots": 2,
                "start_after_timeslot": 0,
                "start_before_timeslot": 4,
                "energy_kwh": 2.0,
                "options": 3,
                "min_option_gap_timeslots": 2,
                "allow_overlapping_options": False,
            }
        ],
    }

    with pytest.raises(ValidationError):
        OptimizationParams(**payload)


def test_validation_rejects_optional_curve_longer_than_duration():
    payload = {
        "grid_import_price_per_kwh": [0.3, 0.2, 0.1, 0.2],
        "solar_input_kwh": [0.0, 0.0, 0.0, 0.0],
        "usage_kwh": [1.0, 1.0, 1.0, 1.0],
        "battery_entities": [
            {
                "name": "b",
                "initial_kwh": 1.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 2.0,
                "charge_curve_kwh": [1.0],
                "discharge_curve_kwh": [1.0],
                "can_charge_from": 1,
            }
        ],
        "comfort_entities": [],
        "optional_entities": [
            {
                "name": "wash",
                "duration_timeslots": 2,
                "start_after_timeslot": 0,
                "start_before_timeslot": 4,
                "energy_kwh": [1.0, 2.0, 3.0],
                "options": 1,
                "min_option_gap_timeslots": 0,
                "allow_overlapping_options": True,
            }
        ],
    }

    with pytest.raises(ValidationError):
        OptimizationParams(**payload)


def test_validation_normalizes_optional_energy_to_full_profile_list():
    payload = {
        "grid_import_price_per_kwh": [0.3, 0.2, 0.1, 0.2],
        "solar_input_kwh": [0.0, 0.0, 0.0, 0.0],
        "usage_kwh": [1.0, 1.0, 1.0, 1.0],
        "rolling_window_slots": 24,
        "battery_entities": [
            {
                "name": "b",
                "initial_kwh": 1.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 2.0,
                "charge_curve_kwh": [1],
                "discharge_curve_kwh": [1],
                "can_charge_from": 1,
            }
        ],
        "comfort_entities": [],
        "optional_entities": [
            {
                "name": "one",
                "duration_timeslots": 4,
                "start_after_timeslot": 0,
                "start_before_timeslot": 4,
                "energy_kwh": 2.0,
                "options": 1,
            },
            {
                "name": "two",
                "duration_timeslots": 4,
                "start_after_timeslot": 0,
                "start_before_timeslot": 4,
                "energy_kwh": [1.0, 2.0],
                "options": 1,
            },
        ],
    }
    params = OptimizationParams(**payload)

    assert params.optional_entities[0].energy_kwh == [0.5, 0.5, 0.5, 0.5]
    assert params.optional_entities[1].energy_kwh == [1.0, 1.0, 2.0, 2.0]
    assert params.battery_entities[0].charge_curve_kwh == [1.0]
    assert params.battery_entities[0].charge_efficiency == pytest.approx(1.0)
    assert params.battery_entities[0].discharge_efficiency == pytest.approx(1.0)


@pytest.mark.parametrize(
    "field_name,field_value",
    [
        ("charge_efficiency", 0.0),
        ("charge_efficiency", 1.1),
        ("discharge_efficiency", 0.0),
        ("discharge_efficiency", 1.1),
    ],
)
def test_validation_rejects_out_of_range_efficiency(field_name, field_value):
    payload = {
        "grid_import_price_per_kwh": [0.2, 0.2, 0.2, 0.2],
        "solar_input_kwh": [0.0, 0.0, 0.0, 0.0],
        "usage_kwh": [0.0, 0.0, 0.0, 0.0],
        "battery_entities": [
            {
                "name": "b",
                "initial_kwh": 0.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 4.0,
                "charge_curve_kwh": [1.0],
                "discharge_curve_kwh": [1.0],
                "can_charge_from": 1,
                field_name: field_value,
            }
        ],
        "comfort_entities": [],
    }

    with pytest.raises(ValidationError):
        OptimizationParams(**payload)


def test_validation_rejects_battery_target_slot_outside_horizon():
    payload = {
        "grid_import_price_per_kwh": [0.3, 0.2, 0.1, 0.2],
        "solar_input_kwh": [0.0, 0.0, 0.0, 0.0],
        "usage_kwh": [1.0, 1.0, 1.0, 1.0],
        "battery_entities": [
            {
                "name": "ev",
                "initial_kwh": 1.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 10.0,
                "charge_curve_kwh": [2.0],
                "discharge_curve_kwh": [0.0],
                "can_charge_from": 1,
                "target": {
                    "timeslot": 10,
                    "soc_kwh": 8.0,
                    "mode": "at_least",
                    "tolerance_kwh": 0.1,
                },
            }
        ],
        "comfort_entities": [],
    }

    with pytest.raises(ValidationError):
        OptimizationParams(**payload)


def test_validation_rejects_unknown_charge_ingress_bits():
    # Any bits outside GRID(1) and PV(2) must be rejected at validation time.
    payload = {
        "grid_import_price_per_kwh": [0.2, 0.2, 0.2, 0.2],
        "solar_input_kwh": [0.0, 0.0, 0.0, 0.0],
        "usage_kwh": [0.0, 0.0, 0.0, 0.0],
        "battery_entities": [
            {
                "name": "b",
                "initial_kwh": 0.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 4.0,
                "charge_curve_kwh": [2.0],
                "discharge_curve_kwh": [0.0],
                "can_charge_from": 4,
            }
        ],
        "comfort_entities": [],
    }

    with pytest.raises(ValidationError):
        OptimizationParams(**payload)
