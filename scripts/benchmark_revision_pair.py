#!/usr/bin/env python3
"""Run alternating, serialized optimizer benchmarks across two revisions."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys


HARNESS_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK = HARNESS_ROOT / "scripts" / "benchmark_optimizer.py"


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


def _run(root, scenario, slots, lookahead):
    env = os.environ.copy()
    env["WATTPLAN_BENCHMARK_PRODUCTION_ROOT"] = str(root)
    command = [
        sys.executable,
        str(BENCHMARK),
        "--scenario",
        scenario,
        "--slots",
        str(slots),
        "--lookahead",
        str(lookahead),
        "--repeats",
        "1",
        "--serial",
    ]
    completed = subprocess.run(
        command,
        cwd=HARNESS_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def _placement_work(placement):
    if placement is None:
        return None
    fields = (
        "status",
        "accepted",
        "cost_mode",
        "candidate_cost_calls",
        "candidates_generated",
        "candidates_considered",
        "candidates_evaluated",
        "candidates_rejected",
        "candidates_not_improving",
        "candidates_unvisited",
        "accepted_moves",
        "demand_changed",
        "additional_planning_passes",
        "additional_successful_solves",
        "baseline_projected_cost",
        "final_projected_cost",
        "fallback_reason",
    )
    return {field: placement.get(field) for field in fields}


def _count_unique(rows):
    unique = {}
    for row in rows:
        key = json.dumps(row, sort_keys=True, separators=(",", ":"))
        if key not in unique:
            unique[key] = {"occurrences": 0, "work": row}
        unique[key]["occurrences"] += 1
    return list(unique.values())


def _normalize(report):
    full = report["full"]
    trajectory = report["serial"]["trajectories"][0]
    return {
        "standalone": {
            "seconds": full["samples_seconds"][0],
            "projected_cost": full["projected_cost"],
            "quality": full["quality"][0],
            "successful_solves": full["primary_solves"],
            "solver": {
                field: full["runs"][0]["solver"][field]
                for field in (
                    "primary_calls",
                    "probe_calls",
                    "total_calls",
                    "total_wrapper_seconds",
                    "total_highs_seconds",
                    "max_model",
                )
            },
            "comfort_placement": _placement_work(
                full.get("comfort_placement", [None])[0]
            ),
        },
        "trajectory": [
            {
                "tick": row["tick"],
                "cadence": row["cadence"],
                "cadence_reason": row.get("cadence_reason"),
                "seconds": row["seconds"],
                "projected_cost": row["projected_cost"],
                "quality": row["quality"],
                "successful_solves": row["primary_solves"],
                "solver": {
                    field: row["solver"][field]
                    for field in (
                        "primary_calls",
                        "probe_calls",
                        "total_calls",
                        "total_wrapper_seconds",
                        "total_highs_seconds",
                        "max_model",
                    )
                },
                "comfort_placement": _placement_work(
                    row.get("comfort_placement")
                ),
            }
            for row in trajectory
        ],
    }


def _variant_summary(runs):
    standalone = [run["standalone"]["seconds"] for run in runs]
    full = [
        row["seconds"]
        for run in runs
        for row in run["trajectory"]
        if row["cadence"] != "repair"
    ]
    prefix = [
        row["seconds"]
        for run in runs
        for row in run["trajectory"]
        if row["cadence"] == "repair"
    ]
    cadence_counts = {}
    for run in runs:
        for row in run["trajectory"]:
            cadence = row["cadence"]
            cadence_counts[cadence] = cadence_counts.get(cadence, 0) + 1
    standalone_work = [
        {
            "successful_solves": run["standalone"]["successful_solves"],
            **run["standalone"]["solver"],
            "comfort_placement": run["standalone"]["comfort_placement"],
        }
        for run in runs
    ]
    trajectory_work = [
        {
            "cadence": row["cadence"],
            "successful_solves": row["successful_solves"],
            **row["solver"],
            "comfort_placement": row["comfort_placement"],
        }
        for run in runs
        for row in run["trajectory"]
    ]
    return {
        "standalone": _sample_summary(standalone),
        "trajectory_full": _sample_summary(full),
        "trajectory_prefix": _sample_summary(prefix),
        "quality_valid": all(
            run["standalone"]["quality"]["valid"]
            and all(row["quality"]["valid"] for row in run["trajectory"])
            for run in runs
        ),
        "standalone_projected_costs": [
            run["standalone"]["projected_cost"] for run in runs
        ],
        "standalone_quality": [run["standalone"]["quality"] for run in runs],
        "cadence_counts": cadence_counts,
        "standalone_work": _count_unique(standalone_work),
        "trajectory_work": _count_unique(trajectory_work),
    }


def _paired_summary(pairs, field):
    deltas = []
    wins = 0
    for pair in pairs:
        baseline = field(pair["baseline"])
        candidate = field(pair["candidate"])
        delta = candidate - baseline
        deltas.append(delta)
        wins += delta < 0
    return {
        **_sample_summary(deltas),
        "candidate_win_count": wins,
        "pair_count": len(pairs),
    }


def _scenario_report(baseline_root, candidate_root, scenario, slots, lookahead, repeats):
    runs = {"baseline": [], "candidate": []}
    pairs = []
    for repeat in range(repeats):
        order = (
            ("baseline", "candidate")
            if repeat % 2 == 0
            else ("candidate", "baseline")
        )
        current = {}
        for label in order:
            root = baseline_root if label == "baseline" else candidate_root
            current[label] = _normalize(_run(root, scenario, slots, lookahead))
            runs[label].append(current[label])
        pairs.append({"execution_order": list(order), **current})

    return {
        "variants": {
            label: _variant_summary(runs[label])
            for label in ("baseline", "candidate")
        },
        "paired_deltas": {
            "standalone": _paired_summary(
                pairs, lambda run: run["standalone"]["seconds"]
            ),
            "trajectory_total": _paired_summary(
                pairs,
                lambda run: sum(row["seconds"] for row in run["trajectory"]),
            ),
            "trajectory_full_total": _paired_summary(
                pairs,
                lambda run: sum(
                    row["seconds"]
                    for row in run["trajectory"]
                    if row["cadence"] != "repair"
                ),
            ),
            "trajectory_prefix_total": _paired_summary(
                pairs,
                lambda run: sum(
                    row["seconds"]
                    for row in run["trajectory"]
                    if row["cadence"] == "repair"
                ),
            ),
        },
        "pairs": pairs,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-root", type=Path, required=True)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--scenario", action="append", required=True)
    parser.add_argument("--slots", type=int, default=96)
    parser.add_argument("--lookahead", type=int, default=48)
    parser.add_argument("--repeats", type=int, default=9)
    parser.add_argument("--include-pairs", action="store_true")
    args = parser.parse_args()

    report = {
        "baseline_root": str(args.baseline_root.resolve()),
        "candidate_root": str(args.candidate_root.resolve()),
        "slots": args.slots,
        "lookahead": args.lookahead,
        "repeats": args.repeats,
        "execution": "alternating_pairs_single_process_serial",
        "scenarios": {},
    }
    for scenario in args.scenario:
        scenario_report = _scenario_report(
            args.baseline_root.resolve(),
            args.candidate_root.resolve(),
            scenario,
            args.slots,
            args.lookahead,
            args.repeats,
        )
        if not args.include_pairs:
            scenario_report.pop("pairs")
        report["scenarios"][scenario] = scenario_report
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
