"""Write reproducible synthetic full-day recordings for offline planner work.

Forecasts and actuals use separate deterministic profiles. There is no measured
household data here, and generated records are explicitly marked synthetic.
"""

import argparse
from copy import deepcopy
from datetime import UTC, date, datetime, time, timedelta
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from custom_components.wattplan.optimizer.models import OptimizationParams
from custom_components.wattplan.optimizer.simulation.dataset import load_dataset


def profile(slot: int, *, actual: bool) -> dict[str, float]:
    """Tariffs are known; usage/PV forecasts have deterministic forecast errors."""
    hour = (slot % 96) / 4
    usage = 0.16 + (0.38 if 6 <= hour < 9 else 0.55 if 17 <= hour < 21 else 0.0)
    pv = max(0.0, math.sin((hour - 6) / 12 * math.pi)) * (
        1.10 if 6 <= hour <= 18 else 0
    )
    if actual:
        usage *= 1 + 0.12 * math.sin(slot * 0.73)
        pv *= 0.76 + 0.16 * math.sin(slot * 0.31) ** 2
    return {
        "usage_kwh": round(usage, 6),
        "pv_kwh": round(pv, 6),
        "import_price": -0.20 if 2 <= hour < 4 else 0.75 if 17 <= hour < 21 else 0.25,
        "export_price": -0.04 if 11 <= hour < 14 else 0.08,
    }


def records_for_day(day: date, *, stress: bool = False) -> list[dict]:
    start = datetime.combine(day, time(), UTC)
    batteries = [
        {
            "name": "house",
            "initial_kwh": 2.8,
            "minimum_kwh": 0.7,
            "capacity_kwh": 7.0,
            "charge_curve_kwh": [0.85, 0.85, 0.45],
            "discharge_curve_kwh": [0.7, 0.85, 0.85],
            "charge_efficiency": 0.94,
            "discharge_efficiency": 0.92,
            "can_charge_from": 3,
        },
        {
            "name": "vehicle",
            "initial_kwh": 1.2,
            "minimum_kwh": 0.4,
            "capacity_kwh": 5.0,
            "charge_curve_kwh": [0.65, 0.65, 0.25],
            "discharge_curve_kwh": [0.0],
            "charge_efficiency": 0.89,
            "discharge_efficiency": 0.9,
            "can_charge_from": 3,
        },
    ]
    if stress:
        batteries[0]["initial_kwh"] = 0.25  # legal observation below reserve
    records = []
    for tick in range(96):
        at = start + timedelta(minutes=15 * tick)
        forecast = [profile(tick + offset, actual=False) for offset in range(96)]
        equipment = deepcopy(batteries)
        # Fixed 06:00 deadline; the target disappears after its actual deadline.
        if tick <= 23:
            equipment[1]["target"] = {
                "timeslot": 23 - tick,
                "soc_kwh": 4.0,
                "mode": "at_least",
                "tolerance_kwh": 0.05,
            }
        if stress and tick <= 1:
            equipment[0]["target"] = {
                "timeslot": 1 - tick,
                "soc_kwh": 6.0,
                "mode": "exact",
                "tolerance_kwh": 0.0,
            }
        params = OptimizationParams(
            plan_start=at,
            slot_minutes=15,
            grid_import_price_per_kwh=[p["import_price"] for p in forecast],
            grid_export_price_per_kwh=[p["export_price"] for p in forecast],
            usage_kwh=[p["usage_kwh"] for p in forecast],
            solar_input_kwh=[p["pv_kwh"] for p in forecast],
            battery_entities=equipment,
            comfort_entities=[],
            lookahead_slots=32,
            solver_time_limit_seconds=2.0,
        )
        base = {
            "schema_version": 2,
            "setup_id": "synthetic-stress" if stress else "synthetic-day",
            "timezone": "UTC",
            "currency": "TEST",
            "synthetic": True,
            "provenance": {
                "generator": "scripts/generate_simulation_data.py",
                "version": 1,
                "kind": "synthetic",
                "forecast_actuals": "independent deterministic profiles",
            },
        }
        records.append(
            {
                **base,
                "record_type": "plan",
                "created_at": at.isoformat(),
                "input_captured_at": at.isoformat(),
                "request": {"optimizer_params": params.model_dump(mode="json")},
            }
        )
        end = at + timedelta(minutes=15)
        records.append(
            {
                **base,
                "record_type": "actuals",
                "created_at": end.isoformat(),
                "actuals": [
                    {
                        "start": at.isoformat(),
                        "end": end.isoformat(),
                        **profile(tick, actual=True),
                        "quality": [],
                        "flags": 0,
                        "source_roles": {
                            "usage": "synthetic",
                            "pv": "synthetic",
                            "import_price": "synthetic",
                            "export_price": "synthetic",
                        },
                    }
                ],
            }
        )
    return records


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="New or empty data folder")
    parser.add_argument("--start", type=date.fromisoformat, default=date(2026, 6, 15))
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument(
        "--stress",
        action="store_true",
        help="Include below-reserve initial SoC and an unreachable deadline",
    )
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days must be positive")
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("Output folder must be empty")
    args.output.mkdir(parents=True, exist_ok=True)
    for offset in range(args.days):
        day = args.start + timedelta(days=offset)
        with (args.output / f"{day.isoformat()}.jsonl").open(
            "w", encoding="utf-8"
        ) as stream:
            for record in records_for_day(day, stress=args.stress):
                stream.write(json.dumps(record, sort_keys=True, allow_nan=False) + "\n")
    dataset = load_dataset(args.output)
    dataset.validate_coverage()
    print(
        json.dumps(
            {"range": dataset.range_info(), "synthetic": True, "files": dataset.files},
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
