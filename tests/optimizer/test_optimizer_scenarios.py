import base64
import math
import json

import pytest
from pydantic import ValidationError

pytest.importorskip("numpy")
pytest.importorskip("pydantic")

from custom_components.wattplan.optimizer import mpc_power_optimizer as optimizer


def _run_optimizer(input_payload):
    optimizer.np.random.seed(12345)
    params = optimizer.OptimizationParams(**input_payload)
    return optimizer.optimize(params)


def _assert_common_result_shape(
    result, intervals, expected_entities, expect_suboptimal=False
):
    assert isinstance(result, dict)
    assert result["execution_time"] > 0
    assert result["generations"] > 1
    assert math.isfinite(float(result["fitness"]))
    assert math.isfinite(float(result["avg_price"]))
    assert isinstance(result["overconstrained"], bool)
    assert isinstance(result.get("suboptimal"), bool)
    assert isinstance(result.get("suboptimal_reasons"), list)
    assert isinstance(result["problems"], list)
    assert isinstance(result["entities"], list)
    assert isinstance(result.get("optional_entity_options"), list)
    assert isinstance(result.get("state"), str)
    assert isinstance(result.get("projections"), dict)
    assert len(result["entities"]) == expected_entities
    assert result["suboptimal"] is expect_suboptimal
    assert result["overconstrained"] is expect_suboptimal

    projections = result["projections"]
    assert math.isfinite(float(projections.get("baseline_cost")))
    assert math.isfinite(float(projections.get("projected_cost")))
    assert math.isfinite(float(projections.get("projected_savings_cost")))
    assert math.isfinite(float(projections.get("projected_savings_pct")))
    assert isinstance(projections.get("per_slot"), list)
    assert len(projections["per_slot"]) == intervals
    assert projections["projected_savings_cost"] == pytest.approx(
        projections["baseline_cost"] - projections["projected_cost"], abs=1e-6
    )
    if abs(float(projections["baseline_cost"])) > 1e-9:
        assert projections["projected_savings_pct"] == pytest.approx(
            (projections["projected_savings_cost"] / projections["baseline_cost"])
            * 100.0,
            abs=1e-6,
        )
    else:
        assert projections["projected_savings_pct"] == 0.0

    per_slot_baseline_sum = 0.0
    per_slot_projected_sum = 0.0
    per_slot_savings_sum = 0.0
    for slot in projections["per_slot"]:
        assert isinstance(slot, dict)
        assert "baseline_cost" in slot
        assert "projected_cost" in slot
        assert "projected_savings_cost" in slot
        assert "projected_savings_pct" in slot
        assert math.isfinite(float(slot["baseline_cost"]))
        assert math.isfinite(float(slot["projected_cost"]))
        assert math.isfinite(float(slot["projected_savings_cost"]))
        assert math.isfinite(float(slot["projected_savings_pct"]))
        assert slot["projected_savings_cost"] == pytest.approx(
            slot["baseline_cost"] - slot["projected_cost"], abs=1e-6
        )
        if abs(float(slot["baseline_cost"])) > 1e-9:
            assert slot["projected_savings_pct"] == pytest.approx(
                (slot["projected_savings_cost"] / slot["baseline_cost"]) * 100.0,
                abs=1e-6,
            )
        else:
            assert slot["projected_savings_pct"] == 0.0

        per_slot_baseline_sum += float(slot["baseline_cost"])
        per_slot_projected_sum += float(slot["projected_cost"])
        per_slot_savings_sum += float(slot["projected_savings_cost"])

    assert projections["baseline_cost"] == pytest.approx(
        per_slot_baseline_sum, abs=1e-6
    )
    assert projections["projected_cost"] == pytest.approx(
        per_slot_projected_sum, abs=1e-6
    )
    assert projections["projected_savings_cost"] == pytest.approx(
        per_slot_savings_sum, abs=1e-6
    )

    for entity in result["entities"]:
        assert "name" in entity
        assert entity["type"] in {"battery", "comfort"}
        assert isinstance(entity.get("schedule"), list)
        assert len(entity["schedule"]) == intervals

        for point in entity["schedule"]:
            assert isinstance(point, dict)
            assert "level" in point
            assert math.isfinite(float(point["level"]))
            if entity["type"] == "battery":
                assert point.get("state") in {"hold", "charge", "discharge"}
                assert int(point.get("charge_source", -1)) in {0, 1, 2, 3}
            else:
                assert isinstance(point.get("enabled"), bool)


def test_simple_input_returns_valid_result():
    payload = {
        "price_per_kwh": [0.4, 0.2, 0.3, 0.25],
        "solar_input_kwh": [0.0, 0.0, 0.0, 0.0],
        "usage_kwh": [1.0, 1.0, 1.0, 1.0],
        "battery_entities": [
            {
                "name": "home_battery",
                "initial_kwh": 5.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 10.0,
                "charge_curve_kwh": [2.0],
                "discharge_curve_kwh": [2.0],
                "can_charge_from": 1,
            }
        ],
        "comfort_entities": [],
    }

    result = _run_optimizer(payload)
    _assert_common_result_shape(result, intervals=4, expected_entities=1)


def test_projections_are_zero_when_baseline_is_zero():
    payload = {
        "price_per_kwh": [0.4, 0.2, 0.3, 0.25],
        "solar_input_kwh": [0.0, 0.0, 0.0, 0.0],
        "usage_kwh": [0.0, 0.0, 0.0, 0.0],
        "battery_entities": [
            {
                "name": "idle_battery",
                "initial_kwh": 1.0,
                "minimum_kwh": 1.0,
                "capacity_kwh": 1.0,
                "charge_curve_kwh": [0.0],
                "discharge_curve_kwh": [0.0],
                "can_charge_from": 1,
            }
        ],
        "comfort_entities": [],
    }

    result = _run_optimizer(payload)
    projections = result["projections"]
    assert projections["baseline_cost"] == pytest.approx(0.0, abs=1e-9)
    assert projections["projected_cost"] == pytest.approx(0.0, abs=1e-9)
    assert projections["projected_savings_cost"] == pytest.approx(0.0, abs=1e-9)
    assert projections["projected_savings_pct"] == pytest.approx(0.0, abs=1e-9)
    assert len(projections["per_slot"]) == 4
    for slot in projections["per_slot"]:
        assert slot["baseline_cost"] == pytest.approx(0.0, abs=1e-9)
        assert slot["projected_cost"] == pytest.approx(0.0, abs=1e-9)
        assert slot["projected_savings_cost"] == pytest.approx(0.0, abs=1e-9)
        assert slot["projected_savings_pct"] == pytest.approx(0.0, abs=1e-9)


def test_complex_48h_input_returns_valid_result():
    intervals = 48
    payload = {
        "price_per_kwh": [0.35 + ((i % 24) / 100.0) for i in range(intervals)],
        "solar_input_kwh": [
            max(0.0, 3.0 - abs(12 - (i % 24)) * 0.4) for i in range(intervals)
        ],
        "usage_kwh": [
            1.5 + (0.8 if 17 <= (i % 24) <= 22 else 0.0) for i in range(intervals)
        ],
        "battery_entities": [
            {
                "name": "car",
                "initial_kwh": 12.0,
                "minimum_kwh": 5.0,
                "capacity_kwh": 40.0,
                "charge_curve_kwh": [7.0],
                "discharge_curve_kwh": [0.0],
                "can_charge_from": 1,
            },
            {
                "name": "house_battery",
                "initial_kwh": 6.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 13.0,
                "charge_curve_kwh": [4.0, 4.0, 3.0, 1.0],
                "discharge_curve_kwh": [0.0],
                "can_charge_from": 3,
            },
        ],
        "comfort_entities": [
            {
                "name": "heatpump",
                "target_on_slots_per_rolling_window": 8,
                "min_consecutive_on_slots": 4,
                "min_consecutive_off_slots": 4,
                "max_consecutive_off_slots": 6,
                "power_usage_kwh": 2.5,
                "is_on_now": False,
                "on_slots_last_rolling_window": 3,
                "off_streak_slots_now": 1,
            },
            {
                "name": "water_heater",
                "target_on_slots_per_rolling_window": 10,
                "min_consecutive_on_slots": 8,
                "min_consecutive_off_slots": 4,
                "max_consecutive_off_slots": 5,
                "power_usage_kwh": 1.8,
                "is_on_now": True,
                "on_slots_last_rolling_window": 6,
                "off_streak_slots_now": 0,
            },
        ],
    }

    result = _run_optimizer(payload)
    _assert_common_result_shape(result, intervals=intervals, expected_entities=4)


def test_state_roundtrip_with_sliding_window_inputs():
    full_prices = [0.40, 0.22, 0.27, 0.33, 0.19, 0.41, 0.36, 0.24]
    full_solar = [0.0, 0.1, 0.5, 1.2, 0.8, 0.2, 0.0, 0.0]
    full_usage = [1.1, 1.0, 0.9, 1.2, 1.0, 1.4, 1.3, 1.1]

    base_payload = {
        "battery_entities": [
            {
                "name": "home_battery",
                "initial_kwh": 5.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 10.0,
                "charge_curve_kwh": [2.0],
                "discharge_curve_kwh": [2.0],
                "can_charge_from": 1,
            }
        ],
        "comfort_entities": [],
    }

    first_payload = {
        **base_payload,
        "price_per_kwh": full_prices[:6],
        "solar_input_kwh": full_solar[:6],
        "usage_kwh": full_usage[:6],
    }
    first_result = _run_optimizer(first_payload)
    _assert_common_result_shape(first_result, intervals=6, expected_entities=1)
    assert isinstance(first_result.get("state"), str)
    assert first_result["state"]
    assert not first_result["state"].startswith("{")

    decoded = json.loads(
        base64.urlsafe_b64decode(first_result["state"].encode("ascii")).decode("utf-8")
    )
    assert decoded["v"] == 1

    second_payload = {
        **base_payload,
        "price_per_kwh": full_prices[2:],
        "solar_input_kwh": full_solar[2:],
        "usage_kwh": full_usage[2:],
        "state": first_result["state"],
    }
    second_result = _run_optimizer(second_payload)
    _assert_common_result_shape(second_result, intervals=6, expected_entities=1)
    assert isinstance(second_result.get("state"), str)
    assert second_result["state"]


def test_optional_entities_return_independent_start_options_without_affecting_schedule():
    base_payload = {
        "price_per_kwh": [0.50, 0.48, 0.45, 0.30, 0.20, 0.18, 0.40, 0.55],
        "solar_input_kwh": [0.0, 0.0, 0.0, 0.2, 0.8, 0.9, 0.2, 0.0],
        "usage_kwh": [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
        "battery_entities": [
            {
                "name": "home_battery",
                "initial_kwh": 4.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 8.0,
                "charge_curve_kwh": [2.0],
                "discharge_curve_kwh": [2.0],
                "can_charge_from": 1,
            }
        ],
        "comfort_entities": [],
    }

    without_optional = _run_optimizer(base_payload)

    with_optional = _run_optimizer(
        {
            **base_payload,
            "optional_entities": [
                {
                    "name": "washer",
                    "duration_timeslots": 2,
                    "start_after_timeslot": 0,
                    "start_before_timeslot": 8,
                    "energy_kwh": 2.0,
                    "options": 3,
                    "min_option_gap_timeslots": 2,
                    "allow_overlapping_options": False,
                },
                {
                    "name": "dryer",
                    "duration_timeslots": 2,
                    "start_after_timeslot": 0,
                    "start_before_timeslot": 8,
                    "energy_kwh": [1.0, 3.0],
                    "options": 2,
                    "min_option_gap_timeslots": 0,
                    "allow_overlapping_options": True,
                },
            ],
        }
    )

    _assert_common_result_shape(with_optional, intervals=8, expected_entities=1)

    # Optional entities must not alter the optimized schedule.
    assert with_optional["entities"] == without_optional["entities"]

    by_name = {
        row["name"]: row["options"] for row in with_optional["optional_entity_options"]
    }
    assert set(by_name.keys()) == {"washer", "dryer"}
    assert len(by_name["washer"]) == 3
    assert len(by_name["dryer"]) == 2

    # Best starts should concentrate around low-cost/high-PV timeslots.
    assert by_name["washer"][0]["start_timeslot"] in {3, 4}
    assert by_name["dryer"][0]["start_timeslot"] in {3, 4}


def test_optional_entities_consider_pv_surplus_not_just_price():
    payload = {
        "price_per_kwh": [0.05, 0.05, 0.40, 0.40],
        "solar_input_kwh": [0.0, 0.0, 4.0, 4.0],
        "usage_kwh": [2.0, 2.0, 0.0, 0.0],
        "battery_entities": [
            {
                "name": "home_battery",
                "initial_kwh": 0.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 1.0,
                "charge_curve_kwh": [0.0],
                "discharge_curve_kwh": [0.0],
                "can_charge_from": 1,
            }
        ],
        "comfort_entities": [],
        "optional_entities": [
            {
                "name": "dishwasher",
                "duration_timeslots": 2,
                "start_after_timeslot": 0,
                "start_before_timeslot": 4,
                "energy_kwh": 2.0,
                "options": 2,
                "min_option_gap_timeslots": 0,
                "allow_overlapping_options": True,
            }
        ],
    }

    result = _run_optimizer(payload)
    options = result["optional_entity_options"][0]["options"]

    # Despite high price in timeslots 2-3, PV surplus should make it the best start.
    assert options[0]["start_timeslot"] == 2
    assert options[0]["incremental_cost"] == pytest.approx(0.0)
    assert result["suboptimal"] is False


def test_optional_entities_favor_negative_price_slots():
    payload = {
        "price_per_kwh": [0.30, -0.25, 0.35, 0.40],
        "solar_input_kwh": [0.0, 0.0, 0.0, 0.0],
        "usage_kwh": [1.0, 1.0, 1.0, 1.0],
        "battery_entities": [
            {
                "name": "home_battery",
                "initial_kwh": 0.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 1.0,
                "charge_curve_kwh": [0.0],
                "discharge_curve_kwh": [0.0],
                "can_charge_from": 1,
            }
        ],
        "comfort_entities": [],
        "optional_entities": [
            {
                "name": "ev_topup",
                "duration_timeslots": 1,
                "start_after_timeslot": 0,
                "start_before_timeslot": 4,
                "energy_kwh": 1.0,
                "options": 1,
                "min_option_gap_timeslots": 0,
                "allow_overlapping_options": True,
            }
        ],
    }

    result = _run_optimizer(payload)
    option = result["optional_entity_options"][0]["options"][0]
    assert option["start_timeslot"] == 1
    assert result["suboptimal"] is False


def test_optional_entities_favor_negative_price_with_pv_surplus_area():
    payload = {
        "price_per_kwh": [0.05, 0.05, -0.30, -0.30],
        "solar_input_kwh": [0.0, 0.0, 4.0, 4.0],
        "usage_kwh": [2.0, 2.0, 0.0, 0.0],
        "battery_entities": [
            {
                "name": "home_battery",
                "initial_kwh": 0.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 1.0,
                "charge_curve_kwh": [0.0],
                "discharge_curve_kwh": [0.0],
                "can_charge_from": 1,
            }
        ],
        "comfort_entities": [],
        "optional_entities": [
            {
                "name": "dryer",
                "duration_timeslots": 2,
                "start_after_timeslot": 0,
                "start_before_timeslot": 4,
                "energy_kwh": 2.0,
                "options": 1,
                "min_option_gap_timeslots": 0,
                "allow_overlapping_options": True,
            }
        ],
    }

    result = _run_optimizer(payload)
    option = result["optional_entity_options"][0]["options"][0]
    assert option["start_timeslot"] == 2
    assert result["suboptimal"] is False


def test_validation_rejects_series_length_mismatch():
    payload = {
        "price_per_kwh": [0.3, 0.2, 0.1, 0.2],
        "solar_input_kwh": [0.0, 0.0, 0.0],
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
    }

    with pytest.raises(ValidationError):
        optimizer.OptimizationParams(**payload)


def test_validation_rejects_too_short_horizon():
    payload = {
        "price_per_kwh": [0.3, 0.2, 0.1],
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
        optimizer.OptimizationParams(**payload)


def test_validation_rejects_comfort_min_on_not_less_than_solve_horizon():
    payload = {
        "price_per_kwh": [0.3, 0.2, 0.1, 0.2],
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
        optimizer.OptimizationParams(**payload)


def test_validation_rejects_infeasible_optional_options():
    payload = {
        "price_per_kwh": [0.3, 0.2, 0.1, 0.2],
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
        optimizer.OptimizationParams(**payload)


def test_optional_energy_curve_is_spread_over_duration_timeslots():
    payload = {
        "price_per_kwh": [1.0, 1.0, 1.0, 1.0],
        "solar_input_kwh": [0.0, 0.0, 0.0, 0.0],
        "usage_kwh": [0.0, 0.0, 0.0, 0.0],
        "battery_entities": [
            {
                "name": "b",
                "initial_kwh": 0.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 1.0,
                "charge_curve_kwh": [0.0],
                "discharge_curve_kwh": [0.0],
                "can_charge_from": 1,
            }
        ],
        "comfort_entities": [],
        "optional_entities": [
            {
                "name": "spread_curve",
                "duration_timeslots": 4,
                "start_after_timeslot": 0,
                "start_before_timeslot": 4,
                "energy_kwh": [1.0, 2.0],
                "options": 1,
                "min_option_gap_timeslots": 0,
                "allow_overlapping_options": True,
            }
        ],
    }

    result = _run_optimizer(payload)
    option = result["optional_entity_options"][0]["options"][0]
    assert option["incremental_cost"] == pytest.approx(6.0)


def test_validation_rejects_optional_curve_longer_than_duration():
    payload = {
        "price_per_kwh": [0.3, 0.2, 0.1, 0.2],
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
        optimizer.OptimizationParams(**payload)


def test_validation_normalizes_optional_energy_to_full_profile_list():
    payload = {
        "price_per_kwh": [0.3, 0.2, 0.1, 0.2],
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
    params = optimizer.OptimizationParams(**payload)

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
        "price_per_kwh": [0.2, 0.2, 0.2, 0.2],
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
        optimizer.OptimizationParams(**payload)


def test_battery_target_deadline_at_least_is_enforced():
    payload = {
        "price_per_kwh": [0.1, 0.1, 0.1, 0.5],
        "solar_input_kwh": [0.0, 0.0, 0.0, 0.0],
        "usage_kwh": [0.0, 0.0, 0.0, 0.0],
        "battery_entities": [
            {
                "name": "ev",
                "initial_kwh": 1.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 10.0,
                "charge_curve_kwh": [4.0],
                "discharge_curve_kwh": [0.0],
                "can_charge_from": 1,
                "target": {
                    "timeslot": 2,
                    "soc_kwh": 8.0,
                    "mode": "at_least",
                    "tolerance_kwh": 0.0,
                },
            }
        ],
        "comfort_entities": [],
    }

    result = _run_optimizer(payload)
    schedule = result["entities"][0]["schedule"]
    assert schedule[2]["level"] == pytest.approx(8.0, abs=1e-6)
    assert result["suboptimal"] is False


def test_suboptimal_reasons_include_comfort_target_unmet_key():
    payload = {
        "price_per_kwh": [0.2, 0.2, 0.2, 0.2],
        "solar_input_kwh": [0.0, 0.0, 0.0, 0.0],
        "usage_kwh": [0.0, 0.0, 0.0, 0.0],
        "battery_entities": [
            {
                "name": "dummy",
                "initial_kwh": 0.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 1.0,
                "charge_curve_kwh": [0.0],
                "discharge_curve_kwh": [0.0],
                "can_charge_from": 1,
            }
        ],
        "comfort_entities": [
            {
                "name": "hvac",
                "target_on_slots_per_rolling_window": 24,
                "max_consecutive_off_slots": 8,
                "power_usage_kwh": 1.0,
                "is_on_now": False,
                "on_slots_last_rolling_window": 0,
                "off_streak_slots_now": 0,
            }
        ],
    }

    result = _run_optimizer(payload)
    assert result["suboptimal"] is True
    assert "comfort_target_unmet" in result["suboptimal_reasons"]


def test_validation_rejects_battery_target_slot_outside_horizon():
    payload = {
        "price_per_kwh": [0.3, 0.2, 0.1, 0.2],
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
        optimizer.OptimizationParams(**payload)


def test_validation_rejects_unknown_charge_source_bits():
    # Any bits outside GRID(1) and PV(2) must be rejected at validation time.
    payload = {
        "price_per_kwh": [0.2, 0.2, 0.2, 0.2],
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
        optimizer.OptimizationParams(**payload)


def test_grid_flag_controls_charging_when_no_pv_surplus_exists():
    # With no PV surplus available, a PV-only battery must not charge,
    # while a GRID-allowed battery should be able to hit its target.
    base_payload = {
        "price_per_kwh": [0.2, 0.2, 0.2, 0.2],
        "solar_input_kwh": [0.0, 0.0, 0.0, 0.0],
        "usage_kwh": [0.0, 0.0, 0.0, 0.0],
        "battery_entities": [],
        "comfort_entities": [],
    }

    pv_only_payload = {
        **base_payload,
        "battery_entities": [
            {
                "name": "pv_only",
                "initial_kwh": 0.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 4.0,
                "charge_curve_kwh": [4.0],
                "discharge_curve_kwh": [0.0],
                "can_charge_from": 2,
                "target": {
                    "timeslot": 3,
                    "soc_kwh": 4.0,
                    "mode": "at_least",
                    "tolerance_kwh": 0.0,
                },
            }
        ],
    }

    grid_allowed_payload = {
        **base_payload,
        "battery_entities": [
            {
                "name": "grid_allowed",
                "initial_kwh": 0.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 4.0,
                "charge_curve_kwh": [4.0],
                "discharge_curve_kwh": [0.0],
                "can_charge_from": 1,
                "target": {
                    "timeslot": 3,
                    "soc_kwh": 4.0,
                    "mode": "at_least",
                    "tolerance_kwh": 0.0,
                },
            }
        ],
    }

    pv_only_result = _run_optimizer(pv_only_payload)
    pv_only_schedule = pv_only_result["entities"][0]["schedule"]
    assert pv_only_schedule[3]["level"] == pytest.approx(0.0, abs=1e-6)
    assert pv_only_result["suboptimal"] is True
    assert "battery_target_unmet" in pv_only_result["suboptimal_reasons"]

    grid_allowed_result = _run_optimizer(grid_allowed_payload)
    grid_allowed_schedule = grid_allowed_result["entities"][0]["schedule"]
    assert grid_allowed_schedule[3]["level"] == pytest.approx(4.0, abs=1e-6)
    assert grid_allowed_result["suboptimal"] is False


def test_pv_flag_controls_charging_when_solar_surplus_exists():
    # With persistent PV surplus, a battery that allows PV should charge,
    # while charging-disabled should stay at its initial level.
    base_payload = {
        "price_per_kwh": [0.2, 0.2, 0.2, 0.2],
        "solar_input_kwh": [4.0, 4.0, 4.0, 4.0],
        "usage_kwh": [0.0, 0.0, 0.0, 0.0],
        "battery_entities": [],
        "comfort_entities": [],
    }

    charging_disabled_payload = {
        **base_payload,
        "battery_entities": [
            {
                "name": "charging_disabled",
                "initial_kwh": 0.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 4.0,
                "charge_curve_kwh": [4.0],
                "discharge_curve_kwh": [0.0],
                "can_charge_from": 0,
                "target": {
                    "timeslot": 3,
                    "soc_kwh": 4.0,
                    "mode": "at_least",
                    "tolerance_kwh": 0.0,
                },
            }
        ],
    }

    pv_allowed_payload = {
        **base_payload,
        "battery_entities": [
            {
                "name": "pv_allowed",
                "initial_kwh": 0.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 4.0,
                "charge_curve_kwh": [4.0],
                "discharge_curve_kwh": [0.0],
                "can_charge_from": 2,
                "target": {
                    "timeslot": 3,
                    "soc_kwh": 4.0,
                    "mode": "at_least",
                    "tolerance_kwh": 0.0,
                },
            }
        ],
    }

    charging_disabled_result = _run_optimizer(charging_disabled_payload)
    charging_disabled_schedule = charging_disabled_result["entities"][0]["schedule"]
    assert charging_disabled_schedule[3]["level"] == pytest.approx(0.0, abs=1e-6)
    assert charging_disabled_result["suboptimal"] is True
    assert "battery_target_unmet" in charging_disabled_result["suboptimal_reasons"]

    pv_allowed_result = _run_optimizer(pv_allowed_payload)
    pv_allowed_schedule = pv_allowed_result["entities"][0]["schedule"]
    assert pv_allowed_schedule[3]["level"] == pytest.approx(4.0, abs=1e-6)
    assert pv_allowed_result["suboptimal"] is False


def test_comfort_entity_is_kept_on_for_required_slots():
    # The planner should satisfy the minimum ON-slot requirement,
    # even when prices vary across the horizon.
    payload = {
        "price_per_kwh": [0.5, 0.5, 0.2, 0.2, 0.2, 0.5, 0.5, 0.5],
        "solar_input_kwh": [0.0] * 8,
        "usage_kwh": [0.0] * 8,
        "battery_entities": [
            {
                "name": "dummy",
                "initial_kwh": 0.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 1.0,
                "charge_curve_kwh": [0.0],
                "discharge_curve_kwh": [0.0],
                "can_charge_from": 1,
            }
        ],
        "comfort_entities": [
            {
                "name": "hvac",
                "target_on_slots_per_rolling_window": 3,
                "min_consecutive_on_slots": 4,
                "min_consecutive_off_slots": 4,
                "max_consecutive_off_slots": 8,
                "power_usage_kwh": 1.0,
                "is_on_now": False,
                "on_slots_last_rolling_window": 0,
                "off_streak_slots_now": 0,
            }
        ],
    }

    result = _run_optimizer(payload)
    comfort = next(e for e in result["entities"] if e["type"] == "comfort")
    enabled_indices = [
        idx for idx, point in enumerate(comfort["schedule"]) if point["enabled"]
    ]
    enabled_slots = sum(1 for p in comfort["schedule"] if p["enabled"])
    # Expected outcome: comfort entity is ON at least target_on_slots_per_rolling_window times.
    assert enabled_slots >= 3
    # Expected outcome: with min_consecutive_on_slots=4, ON decisions are blocky.
    assert enabled_slots >= 4
    assert enabled_indices == list(range(enabled_indices[0], enabled_indices[0] + 4))


def test_battery_never_discharges_below_minimum_kwh():
    payload = {
        "price_per_kwh": [2.0] * 8 + [1.0] * 8,
        "solar_input_kwh": [0.0] * 16,
        "usage_kwh": [0.2] * 16,
        "battery_entities": [
            {
                "name": "house",
                "initial_kwh": 1.95,
                "minimum_kwh": 1.0,
                "capacity_kwh": 10.0,
                "charge_curve_kwh": [1.25],
                "discharge_curve_kwh": [1.25],
                "can_charge_from": 3,
            }
        ],
        "comfort_entities": [],
    }

    result = _run_optimizer(payload)
    schedule = result["entities"][0]["schedule"]
    levels = [float(point["level"]) for point in schedule]
    assert min(levels) >= 1.0 - 1e-6


def test_charge_efficiency_increases_required_input_cost():
    base_payload = {
        "price_per_kwh": [0.2, 0.2, 0.2, 0.2],
        "solar_input_kwh": [0.0, 0.0, 0.0, 0.0],
        "usage_kwh": [0.0, 0.0, 0.0, 0.0],
        "battery_entities": [
            {
                "name": "b",
                "initial_kwh": 0.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 2.0,
                "charge_curve_kwh": [2.0],
                "discharge_curve_kwh": [0.0],
                "can_charge_from": 1,
                "target": {
                    "timeslot": 0,
                    "soc_kwh": 1.0,
                    "mode": "at_least",
                    "tolerance_kwh": 0.0,
                },
            }
        ],
        "comfort_entities": [],
    }

    efficient_payload = json.loads(json.dumps(base_payload))
    efficient_payload["battery_entities"][0]["charge_efficiency"] = 1.0
    lossy_payload = json.loads(json.dumps(base_payload))
    lossy_payload["battery_entities"][0]["charge_efficiency"] = 0.9

    efficient = _run_optimizer(efficient_payload)
    lossy = _run_optimizer(lossy_payload)

    assert efficient["entities"][0]["schedule"][0]["level"] >= 1.0 - 1e-6
    assert lossy["entities"][0]["schedule"][0]["level"] >= 1.0 - 1e-6
    assert (
        lossy["projections"]["projected_cost"]
        > efficient["projections"]["projected_cost"]
    )


def test_discharge_efficiency_reduces_deliverable_energy():
    base_payload = {
        "price_per_kwh": [1.0, 1.0, 0.1, 0.1],
        "solar_input_kwh": [0.0, 0.0, 0.0, 0.0],
        "usage_kwh": [1.0, 1.0, 1.0, 1.0],
        "battery_entities": [
            {
                "name": "b",
                "initial_kwh": 2.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 2.0,
                "charge_curve_kwh": [0.0],
                "discharge_curve_kwh": [1.0],
                "can_charge_from": 0,
            }
        ],
        "comfort_entities": [],
    }

    efficient_payload = json.loads(json.dumps(base_payload))
    efficient_payload["battery_entities"][0]["discharge_efficiency"] = 1.0
    lossy_payload = json.loads(json.dumps(base_payload))
    lossy_payload["battery_entities"][0]["discharge_efficiency"] = 0.5

    efficient = _run_optimizer(efficient_payload)
    lossy = _run_optimizer(lossy_payload)

    assert (
        lossy["projections"]["projected_cost"]
        > efficient["projections"]["projected_cost"]
    )
    assert (
        lossy["projections"]["per_slot"][0]["projected_cost"]
        > efficient["projections"]["per_slot"][0]["projected_cost"]
    )
