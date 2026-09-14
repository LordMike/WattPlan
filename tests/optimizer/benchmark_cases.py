"""Dedicated, deterministic fixtures for planner performance benchmarks."""

from __future__ import annotations

import copy
import math
from datetime import UTC, datetime


CASE_METADATA = {
    "no-assets-no-pv": {
        "provenance": "synthetic",
        "description": "No batteries, comfort loads, or PV.",
    },
    "no-battery-comfort-no-pv": {
        "provenance": "synthetic",
        "description": "Flexible comfort demand without batteries or PV.",
    },
    "no-battery-comfort-pv": {
        "provenance": "synthetic",
        "description": "Flexible comfort demand and daytime PV without batteries.",
    },
    "battery-zero-pv": {
        "provenance": "synthetic",
        "description": "Bidirectional battery with an explicitly zero PV series.",
    },
    "battery-zero-pv-signed-target": {
        "provenance": "synthetic",
        "description": (
            "Zero-PV battery with signed tariffs, deadband, and a fixed SOC target."
        ),
    },
    "charge-only-battery": {
        "provenance": "synthetic",
        "description": "Grid-charge-only vehicle battery with no discharge capability.",
    },
    "mixed-batteries-pv": {
        "provenance": "synthetic",
        "description": "Charge-only vehicle and bidirectional house battery with PV.",
    },
    "comfort-flexible": {
        "provenance": "synthetic",
        "description": "Comfort-only case with substantial placement flexibility.",
    },
    "comfort-tight": {
        "provenance": "synthetic-historical-substitute",
        "description": (
            "Tightly constrained comfort-only substitute; repository history has no "
            "recoverable named slow-comfort fixture."
        ),
    },
}


def _base_payload(slots: int, lookahead: int) -> dict:
    prices = []
    exports = []
    pv = []
    usage = []
    for slot in range(slots):
        day_slot = slot % 96
        prices.append(
            0.18
            + (0.42 if 28 <= day_slot < 40 else 0.0)
            + (0.85 if 68 <= day_slot < 84 else 0.0)
        )
        exports.append(0.04 + (0.12 if 40 <= day_slot < 68 else 0.0))
        pv.append(max(0.0, 1.8 * math.sin((day_slot - 24) * math.pi / 56)))
        usage.append(0.22 + (0.55 if 68 <= day_slot < 88 else 0.0))
    return {
        "plan_start": datetime(2026, 9, 14, tzinfo=UTC),
        "slot_minutes": 15,
        "grid_import_price_per_kwh": prices,
        "grid_export_price_per_kwh": exports,
        "solar_input_kwh": pv,
        "usage_kwh": usage,
        "rolling_window_slots": 24,
        "lookahead_slots": min(lookahead, slots),
        "battery_entities": [],
        "comfort_entities": [],
    }


def _flexible_comfort() -> dict:
    return {
        "name": "flexible-heat-pump",
        "target_on_slots_per_rolling_window": 6,
        "min_consecutive_on_slots": 2,
        "min_consecutive_off_slots": 2,
        "max_consecutive_off_slots": 12,
        "power_usage_kwh": 0.55,
        "is_on_now": False,
        "on_slots_last_rolling_window": 6,
        "on_history": [False] * 11 + [True] * 6 + [False] * 6,
        "off_streak_slots_now": 6,
    }


def _tight_comfort() -> dict:
    return {
        "name": "tight-heat-pump",
        "target_on_slots_per_rolling_window": 12,
        "min_consecutive_on_slots": 4,
        "min_consecutive_off_slots": 3,
        "max_consecutive_off_slots": 4,
        "power_usage_kwh": 0.75,
        "is_on_now": False,
        "on_slots_last_rolling_window": 12,
        "on_history": [True] * 12 + [False] * 11,
        "off_streak_slots_now": 11,
    }


def _house_battery(*, charge_sources: int = 3) -> dict:
    return {
        "name": "house-battery",
        "initial_kwh": 4.0,
        "minimum_kwh": 0.8,
        "capacity_kwh": 10.0,
        "charge_curve_kwh": [2.0, 1.5, 0.8],
        "discharge_curve_kwh": [2.1, 1.6, 0.9],
        "charge_efficiency": 0.92,
        "discharge_efficiency": 0.91,
        "can_charge_from": charge_sources,
    }


def build_case(name: str, slots: int = 96, lookahead: int = 48) -> dict:
    """Build one isolated benchmark payload without sharing mutable state."""
    if name not in CASE_METADATA:
        raise KeyError(name)
    payload = _base_payload(slots, lookahead)

    if name in {
        "no-assets-no-pv",
        "no-battery-comfort-no-pv",
        "battery-zero-pv",
        "battery-zero-pv-signed-target",
    }:
        payload["solar_input_kwh"] = [0.0] * slots
    if name in {"no-battery-comfort-no-pv", "no-battery-comfort-pv", "comfort-flexible"}:
        payload["comfort_entities"] = [_flexible_comfort()]
    elif name == "comfort-tight":
        payload["comfort_entities"] = [_tight_comfort()]
    elif name == "battery-zero-pv":
        payload["action_deadband_kwh"] = 0.04
        payload["battery_entities"] = [_house_battery()]
    elif name == "battery-zero-pv-signed-target":
        payload["grid_import_price_per_kwh"] = [
            price - (0.55 if slot % 24 < 5 else 0.0)
            for slot, price in enumerate(payload["grid_import_price_per_kwh"])
        ]
        payload["grid_export_price_per_kwh"] = [
            (-0.20 if slot % 3 == 0 else 0.35)
            for slot in range(slots)
        ]
        payload["action_deadband_kwh"] = 0.04
        battery = _house_battery()
        battery["target"] = {
            "timeslot": max(slots - 12, 0),
            "soc_kwh": 6.0,
            "mode": "at_least",
            "tolerance_kwh": 0.05,
        }
        payload["battery_entities"] = [battery]
    elif name == "charge-only-battery":
        payload["battery_entities"] = [
            {
                "name": "vehicle",
                "initial_kwh": 8.0,
                "minimum_kwh": 5.0,
                "capacity_kwh": 40.0,
                "charge_curve_kwh": [2.8, 2.0, 1.2],
                "discharge_curve_kwh": [0.0],
                "charge_efficiency": 0.94,
                "discharge_efficiency": 1.0,
                "can_charge_from": 1,
            }
        ]
    elif name == "mixed-batteries-pv":
        payload["action_deadband_kwh"] = 0.04
        payload["battery_entities"] = [
            _house_battery(),
            {
                "name": "vehicle",
                "initial_kwh": 12.0,
                "minimum_kwh": 5.0,
                "capacity_kwh": 40.0,
                "charge_curve_kwh": [2.8, 2.0, 1.2],
                "discharge_curve_kwh": [0.0],
                "charge_efficiency": 0.94,
                "discharge_efficiency": 1.0,
                "can_charge_from": 1,
            },
        ]

    return copy.deepcopy(payload)
