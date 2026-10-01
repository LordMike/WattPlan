"""Accepted physical energy-flow API tests."""

import base64
import json
from datetime import UTC, datetime

import pytest

from custom_components.wattplan.optimizer import OptimizationParams, optimize


def _accepted_state(result: dict) -> dict:
    return json.loads(base64.urlsafe_b64decode(result["state"]))


def test_energy_flows_are_per_slot_and_conserve_site_balance() -> None:
    params = OptimizationParams(
        plan_start=datetime(2026, 9, 23, tzinfo=UTC),
        slot_minutes=15,
        grid_import_price_per_kwh=[0.2] * 8,
        grid_export_price_per_kwh=[0.1] * 8,
        solar_input_kwh=[0.0, 1.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0],
        usage_kwh=[0.5] * 8,
        battery_entities=[],
        comfort_entities=[],
    )

    result = optimize(params)
    flows = result["energy_flows"]["per_slot"]

    assert len(flows) == 8
    for flow, solar in zip(flows, params.solar_input_kwh, strict=True):
        assert flow["grid_import_kwh"] >= 0
        assert flow["grid_export_kwh"] >= 0
        assert flow["grid_import_kwh"] * flow["grid_export_kwh"] == pytest.approx(0)
        assert flow["grid_import_kwh"] - flow["grid_export_kwh"] == pytest.approx(
            flow["load_kwh"] - solar
        )
        assert flow["base_load_kwh"] == pytest.approx(0.5)


def test_energy_flows_include_accepted_comfort_schedule_without_extra_solve() -> None:
    params = OptimizationParams(
        plan_start=datetime(2026, 9, 23, tzinfo=UTC),
        slot_minutes=60,
        grid_import_price_per_kwh=[10.0, 9.0, 1.0, 10.0],
        grid_export_price_per_kwh=[0.0] * 4,
        solar_input_kwh=[0.0] * 4,
        usage_kwh=[0.25] * 4,
        rolling_window_slots=3,
        lookahead_slots=4,
        battery_entities=[],
        comfort_entities=[{
            "name": "water heating",
            "target_on_slots_per_rolling_window": 1,
            "min_consecutive_on_slots": 1,
            "min_consecutive_off_slots": 1,
            "max_consecutive_off_slots": 4,
            "power_usage_kwh": 1.0,
            "is_on_now": False,
            "on_slots_last_rolling_window": 0,
            "on_history": [False, False],
            "off_streak_slots_now": 2,
        }],
    )

    result = optimize(params)
    schedule = next(
        entity["schedule"]
        for entity in result["entities"]
        if entity["type"] == "comfort"
    )
    flows = result["energy_flows"]["per_slot"]

    assert len(flows) == len(schedule) == 4
    assert result["successful_solves"] == 0
    for point, flow in zip(schedule, flows, strict=True):
        expected_comfort = 1.0 if point["enabled"] else 0.0
        assert flow["comfort_load_kwh"] == pytest.approx(expected_comfort)
        assert flow["load_kwh"] == pytest.approx(0.25 + expected_comfort)
        assert flow["grid_import_kwh"] == pytest.approx(flow["load_kwh"])


def test_energy_flows_include_accepted_grid_battery_charging() -> None:
    params = OptimizationParams(
        plan_start=datetime(2026, 9, 23, tzinfo=UTC),
        slot_minutes=60,
        grid_import_price_per_kwh=[0.1, 0.1, 1.0, 1.0],
        grid_export_price_per_kwh=[0.0] * 4,
        solar_input_kwh=[0.0] * 4,
        usage_kwh=[0.0, 0.0, 1.0, 1.0],
        battery_entities=[{
            "name": "battery",
            "initial_kwh": 0.0,
            "minimum_kwh": 0.0,
            "capacity_kwh": 2.0,
            "charge_curve_kwh": [1.0],
            "discharge_curve_kwh": [1.0],
            "can_charge_from": 1,
        }],
        comfort_entities=[],
    )

    result = optimize(params)
    flows = result["energy_flows"]["per_slot"]
    state = _accepted_state(result)

    assert [flow["grid_charge_kwh"] for flow in flows] == pytest.approx([1, 1, 0, 0])
    assert state["battery_charge_grid"][0] == pytest.approx([1, 1, 0, 0])
    assert state["battery_charge"][0] == pytest.approx([1, 1, 0, 0])
    assert state["battery_discharge"][0] == pytest.approx([0, 0, 1, 1], abs=2e-6)
    for flow, solar in zip(flows, params.solar_input_kwh, strict=True):
        assert flow["grid_import_kwh"] * flow["grid_export_kwh"] == pytest.approx(0)
        assert flow["grid_import_kwh"] - flow["grid_export_kwh"] == pytest.approx(
            flow["load_kwh"] - solar
        )


def test_energy_flows_include_accepted_pv_charge_and_battery_discharge() -> None:
    params = OptimizationParams(
        plan_start=datetime(2026, 9, 23, tzinfo=UTC),
        slot_minutes=60,
        grid_import_price_per_kwh=[0.4, 0.2, 1.0, 1.0],
        grid_export_price_per_kwh=[0.0] * 4,
        solar_input_kwh=[0.0, 2.0, 0.0, 0.0],
        usage_kwh=[1.0, 0.0, 1.0, 1.0],
        battery_entities=[{
            "name": "battery",
            "initial_kwh": 1.0,
            "minimum_kwh": 0.0,
            "capacity_kwh": 2.0,
            "charge_curve_kwh": [1.0],
            "discharge_curve_kwh": [1.0],
            "can_charge_from": 2,
        }],
        comfort_entities=[],
    )

    result = optimize(params)
    flows = result["energy_flows"]["per_slot"]
    state = _accepted_state(result)

    assert state["battery_charge_pv"][0] == pytest.approx([0, 1, 0, 0])
    assert state["battery_charge_grid"][0] == pytest.approx([0, 0, 0, 0])
    assert state["battery_discharge"][0] == pytest.approx([0, 0, 1, 1])
    assert flows[1]["load_kwh"] == pytest.approx(1)
    assert flows[1]["grid_export_kwh"] == pytest.approx(1)
    assert flows[1]["grid_charge_kwh"] == pytest.approx(0)
    for flow, solar in zip(flows, params.solar_input_kwh, strict=True):
        assert flow["grid_import_kwh"] * flow["grid_export_kwh"] == pytest.approx(0)
        assert flow["grid_import_kwh"] - flow["grid_export_kwh"] == pytest.approx(
            flow["load_kwh"] - solar
        )
