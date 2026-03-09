#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List


def _as_float_list(values: Any, field_name: str) -> List[float]:
    if not isinstance(values, list):
        raise ValueError(f"{field_name} must be a list")
    try:
        return [float(value) for value in values]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must only contain numbers") from exc


def _clamp_percent(value: Any, default: float = 0.0) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(100.0, numeric))


def _build_battery_entity(
    source: Dict[str, Any],
    fallback_name: str,
    can_charge_from: int,
    horizon_timeslots: int,
) -> Dict[str, Any]:
    if not isinstance(source, dict):
        raise ValueError(f"{fallback_name} must be an object")

    capacity_wh = float(source.get("capacity_wh", 0.0))
    if capacity_wh <= 0:
        raise ValueError(f"{fallback_name}.capacity_wh must be > 0")

    # EOS values are treated as power in kW. We assume 15-minute slots,
    # so per-slot energy is kWh_per_slot = kW * 0.25.
    max_charge_power_kw = float(source.get("max_charge_power_w", 0.0))
    max_discharge_power_kw = float(
        source.get("max_discharge_power_w", max_charge_power_kw)
    )
    slot_hours = 0.25

    capacity_kwh = capacity_wh / 1000.0
    initial_soc = _clamp_percent(source.get("initial_soc_percentage"), default=0.0)
    minimum_soc = _clamp_percent(source.get("min_soc_percentage"), default=0.0)
    target_soc = _clamp_percent(
        source.get("target_soc_percentage", source.get("initial_soc_percentage")),
        default=initial_soc,
    )

    return {
        "name": str(source.get("device_id") or fallback_name),
        "initial_kwh": capacity_kwh * (initial_soc / 100.0),
        "minimum_kwh": capacity_kwh * (minimum_soc / 100.0),
        "capacity_kwh": capacity_kwh,
        "target": {
            "timeslot": max(0, horizon_timeslots - 1),
            "soc_kwh": capacity_kwh * (target_soc / 100.0),
            "mode": "at_least",
            "tolerance_kwh": capacity_kwh * 0.005,
        },
        "charge_curve_kwh": [max(0.0, max_charge_power_kw * slot_hours)],
        "discharge_curve_kwh": [max(0.0, max_discharge_power_kw * slot_hours)],
        "can_charge_from": int(can_charge_from),
    }


def convert_eos_request(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("input payload must be a JSON object")

    ems = payload.get("ems")
    if not isinstance(ems, dict):
        raise ValueError("missing required object: ems")

    prices_eur_per_wh = _as_float_list(
        ems.get("strompreis_euro_pro_wh"), "ems.strompreis_euro_pro_wh"
    )
    solar_wh = _as_float_list(ems.get("pv_prognose_wh"), "ems.pv_prognose_wh")
    usage_wh = _as_float_list(ems.get("gesamtlast"), "ems.gesamtlast")

    interval_count = len(prices_eur_per_wh)
    if len(solar_wh) != interval_count or len(usage_wh) != interval_count:
        raise ValueError(
            "ems arrays must have equal length: "
            f"prices={len(prices_eur_per_wh)}, solar={len(solar_wh)}, usage={len(usage_wh)}"
        )

    battery_entities: List[Dict[str, Any]] = []
    if payload.get("pv_akku") is not None:
        battery_entities.append(
            _build_battery_entity(
                payload["pv_akku"],
                fallback_name="battery",
                can_charge_from=3,
                horizon_timeslots=interval_count,
            )
        )
    if payload.get("eauto") is not None:
        battery_entities.append(
            _build_battery_entity(
                payload["eauto"],
                fallback_name="ev",
                can_charge_from=1,
                horizon_timeslots=interval_count,
            )
        )

    if not battery_entities:
        raise ValueError("no battery entities found (expected pv_akku and/or eauto)")

    return {
        "grid_import_price_per_kwh": [value * 1000.0 for value in prices_eur_per_wh],
        "solar_input_kwh": [value / 1000.0 for value in solar_wh],
        "usage_kwh": [value / 1000.0 for value in usage_wh],
        "battery_entities": battery_entities,
        "comfort_entities": [],
    }


def _load_json(path_or_stdin: str) -> Dict[str, Any]:
    if path_or_stdin == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(path_or_stdin).read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("input JSON root must be an object")
    return data


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert an EOS_Connect optimization request into this project's input format"
    )
    parser.add_argument(
        "input", help="Input EOS_Connect JSON file path, or '-' for stdin"
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Output path for converted JSON (defaults to stdout)",
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        help="JSON indentation level (default: 2)",
    )
    args = parser.parse_args()

    try:
        source = _load_json(args.input)
        converted = convert_eos_request(source)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    output = json.dumps(converted, indent=args.indent) + "\n"
    if args.output:
        output_path = Path(args.output)
        output_path.write_text(output, encoding="utf-8")
    else:
        sys.stdout.write(output)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
