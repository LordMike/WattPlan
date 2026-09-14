#!/usr/bin/env python3
"""Benchmark full and prefix WattPlan optimization with representative inputs."""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import highspy
import numpy as np

HARNESS_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(
    os.environ.get("WATTPLAN_BENCHMARK_PRODUCTION_ROOT", HARNESS_ROOT)
).resolve()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from custom_components.wattplan.optimizer import OptimizationParams, optimize
from custom_components.wattplan.optimizer import mpc_power_optimizer as core
_cases_spec = importlib.util.spec_from_file_location(
    "wattplan_benchmark_cases",
    HARNESS_ROOT / "tests" / "optimizer" / "benchmark_cases.py",
)
if _cases_spec is None or _cases_spec.loader is None:
    raise RuntimeError("unable to load benchmark cases")
_cases = importlib.util.module_from_spec(_cases_spec)
_cases_spec.loader.exec_module(_cases)
CASE_METADATA = _cases.CASE_METADATA
build_case = _cases.build_case
from tests.optimizer.test_optimizer_scenarios import (
    _live_exported_deye_low_pv_low_soc_payload,
    _live_grid_export_benchmark_payload,
)


def _git_state():
    revision_result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if revision_result.returncode != 0:
        return None, None
    dirty_result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    return revision_result.stdout.strip(), (
        bool(dirty_result.stdout) if dirty_result.returncode == 0 else None
    )


def _extend(payload, slots):
    result = copy.deepcopy(payload)
    for key in (
        "grid_import_price_per_kwh",
        "grid_export_price_per_kwh",
        "solar_input_kwh",
        "usage_kwh",
    ):
        values = result.get(key)
        if values:
            repeats = (slots + len(values) - 1) // len(values)
            result[key] = (values * repeats)[:slots]
    for battery in result.get("battery_entities", []):
        target = battery.get("target")
        if target is not None and int(target["timeslot"]) >= slots:
            target["timeslot"] = slots - 1
    optional_entities = []
    for entity in result.get("optional_entities", []):
        end = min(int(entity["start_before_timeslot"]), slots)
        if end - int(entity["duration_timeslots"]) < int(
            entity.get("start_after_timeslot", 0)
        ):
            continue
        entity["start_before_timeslot"] = end
        optional_entities.append(entity)
    result["optional_entities"] = optional_entities
    result["lookahead_slots"] = min(48, slots)
    result["plan_start"] = datetime(2026, 3, 9, tzinfo=UTC)
    return result


def _stress_payload(slots, lookahead):
    prices = []
    exports = []
    solar = []
    usage = []
    for t in range(slots):
        day = t % 96
        wave = math.sin((day - 20) * math.pi / 48)
        prices.append(
            0.35
            + 0.55 * wave
            + (2.2 if 64 <= day < 84 else 0.0)
            + (0.18 if t % 7 == 0 else 0.0)
        )
        exports.append(-0.25 + 0.8 * wave + (0.9 if 60 <= day < 76 else 0.0))
        solar.append(max(0.0, 2.4 * math.sin((day - 20) * math.pi / 56)))
        usage.append(
            0.25
            + (1.5 if 68 <= day < 88 else 0.0)
            + (0.7 if t % 13 == 0 else 0.0)
        )
    return {
        "plan_start": datetime(2026, 9, 13, tzinfo=UTC),
        "slot_minutes": 15,
        "grid_import_price_per_kwh": prices,
        "grid_export_price_per_kwh": exports,
        "solar_input_kwh": solar,
        "usage_kwh": usage,
        "lookahead_slots": lookahead,
        "throughput_cost_per_kwh": 0.015,
        "action_deadband_kwh": 0.04,
        "mode_switch_cost": 0.01,
        "battery_entities": [
            {
                "name": "house-a",
                "initial_kwh": 4.2,
                "minimum_kwh": 0.8,
                "capacity_kwh": 13.5,
                "charge_curve_kwh": [3.0, 2.5, 1.5, 0.6],
                "discharge_curve_kwh": [3.2, 2.7, 1.7, 0.7],
                "charge_efficiency": 0.92,
                "discharge_efficiency": 0.91,
                "can_charge_from": 3,
                "prefer_pv_surplus_charging": True,
                "target": {
                    "timeslot": slots - 12,
                    "soc_kwh": 7.0,
                    "mode": "at_least",
                },
            },
            {
                "name": "house-b",
                "initial_kwh": 2.0,
                "minimum_kwh": 0.5,
                "capacity_kwh": 8.0,
                "charge_curve_kwh": [1.8, 1.4, 0.8],
                "discharge_curve_kwh": [2.0, 1.5, 0.9],
                "charge_efficiency": 0.88,
                "discharge_efficiency": 0.9,
                "can_charge_from": 2,
                "prefer_pv_surplus_charging": True,
            },
            {
                "name": "vehicle",
                "initial_kwh": 9.0,
                "minimum_kwh": 5.0,
                "capacity_kwh": 42.0,
                "charge_curve_kwh": [2.8, 2.0, 1.2],
                "discharge_curve_kwh": [1.5, 1.0, 0.5],
                "charge_efficiency": 0.94,
                "discharge_efficiency": 0.93,
                "can_charge_from": 1,
                "target": {
                    "timeslot": slots - 20,
                    "soc_kwh": 28.0,
                    "mode": "at_least",
                },
            },
        ],
        "comfort_entities": [],
    }


def _preserve_probe_payload(slots, lookahead):
    prices = ([0.10, 1.00, 1.00, 1.00] * ((slots + 3) // 4))[:slots]
    return {
        "plan_start": datetime(2026, 9, 13, tzinfo=UTC),
        "slot_minutes": 15,
        "grid_import_price_per_kwh": prices,
        "grid_export_price_per_kwh": [0.0] * slots,
        "solar_input_kwh": [0.0] * slots,
        "usage_kwh": [1.0] * slots,
        "lookahead_slots": lookahead,
        "action_deadband_kwh": 0.005,
        "battery_entities": [
            {
                "name": "preserve-probe-battery",
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


def _scenario(name, slots, lookahead):
    if name in CASE_METADATA:
        return build_case(name, slots, lookahead)
    if name == "live-export":
        payload, export_prices = _live_grid_export_benchmark_payload()
        payload["grid_export_price_per_kwh"] = export_prices
        result = _extend(payload, slots)
    elif name == "low-pv":
        result = _extend(_live_exported_deye_low_pv_low_soc_payload(), slots)
    elif name == "preserve-probe":
        return _preserve_probe_payload(slots, lookahead)
    else:
        return _stress_payload(slots, lookahead)
    result["lookahead_slots"] = lookahead
    return result


class _SolverRecorder:
    """Capture solver roles, wrapper/native time, and model construction cost."""

    def __init__(self):
        self.native_calls = []
        self.step_calls = []
        self._role = None

    def __enter__(self):
        self._original_solve = core._solve_lp
        self._original_step = core._solve_mpc_step

        def measured_solve(*args, **kwargs):
            start = time.perf_counter()
            solved = self._original_solve(*args, **kwargs)
            start_status = solved.mip_start_status
            self.native_calls.append(
                {
                    "role": self._role or "unclassified",
                    "wrapper_seconds": time.perf_counter() - start,
                    "native_seconds": solved.solver_runtime,
                    "nodes": solved.mip_node_count,
                    "start_submitted": start_status is not None,
                    "start_accepted": start_status == highspy.HighsStatus.kOk,
                    "start_status": None if start_status is None else str(start_status),
                    "variables": solved.num_variables,
                    "integer_variables": solved.num_integer_variables,
                    "rows": solved.num_rows,
                    "nonzeros": solved.num_nonzeros,
                }
            )
            return solved

        def measured_step(*args, **kwargs):
            role = "probe" if kwargs.get("forced_discharge_first") else "primary"
            previous_role = self._role
            self._role = role
            native_start = len(self.native_calls)
            start = time.perf_counter()
            try:
                return self._original_step(*args, **kwargs)
            finally:
                elapsed = time.perf_counter() - start
                native_calls = self.native_calls[native_start:]
                native_wrapper = sum(row["wrapper_seconds"] for row in native_calls)
                self.step_calls.append(
                    {
                        "role": role,
                        "base_timeslot": kwargs.get("base_timeslot"),
                        "horizon": len(kwargs.get("prices_h", [])),
                        "step_seconds": elapsed,
                        "model_construction_seconds": max(elapsed - native_wrapper, 0.0),
                        "native_calls": len(native_calls),
                    }
                )
                self._role = previous_role

        core._solve_lp = measured_solve
        core._solve_mpc_step = measured_step
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        core._solve_lp = self._original_solve
        core._solve_mpc_step = self._original_step

    def summary(self):
        return _solver_summary(self.native_calls, self.step_calls)


def _empty_model_size():
    return {
        "variables": 0,
        "integer_variables": 0,
        "rows": 0,
        "nonzeros": 0,
    }


def _max_model(rows):
    if not rows:
        return _empty_model_size()
    return {
        key: max(row[key] for row in rows)
        for key in ("variables", "integer_variables", "rows", "nonzeros")
    }


def _solver_summary(native_calls, step_calls):
    started = [row for row in native_calls if row["start_submitted"]]
    primary = [row for row in native_calls if row["role"] == "primary"]
    probes = [row for row in native_calls if row["role"] == "probe"]
    wrapper_seconds = sum(row["wrapper_seconds"] for row in native_calls)
    native_seconds = sum(row["native_seconds"] for row in native_calls)
    return {
        "total_calls": len(native_calls),
        "primary_calls": len(primary),
        "probe_calls": len(probes),
        "unclassified_calls": len(native_calls) - len(primary) - len(probes),
        "submitted_starts": len(started),
        "successful_start_submissions": sum(row["start_accepted"] for row in started),
        "total_wrapper_seconds": wrapper_seconds,
        "total_highs_seconds": native_seconds,
        "wrapper_overhead_seconds": max(wrapper_seconds - native_seconds, 0.0),
        "model_construction_seconds": sum(
            row["model_construction_seconds"] for row in step_calls
        ),
        "total_step_seconds": sum(row["step_seconds"] for row in step_calls),
        "total_nodes": sum(row["nodes"] for row in native_calls),
        "max_model": _max_model(native_calls),
        "max_primary_model": _max_model(primary),
        "max_probe_model": _max_model(probes),
        "calls": native_calls,
        "steps": step_calls,
    }


def _run(payload, *, force_full, disable_mip_starts=False):
    end_to_end_start = time.perf_counter()
    params_start = time.perf_counter()
    params = OptimizationParams(**payload)
    params_seconds = time.perf_counter() - params_start
    original = core._use_mip_starts
    if disable_mip_starts:
        core._use_mip_starts = lambda _entities, _lookahead: False
    try:
        if force_full:
            normalization_start = time.perf_counter()
            normalized = core.normalize_calculation_input(params)
            normalization_seconds = time.perf_counter() - normalization_start
            planner_start = time.perf_counter()
            result = core.optimize_internal(normalized, reuse_plan_override=None)
        else:
            normalization_seconds = None
            planner_start = time.perf_counter()
            result = optimize(params)
        planner_seconds = time.perf_counter() - planner_start
        return result, {
            "end_to_end_seconds": time.perf_counter() - end_to_end_start,
            "parameter_validation_seconds": params_seconds,
            "normalization_seconds": normalization_seconds,
            "planner_seconds": planner_seconds,
        }
    finally:
        core._use_mip_starts = original


def _result_quality(payload, result):
    schedules = {
        entity["name"]: entity["schedule"]
        for entity in result["entities"]
        if entity["type"] == "battery"
    }
    bounds_valid = True
    targets_valid = True
    for battery in payload.get("battery_entities", []):
        schedule = schedules[battery["name"]]
        minimum = float(battery["minimum_kwh"])
        capacity = float(battery["capacity_kwh"])
        bounds_valid = bounds_valid and all(
            minimum - core.EPSILON <= float(point["level"]) <= capacity + core.EPSILON
            for point in schedule
        )
        target = battery.get("target")
        if target is None:
            continue
        level = float(schedule[int(target["timeslot"])]["level"])
        value = float(target["soc_kwh"])
        tolerance = float(target.get("tolerance_kwh", 0.0))
        mode = target.get("mode", "at_least")
        if mode in ("at_least", "exact"):
            targets_valid = targets_valid and level >= value - tolerance - core.EPSILON
        if mode in ("at_most", "exact"):
            targets_valid = targets_valid and level <= value + tolerance + core.EPSILON
    cost_valid = math.isfinite(float(result["projections"]["projected_cost"]))
    reasons = list(result["suboptimal_reasons"])
    target_levels = {}
    for battery in payload.get("battery_entities", []):
        target = battery.get("target")
        if target is not None:
            target_levels[battery["name"]] = float(
                schedules[battery["name"]][int(target["timeslot"])]["level"]
            )
    return {
        "valid": cost_valid and bounds_valid and targets_valid and not reasons,
        "finite_projected_cost": cost_valid,
        "battery_bounds_valid": bounds_valid,
        "targets_valid": targets_valid,
        "target_levels_kwh": target_levels,
        "suboptimal_reasons": reasons,
    }


def _placement_report(result):
    placement = result.get("comfort_placement")
    if placement is None:
        return None
    return copy.deepcopy(placement)


def _measure(payload, repeats, *, force_full=True, disable_mip_starts=False):
    timings = []
    projected_costs = []
    qualities = []
    solver_reports = []
    placement_reports = []
    result = None

    for _ in range(repeats):
        with _SolverRecorder() as recorder:
            result, run_timing = _run(
                payload,
                force_full=force_full,
                disable_mip_starts=disable_mip_starts,
            )
        timings.append(run_timing)
        solver_reports.append(recorder.summary())
        projected_costs.append(result["projections"]["projected_cost"])
        qualities.append(_result_quality(payload, result))
        placement_reports.append(_placement_report(result))

    samples = [row["end_to_end_seconds"] for row in timings]
    solver = _aggregate_solver_reports(solver_reports)
    report = {
        "median_seconds": statistics.median(samples),
        "samples_seconds": samples,
        "primary_solves": result["successful_solves"],
        "projected_cost": projected_costs[-1],
        "projected_costs": projected_costs,
        "quality_valid": all(quality["valid"] for quality in qualities),
        "quality": qualities,
        "comfort_placement": placement_reports,
        "timing": _timing_summary(timings, solver_reports),
        "runs": [
            {
                "timing": timing,
                "solver": solver_report,
                "comfort_placement": placement,
            }
            for timing, solver_report, placement in zip(
                timings, solver_reports, placement_reports
            )
        ],
        "solver": solver,
    }
    return report


def _timing_summary(timings, solver_reports):
    keys = (
        "end_to_end_seconds",
        "parameter_validation_seconds",
        "normalization_seconds",
        "planner_seconds",
    )
    summary = {}
    for key in keys:
        values = [row[key] for row in timings if row[key] is not None]
        summary[key] = _sample_summary(values)
    non_solver = []
    for timing, solver in zip(timings, solver_reports):
        non_solver.append(
            max(timing["planner_seconds"] - solver["total_step_seconds"], 0.0)
        )
    summary["planner_outside_solver_steps_seconds"] = _sample_summary(non_solver)
    return summary


def _aggregate_solver_reports(reports):
    native_calls = [call for report in reports for call in report["calls"]]
    step_calls = [step for report in reports for step in report["steps"]]
    return _solver_summary(native_calls, step_calls)


def _aggregate_measurements(reports):
    samples = [sample for report in reports for sample in report["samples_seconds"]]
    costs = [cost for report in reports for cost in report["projected_costs"]]
    qualities = [quality for report in reports for quality in report["quality"]]
    placements = [
        placement
        for report in reports
        for placement in report.get("comfort_placement", [])
    ]
    runs = [run for report in reports for run in report["runs"]]
    timings = [run["timing"] for run in runs]
    solver_rows = [run["solver"] for run in runs]
    return {
        "median_seconds": statistics.median(samples),
        "samples_seconds": samples,
        "primary_solves": reports[-1]["primary_solves"],
        "projected_cost": costs[-1],
        "projected_costs": costs,
        "quality_valid": all(quality["valid"] for quality in qualities),
        "quality": qualities,
        "comfort_placement": placements,
        "timing": _timing_summary(timings, solver_rows),
        "runs": runs,
        "solver": _aggregate_solver_reports(solver_rows),
    }


def _paired_full(payload, repeats):
    reports = {"warm": [], "cold": []}
    pairs = []
    for repeat in range(repeats):
        labels = ("warm", "cold") if repeat % 2 == 0 else ("cold", "warm")
        current = {}
        for label in labels:
            report = _measure(
                payload,
                1,
                disable_mip_starts=label == "cold",
            )
            reports[label].append(report)
            current[label] = report
        pairs.append(
            {
                "execution_order": list(labels),
                "warm_seconds": current["warm"]["samples_seconds"][0],
                "cold_seconds": current["cold"]["samples_seconds"][0],
                "warm_minus_cold_seconds": (
                    current["warm"]["samples_seconds"][0]
                    - current["cold"]["samples_seconds"][0]
                ),
                "warm_projected_cost": current["warm"]["projected_cost"],
                "cold_projected_cost": current["cold"]["projected_cost"],
            }
        )
    return {
        "warm": _aggregate_measurements(reports["warm"]),
        "cold": _aggregate_measurements(reports["cold"]),
        "pairs": pairs,
    }


def _shifted(payload, tick, state, previous):
    result = copy.deepcopy(payload)
    result["plan_start"] = payload["plan_start"] + timedelta(
        minutes=int(payload.get("slot_minutes", 15)) * tick
    )
    result["state"] = state
    for key in (
        "grid_import_price_per_kwh",
        "grid_export_price_per_kwh",
        "solar_input_kwh",
        "usage_kwh",
    ):
        values = result[key]
        result[key] = values[tick:] + values[:tick]
    battery_results = {
        entity["name"]: entity
        for entity in previous["entities"]
        if entity["type"] == "battery"
    }
    for battery in result["battery_entities"]:
        battery["initial_kwh"] = battery_results[battery["name"]]["schedule"][0][
            "level"
        ]
        target = battery.get("target")
        if target is not None:
            timeslot = int(target["timeslot"]) - tick
            if timeslot < 0:
                battery.pop("target")
            else:
                target["timeslot"] = timeslot
    optional_entities = []
    for entity in result.get("optional_entities", []):
        entity["start_after_timeslot"] = max(
            int(entity.get("start_after_timeslot", 0)) - tick, 0
        )
        entity["start_before_timeslot"] = int(entity["start_before_timeslot"]) - tick
        if entity["start_before_timeslot"] - int(
            entity["duration_timeslots"]
        ) < entity["start_after_timeslot"]:
            continue
        optional_entities.append(entity)
    result["optional_entities"] = optional_entities
    return result


def _serial_trajectory(payload, *, disable_mip_starts=False):
    rows = []
    state = None
    previous = None
    comfort_observations = {
        entity["name"]: {
            "history": list(entity.get("on_history") or []),
            "is_on_now": bool(entity["is_on_now"]),
            "off_streak_slots_now": int(entity["off_streak_slots_now"]),
        }
        for entity in payload.get("comfort_entities", [])
    }
    for tick in range(5):
        current = copy.deepcopy(payload) if tick == 0 else _shifted(
            payload, tick, state, previous
        )
        if tick:
            for entity in current.get("comfort_entities", []):
                observed = comfort_observations[entity["name"]]
                entity["on_history"] = list(observed["history"])
                entity["is_on_now"] = observed["is_on_now"]
                entity["off_streak_slots_now"] = observed["off_streak_slots_now"]
        with _SolverRecorder() as recorder:
            result, timing = _run(
                current,
                force_full=False,
                disable_mip_starts=disable_mip_starts,
            )
        solver = recorder.summary()
        rows.append(
            {
                "tick": tick,
                "cadence": result["cadence"]["mode"],
                "cadence_reason": result["cadence"]["reason"],
                "seconds": timing["end_to_end_seconds"],
                "timing": timing,
                "solver": solver,
                "primary_solves": result["successful_solves"],
                "projected_cost": result["projections"]["projected_cost"],
                "quality": _result_quality(current, result),
                "comfort_placement": _placement_report(result),
            }
        )
        state = result["state"]
        previous = result
        comfort_results = {
            entity["name"]: bool(entity["schedule"][0]["enabled"])
            for entity in result["entities"]
            if entity["type"] == "comfort"
        }
        for name, enabled in comfort_results.items():
            observed = comfort_observations[name]
            if observed["history"]:
                observed["history"] = observed["history"][1:] + [enabled]
            observed["off_streak_slots_now"] = (
                0
                if enabled
                else (
                    observed["off_streak_slots_now"] + 1
                    if not observed["is_on_now"]
                    else 1
                )
            )
            observed["is_on_now"] = enabled
    return rows


def _sample_summary(samples):
    median = statistics.median(samples) if samples else None
    return {
        "median_seconds": median,
        "mad_seconds": (
            statistics.median(abs(sample - median) for sample in samples)
            if samples
            else None
        ),
        "min_seconds": min(samples) if samples else None,
        "max_seconds": max(samples) if samples else None,
        "samples_seconds": samples,
    }


def _serial_summary(trajectories):
    full_samples = []
    prefix_samples = []
    cadence_counts = {}
    full_rows = []
    prefix_rows = []
    for trajectory in trajectories:
        for row in trajectory:
            cadence = row["cadence"]
            cadence_counts[cadence] = cadence_counts.get(cadence, 0) + 1
            if cadence == "repair":
                prefix_samples.append(row["seconds"])
                prefix_rows.append(row)
            else:
                full_samples.append(row["seconds"])
                full_rows.append(row)
    return {
        "trajectories": trajectories,
        "full": _sample_summary(full_samples),
        "prefix": _sample_summary(prefix_samples),
        "full_solver": _aggregate_solver_reports(
            [row["solver"] for row in full_rows if "solver" in row]
        ),
        "prefix_solver": _aggregate_solver_reports(
            [row["solver"] for row in prefix_rows if "solver" in row]
        ),
        "cadence_counts": cadence_counts,
        "quality_valid": all(
            row["quality"]["valid"]
            for trajectory in trajectories
            for row in trajectory
        ),
    }


def _measure_serial(payload, repeats, *, disable_mip_starts=False):
    return _serial_summary(
        [
            _serial_trajectory(payload, disable_mip_starts=disable_mip_starts)
            for _ in range(repeats)
        ]
    )


def _strip_solver_details(value):
    if isinstance(value, dict):
        value.pop("calls", None)
        value.pop("steps", None)
        for child in value.values():
            _strip_solver_details(child)
    elif isinstance(value, list):
        for child in value:
            _strip_solver_details(child)


def _paired_serial(payload, repeats):
    trajectories = {"warm": [], "cold": []}
    pairs = []
    for repeat in range(repeats):
        labels = ("warm", "cold") if repeat % 2 == 0 else ("cold", "warm")
        current = {}
        for label in labels:
            trajectory = _serial_trajectory(
                payload, disable_mip_starts=label == "cold"
            )
            trajectories[label].append(trajectory)
            current[label] = trajectory
        warm_total = sum(row["seconds"] for row in current["warm"])
        cold_total = sum(row["seconds"] for row in current["cold"])
        pairs.append(
            {
                "warm_total_seconds": warm_total,
                "cold_total_seconds": cold_total,
                "warm_minus_cold_seconds": warm_total - cold_total,
            }
        )
    return {
        "warm": _serial_summary(trajectories["warm"]),
        "cold": _serial_summary(trajectories["cold"]),
        "pairs": pairs,
    }


def main():
    parser = argparse.ArgumentParser()
    scenario_choices = (
        *sorted(CASE_METADATA),
        "live-export",
        "low-pv",
        "preserve-probe",
        "stress",
    )
    parser.add_argument(
        "--scenario",
        choices=scenario_choices,
        default="no-assets-no-pv",
    )
    parser.add_argument("--slots", type=int, default=144)
    parser.add_argument("--lookahead", type=int, default=48)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--serial", action="store_true")
    parser.add_argument("--disable-mip-starts", action="store_true")
    parser.add_argument("--compare-mip-starts", action="store_true")
    parser.add_argument("--include-call-details", action="store_true")
    args = parser.parse_args()
    if args.compare_mip_starts and args.disable_mip_starts:
        parser.error("--compare-mip-starts cannot be combined with --disable-mip-starts")

    payload = _scenario(args.scenario, args.slots, args.lookahead)
    revision, dirty = _git_state()
    if args.scenario in CASE_METADATA:
        scenario_details = CASE_METADATA[args.scenario]
    else:
        scenario_details = {
            "provenance": (
                "recovered"
                if args.scenario in {"live-export", "low-pv"}
                else "synthetic"
            ),
            "description": {
                "live-export": "Captured Home Assistant forecast with live export pricing.",
                "low-pv": "Captured Home Assistant export adjusted to low PV and low state of charge.",
                "preserve-probe": "Synthetic counterfactual preserve-probe workload.",
                "stress": "Synthetic heterogeneous multi-battery stress workload.",
            }[args.scenario],
        }
    report = {
        "environment": {
            "recorded_at": datetime.now(UTC).isoformat(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "cpu_count": os.cpu_count(),
            "python": platform.python_version(),
            "python_executable": sys.executable,
            "highspy": highspy.Highs().version(),
            "numpy": np.__version__,
            "git_revision": revision,
            "git_dirty": dirty,
            "command": [sys.executable, *sys.argv],
        },
        "scenario": args.scenario,
        "scenario_details": scenario_details,
        "slots": args.slots,
        "lookahead": args.lookahead,
        "settings": {
            "repeats": args.repeats,
            "serial": args.serial,
            "disable_mip_starts": args.disable_mip_starts,
            "compare_mip_starts": args.compare_mip_starts,
            "include_call_details": args.include_call_details,
            "timing_clock": "time.perf_counter",
            "execution": "single_process_serial",
        },
    }
    if args.compare_mip_starts:
        full_comparison = _paired_full(payload, args.repeats)
        report["variants"] = {
            label: {"full": full_comparison[label]}
            for label in ("warm", "cold")
        }
        report["standalone_pairs"] = full_comparison["pairs"]
        if args.serial:
            report["serial_comparison"] = _paired_serial(payload, args.repeats)
    else:
        report["mip_starts"] = not args.disable_mip_starts
        report["full"] = _measure(
            payload,
            args.repeats,
            disable_mip_starts=args.disable_mip_starts,
        )
        if args.serial:
            report["serial"] = _measure_serial(
                payload,
                args.repeats,
                disable_mip_starts=args.disable_mip_starts,
            )
    if not args.include_call_details:
        _strip_solver_details(report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
