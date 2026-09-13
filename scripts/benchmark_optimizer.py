#!/usr/bin/env python3
"""Benchmark full and prefix WattPlan optimization with representative inputs."""

from __future__ import annotations

import argparse
import copy
import json
import math
import platform
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import highspy

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from custom_components.wattplan.optimizer import OptimizationParams, optimize
from custom_components.wattplan.optimizer import mpc_power_optimizer as core
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


def _run(payload, *, force_full, disable_mip_starts=False):
    params = OptimizationParams(**payload)
    original = core._use_mip_starts
    if disable_mip_starts:
        core._use_mip_starts = lambda _entities, _lookahead: False
    start = time.perf_counter()
    try:
        if force_full:
            result = core.optimize_internal(
                core.normalize_calculation_input(params), reuse_plan_override=None
            )
        else:
            result = optimize(params)
        return result, time.perf_counter() - start
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
    return {
        "valid": cost_valid and bounds_valid and targets_valid and not reasons,
        "finite_projected_cost": cost_valid,
        "battery_bounds_valid": bounds_valid,
        "targets_valid": targets_valid,
        "suboptimal_reasons": reasons,
    }


def _measure(payload, repeats, *, force_full=True, disable_mip_starts=False):
    samples = []
    projected_costs = []
    qualities = []
    solver_calls = []
    result = None
    original_solve = core._solve_lp

    def measured_solve(*args, **kwargs):
        start = time.perf_counter()
        solved = original_solve(*args, **kwargs)
        solver_calls.append(
            {
                "total": time.perf_counter() - start,
                "solver": solved.solver_runtime,
                "nodes": solved.mip_node_count,
                "start": solved.mip_start_status,
                "variables": solved.num_variables,
                "integer_variables": solved.num_integer_variables,
                "rows": solved.num_rows,
                "nonzeros": solved.num_nonzeros,
            }
        )
        return solved

    core._solve_lp = measured_solve
    try:
        for _ in range(repeats):
            result, elapsed = _run(
                payload,
                force_full=force_full,
                disable_mip_starts=disable_mip_starts,
            )
            samples.append(elapsed)
            projected_costs.append(result["projections"]["projected_cost"])
            qualities.append(_result_quality(payload, result))
    finally:
        core._solve_lp = original_solve

    primary_solves = result["successful_solves"] * repeats
    started = [row for row in solver_calls if row["start"] is not None]
    report = {
        "median_seconds": statistics.median(samples),
        "samples_seconds": samples,
        "primary_solves": result["successful_solves"],
        "projected_cost": projected_costs[-1],
        "projected_costs": projected_costs,
        "quality_valid": all(quality["valid"] for quality in qualities),
        "quality": qualities,
        "solver": {
            "total_calls": len(solver_calls),
            "probe_calls": len(solver_calls) - primary_solves,
            "submitted_starts": len(started),
            "successful_start_submissions": sum(
                row["start"] == highspy.HighsStatus.kOk for row in started
            ),
            "total_wrapper_seconds": sum(row["total"] for row in solver_calls),
            "total_highs_seconds": sum(row["solver"] for row in solver_calls),
            "total_nodes": sum(row["nodes"] for row in solver_calls),
            "max_model": {
                key: max(row[key] for row in solver_calls)
                for key in ("variables", "integer_variables", "rows", "nonzeros")
            },
        },
    }
    return report


def _aggregate_measurements(reports):
    samples = [sample for report in reports for sample in report["samples_seconds"]]
    costs = [cost for report in reports for cost in report["projected_costs"]]
    qualities = [quality for report in reports for quality in report["quality"]]
    solver_rows = [report["solver"] for report in reports]
    return {
        "median_seconds": statistics.median(samples),
        "samples_seconds": samples,
        "primary_solves": reports[-1]["primary_solves"],
        "projected_cost": costs[-1],
        "projected_costs": costs,
        "quality_valid": all(quality["valid"] for quality in qualities),
        "quality": qualities,
        "solver": {
            key: sum(row[key] for row in solver_rows)
            for key in (
                "total_calls",
                "probe_calls",
                "submitted_starts",
                "successful_start_submissions",
                "total_wrapper_seconds",
                "total_highs_seconds",
                "total_nodes",
            )
        }
        | {
            "max_model": {
                key: max(row["max_model"][key] for row in solver_rows)
                for key in ("variables", "integer_variables", "rows", "nonzeros")
            }
        },
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
    for tick in range(5):
        current = copy.deepcopy(payload) if tick == 0 else _shifted(
            payload, tick, state, previous
        )
        result, elapsed = _run(
            current,
            force_full=False,
            disable_mip_starts=disable_mip_starts,
        )
        rows.append(
            {
                "tick": tick,
                "cadence": result["cadence"]["mode"],
                "seconds": elapsed,
                "primary_solves": result["successful_solves"],
                "projected_cost": result["projections"]["projected_cost"],
                "quality": _result_quality(current, result),
            }
        )
        state = result["state"]
        previous = result
    return rows


def _sample_summary(samples):
    return {
        "median_seconds": statistics.median(samples) if samples else None,
        "samples_seconds": samples,
    }


def _serial_summary(trajectories):
    full_samples = []
    prefix_samples = []
    cadence_counts = {}
    for trajectory in trajectories:
        for row in trajectory:
            cadence = row["cadence"]
            cadence_counts[cadence] = cadence_counts.get(cadence, 0) + 1
            if cadence == "repair":
                prefix_samples.append(row["seconds"])
            else:
                full_samples.append(row["seconds"])
    return {
        "trajectories": trajectories,
        "full": _sample_summary(full_samples),
        "prefix": _sample_summary(prefix_samples),
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
    parser.add_argument(
        "--scenario",
        choices=("live-export", "low-pv", "preserve-probe", "stress"),
        default="low-pv",
    )
    parser.add_argument("--slots", type=int, default=144)
    parser.add_argument("--lookahead", type=int, default=48)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--serial", action="store_true")
    parser.add_argument("--disable-mip-starts", action="store_true")
    parser.add_argument("--compare-mip-starts", action="store_true")
    args = parser.parse_args()
    if args.compare_mip_starts and args.disable_mip_starts:
        parser.error("--compare-mip-starts cannot be combined with --disable-mip-starts")

    payload = _scenario(args.scenario, args.slots, args.lookahead)
    revision, dirty = _git_state()
    report = {
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "highspy": highspy.Highs().version(),
            "git_revision": revision,
            "git_dirty": dirty,
        },
        "scenario": args.scenario,
        "slots": args.slots,
        "lookahead": args.lookahead,
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
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
