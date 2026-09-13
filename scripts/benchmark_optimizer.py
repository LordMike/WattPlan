#!/usr/bin/env python3
"""Benchmark full and prefix WattPlan optimization with representative inputs."""

from __future__ import annotations

import argparse
import copy
import json
import math
import platform
import statistics
import time
from datetime import UTC, datetime, timedelta

import highspy

from custom_components.wattplan.optimizer import OptimizationParams, optimize
from custom_components.wattplan.optimizer import mpc_power_optimizer as core
from tests.optimizer.test_optimizer_scenarios import (
    _live_exported_deye_low_pv_low_soc_payload,
    _live_grid_export_benchmark_payload,
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


def _scenario(name, slots, lookahead):
    if name == "live-export":
        payload, export_prices = _live_grid_export_benchmark_payload()
        payload["grid_export_price_per_kwh"] = export_prices
        result = _extend(payload, slots)
    elif name == "low-pv":
        result = _extend(_live_exported_deye_low_pv_low_soc_payload(), slots)
    else:
        return _stress_payload(slots, lookahead)
    result["lookahead_slots"] = lookahead
    return result


def _run(payload, *, force_full, disable_mip_starts=False):
    params = OptimizationParams(**payload)
    original = core._use_mip_starts
    if disable_mip_starts:
        core._use_mip_starts = lambda _entities: False
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


def _measure(payload, repeats, *, force_full=True, disable_mip_starts=False):
    samples = []
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
    finally:
        core._solve_lp = original_solve

    primary_solves = result["successful_solves"] * repeats
    started = [row for row in solver_calls if row["start"] is not None]
    report = {
        "median_seconds": statistics.median(samples),
        "samples_seconds": samples,
        "primary_solves": result["successful_solves"],
        "projected_cost": result["projections"]["projected_cost"],
        "solver": {
            "total_calls": len(solver_calls),
            "probe_calls": len(solver_calls) - primary_solves,
            "submitted_starts": len(started),
            "accepted_starts": sum(
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


def _shifted(payload, tick, state, previous):
    result = copy.deepcopy(payload)
    result["plan_start"] = payload["plan_start"] + timedelta(minutes=15 * tick)
    result["state"] = state
    for key in (
        "grid_import_price_per_kwh",
        "grid_export_price_per_kwh",
        "solar_input_kwh",
        "usage_kwh",
    ):
        values = result[key]
        result[key] = values[tick:] + values[:tick]
    for battery, entity in zip(result["battery_entities"], previous["entities"]):
        battery["initial_kwh"] = entity["schedule"][0]["level"]
    return result


def _serial(payload, *, disable_mip_starts=False):
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
            }
        )
        state = result["state"]
        previous = result
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scenario", choices=("live-export", "low-pv", "stress"), default="low-pv"
    )
    parser.add_argument("--slots", type=int, default=144)
    parser.add_argument("--lookahead", type=int, default=48)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--serial", action="store_true")
    parser.add_argument("--disable-mip-starts", action="store_true")
    args = parser.parse_args()

    payload = _scenario(args.scenario, args.slots, args.lookahead)
    report = {
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "highspy": highspy.Highs().version(),
        },
        "scenario": args.scenario,
        "slots": args.slots,
        "lookahead": args.lookahead,
        "mip_starts": not args.disable_mip_starts,
        "full": _measure(
            payload,
            args.repeats,
            disable_mip_starts=args.disable_mip_starts,
        ),
    }
    if args.serial:
        report["serial"] = _serial(
            payload, disable_mip_starts=args.disable_mip_starts
        )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
