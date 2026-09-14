"""No-battery plans account directly for grid flow without invoking HiGHS."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.wattplan.optimizer import OptimizationParams, optimize
from custom_components.wattplan.optimizer import mpc_power_optimizer as core


START = datetime(2026, 9, 14, tzinfo=UTC)


def _payload(*, timed: bool = False, horizon: int = 16) -> dict:
    payload = {
        "grid_import_price_per_kwh": [0.2] * horizon,
        "grid_export_price_per_kwh": [0.05] * horizon,
        "solar_input_kwh": [0.0] * horizon,
        "usage_kwh": [0.3] * horizon,
        "lookahead_slots": min(8, horizon),
        "battery_entities": [],
        "comfort_entities": [],
    }
    if timed:
        payload.update(plan_start=START, slot_minutes=15)
    return payload


@pytest.fixture
def reject_native_solver(monkeypatch):
    def reject(*_args, **_kwargs):
        raise AssertionError("no-battery plans must not invoke HiGHS")

    monkeypatch.setattr(core, "_solve_lp", reject)


def test_signed_tariffs_and_pv_surplus_use_direct_grid_accounting(
    reject_native_solver,
):
    payload = _payload(horizon=4)
    payload.update(
        grid_import_price_per_kwh=[-0.5, 0.4, 0.3, 0.2],
        grid_export_price_per_kwh=[0.1, -0.2, 0.5, 0.0],
        solar_input_kwh=[0.0, 2.0, 1.0, 0.0],
        usage_kwh=[1.0, 0.5, 2.0, 0.0],
    )

    result = optimize(OptimizationParams(**payload))

    assert result["successful_solves"] == 0
    assert result["entities"] == []
    assert result["projections"]["projected_cost"] == pytest.approx(0.1)
    assert [
        row["projected_cost"] for row in result["projections"]["per_slot"]
    ] == pytest.approx([-0.5, 0.3, 0.3, 0.0])


def test_comfort_and_optional_loads_keep_shared_accounting(reject_native_solver):
    payload = _payload(horizon=4)
    payload.update(
        grid_import_price_per_kwh=[0.5, 0.2, 0.3, 0.3],
        grid_export_price_per_kwh=[0.1, 0.0, 0.0, 0.0],
        solar_input_kwh=[1.0, 0.0, 0.0, 0.0],
        usage_kwh=[0.0, 0.0, 0.0, 0.0],
        rolling_window_slots=4,
        comfort_entities=[
            {
                "name": "heat",
                "target_on_slots_per_rolling_window": 4,
                "max_consecutive_off_slots": 1,
                "power_usage_kwh": 1.0,
                "is_on_now": True,
                "on_history": [True, True, True],
                "off_streak_slots_now": 0,
            }
        ],
        optional_entities=[
            {
                "name": "washer",
                "duration_timeslots": 1,
                "start_before_timeslot": 3,
                "energy_kwh": 1.0,
                "options": 3,
                "allow_overlapping_options": True,
            }
        ],
    )

    result = optimize(OptimizationParams(**payload))

    assert result["successful_solves"] == 0
    assert all(point["enabled"] for point in result["entities"][0]["schedule"])
    assert result["projections"]["projected_cost"] == pytest.approx(0.8)
    options = result["optional_entity_options"][0]["options"]
    assert [option["start_timeslot"] for option in options] == [1, 2, 0]
    assert [option["incremental_cost"] for option in options] == pytest.approx(
        [0.2, 0.3, 0.5]
    )


def test_untimed_state_roundtrip_reuses_direct_plan_without_solver(
    reject_native_solver,
):
    payload = _payload()
    first = optimize(OptimizationParams(**payload))
    payload["state"] = first["state"]
    repeated = optimize(OptimizationParams(**payload))

    assert first["successful_solves"] == repeated["successful_solves"] == 0
    assert repeated["reused_steps"] == 16
    assert repeated["entities"] == first["entities"]
    assert repeated["projections"] == first["projections"]
    assert repeated["state"] == first["state"]


def test_timed_prefix_transitions_preserve_full_accounting_without_solver(
    reject_native_solver,
):
    payload = _payload(timed=True)
    result = optimize(OptimizationParams(**payload))
    modes = [result["cadence"]["mode"]]

    for tick in range(1, 5):
        payload["state"] = result["state"]
        payload["plan_start"] = START + timedelta(minutes=15 * tick)
        result = optimize(OptimizationParams(**payload))
        modes.append(result["cadence"]["mode"])
        assert result["successful_solves"] == 0
        assert len(result["projections"]["per_slot"]) == 16
        assert result["projections"]["projected_cost"] == pytest.approx(0.96)

    assert modes == ["full", "repair", "repair", "repair", "full"]
