import base64
import math
import json

import pytest
from pydantic import ValidationError

pytest.importorskip("numpy")
pytest.importorskip("pydantic")

from custom_components.wattplan.optimizer import mpc_power_optimizer as optimizer
from custom_components.wattplan.test_plan_invariants import assert_plan_invariants


def _run_optimizer(input_payload):
    optimizer.np.random.seed(12345)
    params = optimizer.OptimizationParams(**input_payload)
    return assert_plan_invariants(optimizer.optimize(params))


def _level_increase_count(schedule, *, initial_level):
    """Return how many schedule points increase the battery level."""
    increases = 0
    previous = float(initial_level)
    for point in schedule:
        current = float(point["level"])
        if current > previous + 1e-6:
            increases += 1
        previous = current
    return increases


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
                assert point.get("state") in {
                    "preserve",
                    "self_consume",
                    "grid_charge",
                }
            else:
                assert isinstance(point.get("enabled"), bool)


def test_simple_input_returns_valid_result():
    payload = {
        "grid_import_price_per_kwh": [0.4, 0.2, 0.3, 0.25],
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
        "grid_import_price_per_kwh": [0.4, 0.2, 0.3, 0.25],
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
        "grid_import_price_per_kwh": [0.35 + ((i % 24) / 100.0) for i in range(intervals)],
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
    full_export_prices = [0.05, 0.01, 0.02, 0.08, 0.10, 0.06, 0.03, 0.02]
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
        "grid_import_price_per_kwh": full_prices[:6],
        "grid_export_price_per_kwh": full_export_prices[:6],
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
    assert "grid_import_price_per_kwh" in decoded
    assert "grid_export_price_per_kwh" in decoded
    assert decoded["grid_export_price_per_kwh"] == pytest.approx(full_export_prices[:6])

    second_payload = {
        **base_payload,
        "grid_import_price_per_kwh": full_prices[2:],
        "grid_export_price_per_kwh": full_export_prices[2:],
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
        "grid_import_price_per_kwh": [0.50, 0.48, 0.45, 0.30, 0.20, 0.18, 0.40, 0.55],
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
        "grid_import_price_per_kwh": [0.05, 0.05, 0.40, 0.40],
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
        "grid_import_price_per_kwh": [0.30, -0.25, 0.35, 0.40],
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
        "grid_import_price_per_kwh": [0.05, 0.05, -0.30, -0.30],
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


def test_feed_in_prices_shift_pv_charging_to_lower_export_value_slots():
    zero_feed_in_payload = {
        "grid_import_price_per_kwh": [0.2, 0.2, 1.2, 0.2],
        "solar_input_kwh": [1.0, 1.0, 0.0, 0.0],
        "usage_kwh": [0.0, 0.0, 1.0, 0.0],
        "battery_entities": [
            {
                "name": "home_battery",
                "initial_kwh": 0.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 1.0,
                "charge_curve_kwh": [0.05],
                "discharge_curve_kwh": [0.05],
                "can_charge_from": 2,
            }
        ],
        "comfort_entities": [],
    }

    valued_feed_in_payload = {
        **zero_feed_in_payload,
        "grid_export_price_per_kwh": [1.0, 0.1, 0.0, 0.0],
    }

    zero_feed_in = _run_optimizer(zero_feed_in_payload)
    valued_feed_in = _run_optimizer(valued_feed_in_payload)

    zero_schedule = zero_feed_in["entities"][0]["schedule"]
    valued_schedule = valued_feed_in["entities"][0]["schedule"]

    assert zero_schedule[0]["state"] == "self_consume"
    assert valued_schedule[0]["state"] == "self_consume"
    assert valued_schedule[1]["state"] == "self_consume"
    assert valued_feed_in["projections"]["projected_cost"] < (
        zero_feed_in["projections"]["projected_cost"]
    )


def test_battery_schedule_state_emits_policy_names():
    entity = optimizer.BatteryEntity(
        name="battery",
        initial_kwh=0.0,
        minimum_kwh=0.0,
        capacity_kwh=10.0,
        target=None,
        charge_curve_kwh=[1.0],
        discharge_curve_kwh=[1.0],
        charge_efficiency=1.0,
        discharge_efficiency=1.0,
        throughput_cost_per_kwh=0.0,
        action_deadband_kwh=0.0,
        mode_switch_cost=0.0,
        prefer_pv_surplus_charging=False,
        can_charge_from=3,
    )
    result = {
        "battery_states": optimizer.np.asarray(
            [[1, 1, 1, 0, 2]], dtype=optimizer.np.float64
        ),
        "battery_levels": optimizer.np.asarray(
            [[0.0, 1.0, 2.0, 3.0, 3.0, 2.0]], dtype=optimizer.np.float64
        ),
        "battery_charge_grid": optimizer.np.asarray(
            [[1.0, 0.0, 1.0, 0.0, 0.0]], dtype=optimizer.np.float64
        ),
        "battery_charge_pv": optimizer.np.asarray(
            [[0.0, 1.0, 1.0, 0.0, 0.0]], dtype=optimizer.np.float64
        ),
        "battery_discharge": optimizer.np.asarray(
            [[0.0, 0.0, 0.0, 0.0, 1.0]], dtype=optimizer.np.float64
        ),
        "battery_preserve": optimizer.np.asarray(
            [[False, False, True, True, False]], dtype=optimizer.np.bool_
        ),
        "comfort_enabled": optimizer.np.asarray(
            optimizer.np.zeros((0, 5)), dtype=optimizer.np.float64
        ),
    }

    assert optimizer._battery_schedule_state(result, entity, 0, 0) == "grid_charge"
    assert optimizer._battery_schedule_state(result, entity, 0, 1) == "self_consume"
    assert optimizer._battery_schedule_state(result, entity, 0, 2) == "grid_charge"
    assert optimizer._battery_schedule_state(result, entity, 0, 3) == "preserve"
    assert optimizer._battery_schedule_state(result, entity, 0, 4) == "self_consume"


def test_battery_schedule_state_defaults_pv_or_neutral_flow_to_self_consume():
    entity = optimizer.BatteryEntity(
        name="battery",
        initial_kwh=0.0,
        minimum_kwh=0.0,
        capacity_kwh=10.0,
        target=None,
        charge_curve_kwh=[1.0],
        discharge_curve_kwh=[1.0],
        charge_efficiency=1.0,
        discharge_efficiency=1.0,
        throughput_cost_per_kwh=0.0,
        action_deadband_kwh=0.0,
        mode_switch_cost=0.0,
        prefer_pv_surplus_charging=False,
        can_charge_from=2,
    )
    result = {
        "battery_states": optimizer.np.asarray([[1, 0]], dtype=optimizer.np.float64),
        "battery_levels": optimizer.np.asarray(
            [[0.0, 1.0, 1.0]], dtype=optimizer.np.float64
        ),
        "battery_charge_grid": optimizer.np.asarray(
            [[0.0, 0.0]], dtype=optimizer.np.float64
        ),
        "battery_charge_pv": optimizer.np.asarray(
            [[1.0, 0.0]], dtype=optimizer.np.float64
        ),
        "battery_discharge": optimizer.np.asarray(
            [[0.0, 0.0]], dtype=optimizer.np.float64
        ),
        "battery_preserve": optimizer.np.asarray(
            [[False, False]], dtype=optimizer.np.bool_
        ),
        "comfort_enabled": optimizer.np.asarray(
            optimizer.np.zeros((0, 2)), dtype=optimizer.np.float64
        ),
    }

    assert optimizer._battery_schedule_state(result, entity, 0, 0) == "self_consume"
    assert optimizer._battery_schedule_state(result, entity, 0, 1) == "self_consume"


def test_model_marks_preserve_when_forced_discharge_now_is_more_expensive():
    payload = {
        "grid_import_price_per_kwh": [0.10, 1.00, 1.00, 1.00],
        "grid_export_price_per_kwh": [0.0, 0.0, 0.0, 0.0],
        "solar_input_kwh": [0.0, 0.0, 0.0, 0.0],
        "usage_kwh": [1.0, 1.0, 1.0, 1.0],
        "battery_entities": [
            {
                "name": "battery",
                "initial_kwh": 1.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 1.0,
                "charge_curve_kwh": [0.0],
                "discharge_curve_kwh": [1.0],
                "can_charge_from": 0,
            }
        ],
        "comfort_entities": [],
    }

    result = _run_optimizer(payload)
    schedule = result["entities"][0]["schedule"]

    assert schedule[0]["state"] == "preserve"
    assert schedule[0]["level"] == pytest.approx(1.0)
    assert schedule[1]["state"] == "self_consume"
    assert schedule[1]["level"] == pytest.approx(0.0)


def test_model_marks_preserve_for_marginal_load_after_pv_surplus():
    payload = {
        "grid_import_price_per_kwh": [0.10, 1.00, 1.00, 1.00],
        "grid_export_price_per_kwh": [0.0, 0.0, 0.0, 0.0],
        "solar_input_kwh": [1.2, 0.0, 0.0, 0.0],
        "usage_kwh": [1.0, 1.0, 1.0, 1.0],
        "battery_entities": [
            {
                "name": "battery",
                "initial_kwh": 1.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 1.0,
                "charge_curve_kwh": [0.0],
                "discharge_curve_kwh": [1.0],
                "can_charge_from": 0,
            }
        ],
        "comfort_entities": [],
    }

    result = _run_optimizer(payload)
    schedule = result["entities"][0]["schedule"]

    assert schedule[0]["state"] == "preserve"
    assert schedule[0]["level"] == pytest.approx(1.0)
    assert schedule[1]["state"] == "self_consume"
    assert schedule[1]["level"] == pytest.approx(0.0)


def test_battery_target_does_not_create_preserve_policy_by_itself():
    payload = {
        "grid_import_price_per_kwh": [0.10, 0.10, 0.10, 0.10],
        "grid_export_price_per_kwh": [0.0, 0.0, 0.0, 0.0],
        "solar_input_kwh": [1.0, 1.0, 1.0, 1.0],
        "usage_kwh": [0.0, 0.0, 0.0, 0.0],
        "battery_entities": [
            {
                "name": "battery",
                "initial_kwh": 0.5,
                "minimum_kwh": 0.0,
                "capacity_kwh": 1.0,
                "target": {"timeslot": 3, "soc_kwh": 0.5},
                "charge_curve_kwh": [1.0],
                "discharge_curve_kwh": [1.0],
                "can_charge_from": 2,
            }
        ],
        "comfort_entities": [],
    }

    result = _run_optimizer(payload)

    assert all(
        point["state"] == "self_consume"
        for point in result["entities"][0]["schedule"]
    )


def test_live_exported_deye_forecast_self_consumes_until_evening_discharge():
    # Exported from wattplan.export_planner_input on 2026-04-22 for the live
    # Home Assistant setup. Although the battery remains full until the evening
    # price rise, the model says self-consume is still the correct policy: early
    # marginal load is covered by PV surplus or can be recovered before the
    # high-value discharge window.
    payload = {
        "grid_import_price_per_kwh": [
            0.577338,
            0.573134,
            0.57986,
            0.579954,
            0.57958,
            0.580234,
            0.580421,
            0.589296,
            0.623019,
            0.935472,
            0.945374,
            0.947523,
            1.086435,
            0.961442,
            1.174247,
            1.180226,
            1.529327,
            1.31017,
            1.381634,
            1.620503,
            1.263274,
            1.766701,
            1.358,
            1.180226,
            1.160048,
            1.337009,
            0.861701,
            0.861608,
            0.692055,
            0.846287,
            0.674306,
            0.66132,
        ],
        "grid_export_price_per_kwh": [
            -0.002989,
            -0.007193,
            -0.000467,
            -0.000374,
            -0.000747,
            -0.000093,
            0.000093,
            0.008968,
            0.042692,
            0.03662,
            0.046522,
            0.048671,
            0.187582,
            0.06259,
            0.275395,
            0.281374,
            0.630475,
            0.411317,
            0.482782,
            0.72165,
            0.364422,
            0.867849,
            0.459147,
            0.281374,
            0.261195,
            0.756682,
            0.281374,
            0.28128,
            0.111727,
            0.26596,
            0.093978,
            0.080993,
        ],
        "solar_input_kwh": [
            4.749,
            2.2255,
            2.2255,
            2.0435,
            2.0435,
            1.8395,
            1.8395,
            1.5905,
            1.5905,
            1.313,
            1.313,
            1.0025,
            1.0025,
            0.71,
            0.71,
            0.3945,
            0.3945,
            0.1235,
            0.1235,
            0.032,
            0.032,
            0.0095,
            0.0095,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
        ],
        "usage_kwh": [
            0.2169704598561457,
            0.24453594597439476,
            0.23539831744649636,
            0.23312404106022935,
            0.2298197857041873,
            0.20621877304580288,
            0.2215179470598735,
            0.2548262595346826,
            0.25221516533968485,
            0.30891192422506575,
            0.25971747314094706,
            0.30657993534027406,
            0.3442407494160655,
            0.34868868240295475,
            0.2307738647202011,
            0.1994668734929886,
            0.19826671285536,
            0.2305931644980437,
            0.21648351868157095,
            0.20391384014856817,
            0.19844142051051475,
            0.24931453994647473,
            0.19962354065757215,
            0.19413691567983707,
            0.20074486184872037,
            0.20200322537291235,
            0.25446272213855586,
            0.20780401659467665,
            0.2270879529659598,
            0.3451252775276283,
            0.2691395209923915,
            0.23195316573769167,
        ],
        "rolling_window_slots": 24,
        "throughput_cost_per_kwh": 0.02,
        "action_deadband_kwh": 0.05,
        "mode_switch_cost": 0.01,
        "battery_entities": [
            {
                "name": "Battery",
                "initial_kwh": 10.0,
                "minimum_kwh": 1.0,
                "capacity_kwh": 10.0,
                "charge_efficiency": 0.9,
                "discharge_efficiency": 0.9,
                "charge_curve_kwh": [1.25],
                "discharge_curve_kwh": [1.25],
                "can_charge_from": 3,
                "prefer_pv_surplus_charging": True,
            }
        ],
        "comfort_entities": [],
        "optional_entities": [
            {
                "name": "Hvidevarer",
                "duration_timeslots": 8,
                "start_after_timeslot": 0,
                "start_before_timeslot": 32,
                "energy_kwh": 2.0,
                "options": 2,
                "min_option_gap_timeslots": 4,
            }
        ],
    }

    result = _run_optimizer(payload)
    schedule = result["entities"][0]["schedule"]

    assert {point["state"] for point in schedule} == {"self_consume"}
    assert all(point["level"] == pytest.approx(10.0) for point in schedule[:17])
    assert schedule[17]["level"] < 10.0


def test_live_grid_export_benchmark_scenario_uses_real_15min_stromligning_values():
    # Live Home Assistant data captured on 2026-03-09 in Europe/Copenhagen.
    # Strømligning is native 15-minute price data. Deye daily energy totals are
    # available as hourly recorder statistics, so each hourly kWh delta is split
    # evenly into the four matching 15-minute slots.
    buy_prices = [
        1.637598,
        1.571761,
        1.555792,
        1.507979,
        1.544679,
        1.527123,
        1.517785,
        1.503683,
        1.519652,
        1.509287,
        1.528897,
        1.529738,
        1.530018,
        1.53394,
        1.533847,
        1.540571,
        1.558127,
        1.554392,
        1.587263,
        1.659356,
        1.557287,
        1.679527,
        1.831745,
        2.060352,
        2.062983,
        2.176165,
        2.421114,
        2.451371,
        2.785783,
        2.778592,
        2.432601,
        2.147777,
        2.871323,
        2.224726,
        2.117987,
        1.84745,
        2.241722,
        1.957365,
        1.833162,
        1.703077,
        1.831388,
        1.744633,
        1.628462,
        1.561692,
        1.591295,
        1.540774,
        1.481568,
        1.427031,
        1.417599,
        1.399296,
        1.393506,
        1.382393,
        1.36493,
        1.373615,
        1.394907,
        1.430673,
        1.363156,
        1.489786,
        1.537039,
        1.639015,
        1.623513,
        1.67861,
        1.831295,
        2.076617,
        1.689723,
        2.029177,
        2.153566,
        2.474997,
        2.666953,
        2.990813,
        3.343061,
        3.904025,
        3.644041,
        3.828476,
        3.822033,
        3.81783,
        3.796912,
        3.51629,
        3.513022,
        3.399466,
        3.513302,
        3.301598,
        2.978392,
        2.880245,
        2.334266,
        2.215014,
        2.116773,
        2.004524,
        2.195029,
        2.098002,
        2.083247,
        1.962407,
        2.04608,
        2.001722,
        1.942143,
        1.953909,
    ]
    feed_in_prices = [
        1.134348,
        1.068511,
        1.052542,
        1.004729,
        1.041429,
        1.023873,
        1.014535,
        1.000433,
        1.016402,
        1.006037,
        1.025647,
        1.026488,
        1.026768,
        1.03069,
        1.030597,
        1.037321,
        1.054877,
        1.051142,
        1.084013,
        1.156106,
        1.054037,
        1.176277,
        1.328495,
        1.557102,
        1.315608,
        1.428791,
        1.673739,
        1.703996,
        2.038408,
        2.031217,
        1.685226,
        1.400402,
        2.123948,
        1.477351,
        1.370612,
        1.100075,
        1.494347,
        1.20999,
        1.085787,
        0.955702,
        1.084013,
        0.997258,
        0.881087,
        0.814317,
        0.84392,
        0.793399,
        0.734193,
        0.679656,
        0.670224,
        0.651921,
        0.646131,
        0.635018,
        0.617555,
        0.62624,
        0.647532,
        0.683298,
        0.615781,
        0.742411,
        0.789664,
        0.89164,
        0.876138,
        0.931235,
        1.08392,
        1.329242,
        0.942348,
        1.281802,
        1.406191,
        1.727623,
        1.187203,
        1.511063,
        1.863311,
        2.424275,
        2.164291,
        2.348726,
        2.342283,
        2.33808,
        2.317162,
        2.03654,
        2.033272,
        1.919716,
        2.033552,
        1.821848,
        1.498642,
        1.400495,
        1.586891,
        1.467639,
        1.369398,
        1.257149,
        1.447654,
        1.350627,
        1.335872,
        1.215032,
        1.298705,
        1.254347,
        1.194768,
        1.206534,
    ]
    solar_input = [0.0] * len(buy_prices)
    usage = [0.0] * len(buy_prices)
    pv_energy_hourly = {
        8: 2.52,
        9: 2.43,
        10: 5.50,
        11: 8.17,
        12: 8.76,
        13: 7.75,
        14: 6.23,
        15: 3.32,
        16: 0.98,
        18: 0.28,
    }
    load_energy_hourly = {
        8: 5.32,
        9: 1.51,
        10: 1.51,
        11: 1.02,
        12: 0.97,
        13: 0.83,
        14: 0.97,
        15: 0.75,
        16: 0.91,
        18: 1.83,
    }
    for hour, value in pv_energy_hourly.items():
        for slot in range(hour * 4, hour * 4 + 4):
            solar_input[slot] = value / 4.0
    for hour, value in load_energy_hourly.items():
        for slot in range(hour * 4, hour * 4 + 4):
            usage[slot] = value / 4.0

    base_payload = {
        "grid_import_price_per_kwh": buy_prices,
        "solar_input_kwh": solar_input,
        "usage_kwh": usage,
        "battery_entities": [
            {
                "name": "benchmark_battery",
                "initial_kwh": 0.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 10.0,
                "charge_curve_kwh": [1.25],
                "discharge_curve_kwh": [1.25],
                "can_charge_from": 2,
            }
        ],
        "comfort_entities": [],
    }

    without_feed_in = _run_optimizer(base_payload)
    with_feed_in = _run_optimizer(
        {
            **base_payload,
            "grid_export_price_per_kwh": feed_in_prices,
        }
    )

    without_schedule = without_feed_in["entities"][0]["schedule"]
    with_schedule = with_feed_in["entities"][0]["schedule"]

    assert with_feed_in["projections"]["projected_cost"] < (
        without_feed_in["projections"]["projected_cost"]
    )
    assert with_schedule != without_schedule
    assert _level_increase_count(with_schedule, initial_level=0.0) < (
        _level_increase_count(without_schedule, initial_level=0.0)
    )


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
        optimizer.OptimizationParams(**payload)


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
        optimizer.OptimizationParams(**payload)


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
        optimizer.OptimizationParams(**payload)


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
        optimizer.OptimizationParams(**payload)


def test_optional_energy_curve_is_spread_over_duration_timeslots():
    payload = {
        "grid_import_price_per_kwh": [1.0, 1.0, 1.0, 1.0],
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
        optimizer.OptimizationParams(**payload)


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
        optimizer.OptimizationParams(**payload)


def test_battery_target_deadline_at_least_is_enforced():
    payload = {
        "grid_import_price_per_kwh": [0.1, 0.1, 0.1, 0.5],
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
        "grid_import_price_per_kwh": [0.2, 0.2, 0.2, 0.2],
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
        optimizer.OptimizationParams(**payload)


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
        optimizer.OptimizationParams(**payload)


def test_grid_flag_controls_charging_when_no_pv_surplus_exists():
    # With no PV surplus available, a PV-only battery must not charge,
    # while a GRID-allowed battery should be able to hit its target.
    base_payload = {
        "grid_import_price_per_kwh": [0.2, 0.2, 0.2, 0.2],
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


def test_warm_started_target_remains_reachable_when_horizon_extends_by_one_slot():
    # Extending the warm-started horizon by one slot should not make a
    # previously reachable target become unreachable.
    base_payload = {
        "grid_import_price_per_kwh": [3.0] * 5,
        "solar_input_kwh": [0.0] * 5,
        "usage_kwh": [0.25] * 5,
        "battery_entities": [
            {
                "name": "PVBattery",
                "initial_kwh": 9.15,
                "minimum_kwh": 1.0,
                "capacity_kwh": 10.0,
                "charge_efficiency": 0.9,
                "discharge_efficiency": 0.9,
                "charge_curve_kwh": [1.25],
                "discharge_curve_kwh": [1.25],
                "can_charge_from": 3,
            }
        ],
        "comfort_entities": [],
    }
    warm_state = _run_optimizer(base_payload)["state"]

    result = _run_optimizer(
        {
            **base_payload,
            "battery_entities": [
                {
                    **base_payload["battery_entities"][0],
                    "target": {"timeslot": 4, "soc_kwh": 8.0},
                }
            ],
            "state": warm_state,
        }
    )

    schedule = result["entities"][0]["schedule"]
    assert result["suboptimal"] is False
    assert "battery_target_unmet" not in result["suboptimal_reasons"]
    assert float(schedule[4]["level"]) == pytest.approx(8.0, abs=1e-6)


def test_warm_started_target_uses_early_low_cost_window_before_deadline():
    # With a later deadline and a cheap early charging window, the optimizer
    # should use the low-cost slots to build charge ahead of the target rather
    # than defer into the later expensive period.
    base_payload = {
        "grid_import_price_per_kwh": [0.10, 0.10, 0.80, 0.80, 0.80, 0.80],
        "solar_input_kwh": [0.0] * 6,
        "usage_kwh": [0.25] * 6,
        "battery_entities": [
            {
                "name": "PVBattery",
                "initial_kwh": 1.0,
                "minimum_kwh": 1.0,
                "capacity_kwh": 10.0,
                "charge_efficiency": 0.9,
                "discharge_efficiency": 0.9,
                "charge_curve_kwh": [2.0],
                "discharge_curve_kwh": [2.0],
                "can_charge_from": 1,
            }
        ],
        "comfort_entities": [],
    }
    warm_state = _run_optimizer(base_payload)["state"]

    result = _run_optimizer(
        {
            **base_payload,
            "battery_entities": [
                {
                    **base_payload["battery_entities"][0],
                    "target": {"timeslot": 5, "soc_kwh": 4.5},
                }
            ],
            "state": warm_state,
        }
    )

    schedule = result["entities"][0]["schedule"]
    assert result["suboptimal"] is False
    assert schedule[0]["state"] == "grid_charge"
    assert float(schedule[1]["level"]) > float(schedule[0]["level"])
    assert float(schedule[2]["level"]) >= 4.5
    assert float(schedule[5]["level"]) >= 4.5


def test_pv_flag_controls_charging_when_solar_surplus_exists():
    # With persistent PV surplus, a battery that allows PV should charge,
    # while charging-disabled should stay at its initial level.
    base_payload = {
        "grid_import_price_per_kwh": [0.2, 0.2, 0.2, 0.2],
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
        "grid_import_price_per_kwh": [0.5, 0.5, 0.2, 0.2, 0.2, 0.5, 0.5, 0.5],
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
        "grid_import_price_per_kwh": [2.0] * 8 + [1.0] * 8,
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
        "grid_import_price_per_kwh": [0.2, 0.2, 0.2, 0.2],
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
        "grid_import_price_per_kwh": [1.0, 1.0, 0.1, 0.1],
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


def test_conservative_profile_reduces_marginal_arbitrage():
    base_payload = {
        "grid_import_price_per_kwh": [0.1, 0.1, 0.8, 0.8],
        "grid_export_price_per_kwh": [0.0, 0.0, 0.0, 0.0],
        "solar_input_kwh": [0.0, 0.0, 0.0, 0.0],
        "usage_kwh": [0.0, 0.0, 1.0, 1.0],
        "battery_entities": [
            {
                "name": "b",
                "initial_kwh": 0.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 1.0,
                "charge_curve_kwh": [0.05],
                "discharge_curve_kwh": [0.05],
                "can_charge_from": 1,
            }
        ],
        "comfort_entities": [],
    }

    low_cost = _run_optimizer(base_payload)
    conservative_payload = json.loads(json.dumps(base_payload))
    conservative_payload["throughput_cost_per_kwh"] = 0.08
    conservative_payload["action_deadband_kwh"] = 0.1
    conservative_payload["mode_switch_cost"] = 0.03
    conservative = _run_optimizer(conservative_payload)

    assert low_cost["entities"][0]["schedule"][0]["state"] == "grid_charge"
    assert any(
        point["state"] == "self_consume"
        for point in low_cost["entities"][0]["schedule"]
    )
    assert all(
        point["state"] == "self_consume"
        for point in conservative["entities"][0]["schedule"]
    )


def test_conservative_profile_suppresses_tiny_battery_moves():
    base_payload = {
        "grid_import_price_per_kwh": [0.1, 0.1, 1.0, 1.0],
        "grid_export_price_per_kwh": [0.0, 0.0, 0.0, 0.0],
        "solar_input_kwh": [0.0, 0.0, 0.0, 0.0],
        "usage_kwh": [0.0, 0.0, 0.05, 0.05],
        "battery_entities": [
            {
                "name": "b",
                "initial_kwh": 0.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 1.0,
                "charge_curve_kwh": [0.05],
                "discharge_curve_kwh": [0.05],
                "can_charge_from": 1,
            }
        ],
        "comfort_entities": [],
    }

    no_deadband = _run_optimizer(base_payload)
    conservative_payload = json.loads(json.dumps(base_payload))
    conservative_payload["throughput_cost_per_kwh"] = 0.08
    conservative_payload["action_deadband_kwh"] = 0.1
    conservative_payload["mode_switch_cost"] = 0.03
    with_profile = _run_optimizer(conservative_payload)

    assert no_deadband["entities"][0]["schedule"][0]["state"] == "grid_charge"
    assert any(
        point["state"] == "self_consume"
        for point in no_deadband["entities"][0]["schedule"]
    )
    assert all(
        point["state"] == "self_consume"
        for point in with_profile["entities"][0]["schedule"]
    )


def test_prefer_pv_surplus_charging_sinks_surplus_into_battery():
    base_payload = {
        "grid_import_price_per_kwh": [0.2, 0.2, 0.2, 0.2],
        "grid_export_price_per_kwh": [1.0, 0.8, 0.8, 0.8],
        "solar_input_kwh": [1.0, 0.0, 0.0, 0.0],
        "usage_kwh": [0.0, 0.0, 0.0, 0.0],
        "battery_entities": [
            {
                "name": "ev",
                "initial_kwh": 0.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 1.0,
                "charge_curve_kwh": [1.0],
                "discharge_curve_kwh": [0.0],
                "can_charge_from": 2,
            }
        ],
        "comfort_entities": [],
    }

    baseline = _run_optimizer(base_payload)
    pv_sink_payload = json.loads(json.dumps(base_payload))
    pv_sink_payload["battery_entities"][0]["prefer_pv_surplus_charging"] = True
    pv_sink = _run_optimizer(pv_sink_payload)

    assert baseline["entities"][0]["schedule"][0]["state"] == "self_consume"
    assert baseline["entities"][0]["schedule"][0]["level"] == pytest.approx(0.0)
    assert pv_sink["entities"][0]["schedule"][0]["state"] == "self_consume"
    assert pv_sink["entities"][0]["schedule"][0]["level"] == pytest.approx(1.0)
