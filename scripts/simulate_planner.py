"""Evaluate planner variants against selected recorded inputs and actuals."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from dataclasses import asdict
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from custom_components.wattplan.optimizer.planners.presets import PRESETS
from custom_components.wattplan.optimizer.planners.registry import BUILTINS
from custom_components.wattplan.optimizer.simulation.dataset import (
    canonical,
    input_files,
    load_dataset,
    timestamp,
)
from custom_components.wattplan.optimizer.simulation.engine import (
    RUN_TIMEOUT_SECONDS,
    RunBudget,
    Variant,
    simulate,
)
from custom_components.wattplan.optimizer.simulation.reporting import (
    latency_summary,
    write_reports,
)
from custom_components.wattplan.optimizer.simulation.worker import PlannerWorker


def _provenance() -> dict:
    def git(*args):
        result = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
        )
        return result.stdout.strip() if result.returncode == 0 else None

    hashes = {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(
            (ROOT / "custom_components/wattplan/optimizer").rglob("*.py")
        )
    }
    packages = {}
    for name in ("numpy", "pydantic", "highspy"):
        try:
            packages[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            packages[name] = None
    return {
        "python": sys.version,
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "git_revision": git("rev-parse", "HEAD"),
        "git_status": git("status", "--porcelain"),
        "source_hashes": hashes,
        "packages": packages,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "Workflow and input requirements: docs/simulator.md "
            "(https://github.com/LordMike/WattPlan/blob/main/docs/simulator.md)"
        ),
    )
    parser.add_argument(
        "input",
        nargs="*",
        help="One or more JSON/JSONL files, quoted globs, or recursive directories",
    )
    parser.add_argument("--setup-id")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--config",
        type=Path,
        help="JSON variants: planner is a built-in name or module:factory; settings and options are objects",
    )
    selection.add_argument(
        "--planners", nargs="+", choices=sorted(PRESETS.keys() | BUILTINS.keys() | {"all"}),
        metavar="PRESET",
        help="Preset IDs or built-in algorithms; 'all' runs every published preset",
    )
    parser.add_argument(
        "--list-planners", action="store_true",
        help="List available presets and algorithm names, then exit without inputs",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="New or empty report folder outside the input folder",
    )
    parser.add_argument(
        "--validate-only", action="store_true",
        help="Check recording coverage and variant structure without executing planners",
    )
    parser.add_argument(
        "--patch-one-slot",
        choices=("previous", "next"),
        help="Explicitly copy one deficient slot from its immediate neighbor",
    )
    parser.add_argument(
        "--repeats", "--rounds", type=int, default=1,
        help="Complete replays per selected configuration (default: 1)",
    )
    args = parser.parse_args(argv)
    if args.list_planners:
        for name, preset in PRESETS.items():
            print(f"{name}: {preset.label}")
        print("Algorithm defaults: " + ", ".join(BUILTINS))
        print("all: every published preset above")
        return 0
    if not args.input:
        parser.error("at least one input file, glob, or directory is required")
    if args.planners and "all" in args.planners and len(args.planners) != 1:
        parser.error("use --planners all by itself")
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    if not args.validate_only and args.output is None:
        parser.error("--output is required for execution")
    try:
        selected = input_files(args.input)
        if args.output and (
            any(p.is_relative_to(args.output.resolve()) for p in selected)
            or any(
                Path(source).is_dir()
                and args.output.resolve().is_relative_to(Path(source).resolve())
                for source in args.input
            )
        ):
            raise ValueError("Input and output folders must be separate")
        if args.output and args.output.exists() and any(args.output.iterdir()):
            raise ValueError("Output directory must be empty")
        dataset = load_dataset(selected, setup_id=args.setup_id)
        inferred_range = dataset.range_info()
        dataset.validate_coverage(args.patch_one_slot)
        if args.config:
            config = json.loads(args.config.read_text(encoding="utf-8"))
            if set(config) != {"variants"}:
                raise ValueError("Config must contain only variants")
            variants = [Variant(**item) for item in config["variants"]]
        elif args.planners:
            names = list(PRESETS) if args.planners == ["all"] else args.planners
            variants = [
                Variant(
                    name,
                    PRESETS[name].planner,
                    options=dict(PRESETS[name].options),
                    preset=name,
                )
                if name in PRESETS else Variant(name, name)
                for name in names
            ]
        else:
            variants = [
                Variant(
                    "highs",
                    PRESETS["highs"].planner,
                    options=dict(PRESETS["highs"].options),
                    preset="highs",
                ),
                Variant(
                    "self-consumption",
                    PRESETS["self-consumption"].planner,
                    options=dict(PRESETS["self-consumption"].options),
                    preset="self-consumption",
                ),
            ]
        if not variants or len({v.name for v in variants}) != len(variants):
            raise ValueError("Provide uniquely named variants")
        windows, skipped = dataset.windows()
        coverage = {
            "inferred_range": inferred_range,
            "patches": dataset.patches,
            "initial_capture_delay_seconds": (
                windows[0].start - timestamp(inferred_range["start"])
            ).total_seconds(),
            "decisions": len(dataset.events),
            "executed_decisions": len(windows),
            "actual_intervals": len(dataset.actuals),
            "unusable_actuals": [a.values for a in dataset.actuals if not a.usable],
            "skipped_decisions": skipped,
            "windows": [
                {
                    "start": w.start.isoformat(),
                    "end": w.end.isoformat(),
                    "segment": w.segment,
                    "reset_reason": w.reset_reason,
                }
                for w in windows
            ],
            "covered_seconds": sum((w.end - w.start).total_seconds() for w in windows),
        }
        coverage["covered_hours"] = coverage["covered_seconds"] / 3600
        coverage["covered_slots"] = coverage["covered_seconds"] / (
            inferred_range["slot_minutes"] * 60
        )
        if args.validate_only:
            print(
                json.dumps(
                    {
                        "setup_id": dataset.setup_id,
                        "configuration": [asdict(v) for v in variants],
                        "coverage": coverage,
                        "warnings": dataset.warnings,
                    },
                    indent=2,
                )
            )
            return 0 if windows else 1
        runs, order = [], []
        print(
            json.dumps({"inferred_range": inferred_range, "patches": dataset.patches}),
            flush=True,
        )
        for repeat in range(args.repeats):
            for variant in variants if repeat % 2 == 0 else list(reversed(variants)):
                label = f"{variant.name} / repeat {repeat + 1}"
                print(label, flush=True)
                budget = RunBudget()
                with PlannerWorker(variant.planner, variant.options) as worker:
                    run = simulate(dataset, variant, worker, budget=budget)
                run.update(run=label, repeat=repeat + 1, configuration=asdict(variant))
                run["latency"] = {
                    key: latency_summary(run["cycles"], key)
                    for key in ("wall_seconds", "cpu_seconds")
                }
                runs.append(run)
                order.append(label)
        configuration = [asdict(v) for v in variants]
        summary = {
            "schema_version": 1,
            "run_timeout_seconds": RUN_TIMEOUT_SECONDS,
            "setup_id": dataset.setup_id,
            "currency": dataset.currency,
            "timezone": dataset.timezone,
            "coverage": coverage,
            "warnings": dataset.warnings,
            "files": dataset.files,
            "provenance": _provenance(),
            "run_order": order,
            "configuration": configuration,
            "configuration_sha256": hashlib.sha256(
                canonical(configuration).encode()
            ).hexdigest(),
        }
        write_reports(args.output, summary, runs)
        print(f"Report: {args.output / 'report.html'}")
        return 0 if all(run["status"] == "complete" for run in runs) else 1
    except (ValueError, OSError, TypeError, KeyError) as err:
        if "inferred_range" in locals():
            print(
                json.dumps(
                    {
                        "inferred_range": inferred_range,
                        "files": dataset.files,
                        "error": str(err),
                    }
                )
            )
        print(f"Simulation error: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
