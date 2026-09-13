from datetime import UTC, datetime, timedelta

import pytest

from scripts import benchmark_optimizer as benchmark


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
    assert report["solver"]["submitted_starts"] > 0


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
        return {
            "median_seconds": sample,
            "samples_seconds": [sample],
            "primary_solves": 4,
            "projected_cost": cost,
            "projected_costs": [cost],
            "quality_valid": True,
            "quality": [{"valid": True}],
            "solver": {
                "total_calls": 4,
                "probe_calls": 0,
                "submitted_starts": 3 if label == "warm" else 0,
                "successful_start_submissions": 3 if label == "warm" else 0,
                "total_wrapper_seconds": sample,
                "total_highs_seconds": sample / 2,
                "total_nodes": 1,
                "max_model": {
                    "variables": 10,
                    "integer_variables": 5,
                    "rows": 8,
                    "nonzeros": 20,
                },
            },
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
