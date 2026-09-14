from datetime import UTC, datetime, timedelta

import pytest

from scripts import benchmark_optimizer as benchmark
from tests.optimizer.benchmark_cases import CASE_METADATA, build_case


def _payload():
    return {
        "plan_start": datetime(2026, 9, 1, tzinfo=UTC),
        "slot_minutes": 30,
        "grid_import_price_per_kwh": [1.0, 2.0, 3.0],
        "grid_export_price_per_kwh": [0.1, 0.2, 0.3],
        "solar_input_kwh": [0.0, 1.0, 2.0],
        "usage_kwh": [2.0, 1.0, 0.0],
        "battery_entities": [
            {
                "name": "first",
                "initial_kwh": 1.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 3.0,
                "charge_curve_kwh": [1.0],
                "discharge_curve_kwh": [1.0],
                "target": {"timeslot": 1, "soc_kwh": 2.0},
            },
            {
                "name": "second",
                "initial_kwh": 2.0,
                "minimum_kwh": 0.0,
                "capacity_kwh": 3.0,
                "charge_curve_kwh": [1.0],
                "discharge_curve_kwh": [1.0],
            },
        ],
        "comfort_entities": [],
        "optional_entities": [
            {
                "name": "dryer",
                "duration_timeslots": 1,
                "start_after_timeslot": 1,
                "start_before_timeslot": 3,
                "energy_kwh": 1.0,
            }
        ],
    }


def _previous_result():
    return {
        "entities": [
            {"name": "heater", "type": "comfort", "schedule": []},
            {
                "name": "second",
                "type": "battery",
                "schedule": [{"level": 2.5}],
            },
            {
                "name": "first",
                "type": "battery",
                "schedule": [{"level": 1.5}],
            },
        ]
    }


def test_shifted_trajectory_uses_slot_duration_applied_soc_and_absolute_target():
    payload = _payload()
    shifted = benchmark._shifted(payload, 1, "opaque-state", _previous_result())

    assert shifted["plan_start"] == payload["plan_start"] + timedelta(minutes=30)
    assert shifted["state"] == "opaque-state"
    assert shifted["grid_import_price_per_kwh"] == [2.0, 3.0, 1.0]
    assert shifted["battery_entities"][0]["initial_kwh"] == pytest.approx(1.5)
    assert shifted["battery_entities"][1]["initial_kwh"] == pytest.approx(2.5)
    assert shifted["battery_entities"][0]["target"]["timeslot"] == 0
    assert shifted["optional_entities"][0]["start_after_timeslot"] == 0
    assert shifted["optional_entities"][0]["start_before_timeslot"] == 2
    assert payload["battery_entities"][0]["target"]["timeslot"] == 1


def test_shifted_trajectory_drops_expired_target():
    shifted = benchmark._shifted(_payload(), 3, "opaque-state", _previous_result())

    assert "target" not in shifted["battery_entities"][0]
    assert shifted["optional_entities"] == []


def test_serial_summary_separates_full_and_prefix_distributions():
    trajectories = [
        [
            {"cadence": "full", "seconds": 3.0, "quality": {"valid": True}},
            {"cadence": "repair", "seconds": 1.0, "quality": {"valid": True}},
            {"cadence": "repair", "seconds": 2.0, "quality": {"valid": True}},
            {
                "cadence": "fallback_full",
                "seconds": 4.0,
                "quality": {"valid": True},
            },
        ],
        [
            {"cadence": "full", "seconds": 5.0, "quality": {"valid": True}},
            {"cadence": "repair", "seconds": 3.0, "quality": {"valid": True}},
        ],
    ]

    summary = benchmark._serial_summary(trajectories)

    assert summary["full"]["samples_seconds"] == [3.0, 4.0, 5.0]
    assert summary["full"]["median_seconds"] == pytest.approx(4.0)
    assert summary["prefix"]["samples_seconds"] == [1.0, 2.0, 3.0]
    assert summary["prefix"]["median_seconds"] == pytest.approx(2.0)
    assert summary["cadence_counts"] == {
        "full": 2,
        "repair": 3,
        "fallback_full": 1,
    }


def test_preserve_probe_scenario_records_counterfactual_solver_calls():
    report = benchmark._measure(
        benchmark._preserve_probe_payload(4, 4),
        repeats=1,
    )

    assert report["solver"]["probe_calls"] > 0
    assert report["solver"]["primary_calls"] == report["primary_solves"]
    assert report["solver"]["submitted_starts"] == 0
    assert (
        report["solver"]["total_wrapper_seconds"]
        >= report["solver"]["total_highs_seconds"]
    )
    assert report["solver"]["max_probe_model"]["variables"] > 0
    assert report["timing"]["end_to_end_seconds"]["median_seconds"] > 0


def test_solver_summary_supports_zero_native_calls():
    report = benchmark._solver_summary([], [])

    assert report["total_calls"] == 0
    assert report["primary_calls"] == 0
    assert report["probe_calls"] == 0
    assert report["total_wrapper_seconds"] == 0
    assert report["total_highs_seconds"] == 0
    assert report["model_construction_seconds"] == 0
    assert report["max_model"] == {
        "variables": 0,
        "integer_variables": 0,
        "rows": 0,
        "nonzeros": 0,
    }


def test_session_one_cases_cover_required_asset_shapes():
    assert set(CASE_METADATA) == {
        "no-assets-no-pv",
        "no-battery-comfort-no-pv",
        "no-battery-comfort-pv",
        "battery-zero-pv",
        "battery-zero-pv-signed-target",
        "charge-only-battery",
        "mixed-batteries-pv",
        "comfort-flexible",
        "comfort-tight",
    }

    no_assets = build_case("no-assets-no-pv", slots=12, lookahead=8)
    assert no_assets["battery_entities"] == []
    assert no_assets["comfort_entities"] == []
    assert no_assets["solar_input_kwh"] == [0.0] * 12

    zero_pv = build_case("battery-zero-pv", slots=12, lookahead=8)
    assert len(zero_pv["battery_entities"]) == 1
    assert zero_pv["solar_input_kwh"] == [0.0] * 12

    signed_target = build_case(
        "battery-zero-pv-signed-target", slots=48, lookahead=8
    )
    assert signed_target["solar_input_kwh"] == [0.0] * 48
    assert min(signed_target["grid_import_price_per_kwh"]) < 0.0
    assert min(signed_target["grid_export_price_per_kwh"]) < 0.0
    assert max(signed_target["grid_export_price_per_kwh"]) > 0.0
    assert signed_target["action_deadband_kwh"] > 0.0
    assert signed_target["battery_entities"][0]["target"]["mode"] == "at_least"

    charge_only = build_case("charge-only-battery", slots=12, lookahead=8)
    assert charge_only["battery_entities"][0]["discharge_curve_kwh"] == [0.0]
    assert charge_only["battery_entities"][0]["can_charge_from"] == 1

    mixed = build_case("mixed-batteries-pv", slots=48, lookahead=8)
    assert len(mixed["battery_entities"]) == 2
    assert any(mixed["solar_input_kwh"])
    assert CASE_METADATA["comfort-tight"]["provenance"] == (
        "synthetic-historical-substitute"
    )
    for name in CASE_METADATA:
        benchmark.OptimizationParams(**build_case(name, slots=48, lookahead=8))


def test_serial_trajectory_records_solver_breakdown_per_tick():
    trajectory = benchmark._serial_trajectory(
        build_case("no-assets-no-pv", slots=4, lookahead=4)
    )

    assert len(trajectory) == 5
    assert all("timing" in row and "solver" in row for row in trajectory)
    assert all(
        row["solver"]["primary_calls"] == row["primary_solves"]
        for row in trajectory
    )
    assert all(row["solver"]["total_calls"] == 0 for row in trajectory)
    assert all(row["solver"]["probe_calls"] == 0 for row in trajectory)


def test_real_no_battery_measurement_reports_zero_solver_calls():
    report = benchmark._measure(
        build_case("no-battery-comfort-pv", slots=12, lookahead=8), repeats=1
    )

    assert report["primary_solves"] == 0
    assert report["solver"]["total_calls"] == 0
    assert report["solver"]["primary_calls"] == 0
    assert report["solver"]["probe_calls"] == 0
    assert report["solver"]["max_model"]["variables"] == 0
    assert report["quality_valid"]


def test_git_state_is_best_effort(monkeypatch):
    class Failed:
        returncode = 128
        stdout = ""

    monkeypatch.setattr(benchmark.subprocess, "run", lambda *args, **kwargs: Failed())

    assert benchmark._git_state() == (None, None)


def test_paired_full_alternates_order_and_aggregates_raw_results(monkeypatch):
    calls = []

    def measured(_payload, repeats, *, disable_mip_starts=False, **_kwargs):
        assert repeats == 1
        label = "cold" if disable_mip_starts else "warm"
        calls.append(label)
        sample = float(len(calls))
        cost = 10.0 if label == "warm" else 10.0
        solver = benchmark._solver_summary(
            [
                {
                    "role": "primary",
                    "wrapper_seconds": sample / 4,
                    "native_seconds": sample / 8,
                    "nodes": 1,
                    "start_submitted": label == "warm" and index < 3,
                    "start_accepted": label == "warm" and index < 3,
                    "start_status": (
                        "HighsStatus.kOk"
                        if label == "warm" and index < 3
                        else None
                    ),
                    "variables": 10,
                    "integer_variables": 5,
                    "rows": 8,
                    "nonzeros": 20,
                }
                for index in range(4)
            ],
            [
                {
                    "role": "primary",
                    "base_timeslot": index,
                    "horizon": 4 - index,
                    "step_seconds": sample / 4,
                    "model_construction_seconds": 0.0,
                    "native_calls": 1,
                }
                for index in range(4)
            ],
        )
        timing = {
            "end_to_end_seconds": sample,
            "parameter_validation_seconds": 0.0,
            "normalization_seconds": 0.0,
            "planner_seconds": sample,
        }
        return {
            "median_seconds": sample,
            "samples_seconds": [sample],
            "primary_solves": 4,
            "projected_cost": cost,
            "projected_costs": [cost],
            "quality_valid": True,
            "quality": [{"valid": True}],
            "timing": benchmark._timing_summary([timing], [solver]),
            "runs": [{"timing": timing, "solver": solver}],
            "solver": solver,
        }

    monkeypatch.setattr(benchmark, "_measure", measured)
    report = benchmark._paired_full({}, repeats=3)

    assert calls == ["warm", "cold", "cold", "warm", "warm", "cold"]
    assert report["warm"]["samples_seconds"] == [1.0, 4.0, 5.0]
    assert report["cold"]["samples_seconds"] == [2.0, 3.0, 6.0]
    assert report["warm"]["solver"]["submitted_starts"] == 9
    assert report["cold"]["solver"]["submitted_starts"] == 0
    assert [pair["execution_order"] for pair in report["pairs"]] == [
        ["warm", "cold"],
        ["cold", "warm"],
        ["warm", "cold"],
    ]
