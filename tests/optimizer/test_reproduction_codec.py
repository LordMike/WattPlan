"""Lossless archive format for self-contained optimizer diagnostics."""

from __future__ import annotations

import base64
import lzma
import random

import pytest

from custom_components.wattplan.optimizer.reproduction_codec import (
    MAX_DECODED_BYTES,
    decode_reproduction,
    encode_reproduction,
)


def test_binary_reproduction_round_trip_preserves_numbers_and_types() -> None:
    """Saved floats, settings and actions must survive without rounding."""
    record = {
        "schema_version": 1,
        "request": {
            "optimizer_params": {
                "import_prices": [1.23456789123456, -0.1, 0.0],
                "battery_entities": [{"name": "Batteri", "initial_kwh": 5.125}],
                "state": None,
            }
        },
        "result": {"entities": [{"state": "grid_charge", "enabled": True}]},
    }
    assert decode_reproduction(encode_reproduction(record)) == record


def test_high_entropy_144_slot_archive_and_full_response_limit() -> None:
    """A compact 144-slot result fits, but a full one may exceed Recorder."""
    rng = random.Random(7)
    slots = 144
    request = {
        "grid_import_price_per_kwh": [rng.uniform(0, 6) for _ in range(slots)],
        "grid_export_price_per_kwh": [rng.uniform(-1, 3) for _ in range(slots)],
        "usage_kwh": [rng.uniform(0, 1) for _ in range(slots)],
        "solar_input_kwh": [rng.uniform(0, 2) for _ in range(slots)],
        "battery_entities": [
            {
                "name": "battery",
                "initial_kwh": 5.8,
                "minimum_kwh": 1.0,
                "capacity_kwh": 10.0,
                "charge_curve_kwh": [rng.uniform(0.2, 0.6) for _ in range(slots)],
                "discharge_curve_kwh": [rng.uniform(0.2, 0.6) for _ in range(slots)],
                "charge_efficiency": 0.9,
                "discharge_efficiency": 0.9,
                "can_charge_from": 3,
            }
        ],
        "comfort_entities": [],
        "optional_entities": [],
        "state": None,
    }
    result = {
        "entities": [
            {
                "name": "battery",
                "type": "battery",
                "schedule": [
                    {
                        "state": rng.choice(
                            ["grid_charge", "preserve", "self_consume"]
                        ),
                        "level": rng.uniform(1, 10),
                    }
                    for _ in range(slots)
                ],
            }
        ],
        "projections": {
            "per_slot": [
                {
                    "projected_cost": rng.uniform(-1, 3),
                    "projected_savings_cost": rng.uniform(-1, 3),
                    "projected_savings_pct": rng.uniform(-100, 100),
                }
                for _ in range(slots)
            ]
        },
        "state": None,
    }
    record = {"schema_version": 1, "request": request, "result": result}
    encoded = encode_reproduction(record)
    assert len(encoded) < 16_000
    assert decode_reproduction(encoded) == record

    # A complete result has additional per-slot values. No lossless encoding
    # can promise to fit arbitrary high-entropy floats inside Recorder's cap.
    for slot in result["projections"]["per_slot"]:
        slot["baseline_cost"] = rng.uniform(-1, 3)
    result["energy_flows"] = {
        "per_slot": [
            {
                key: rng.uniform(0, 3)
                for key in (
                    "load_kwh",
                    "base_load_kwh",
                    "comfort_load_kwh",
                    "grid_import_kwh",
                    "grid_export_kwh",
                    "grid_charge_kwh",
                )
            }
            for _ in range(slots)
        ]
    }
    with pytest.raises(ValueError, match="too large for Recorder"):
        encode_reproduction(record)


def test_archive_rejects_invalid_values_and_unknown_format() -> None:
    with pytest.raises(ValueError, match="non-finite"):
        encode_reproduction({"value": float("nan")})
    with pytest.raises(TypeError):
        encode_reproduction({"value": object()})
    with pytest.raises(ValueError, match="Unsupported"):
        decode_reproduction(
            base64.b85encode(lzma.compress(b"NOT-WATTPLAN")).decode()
        )
    with pytest.raises(ValueError, match="too large"):
        decode_reproduction("A" * 16_001)
    bomb = base64.b85encode(lzma.compress(b"x" * (MAX_DECODED_BYTES + 1))).decode()
    with pytest.raises(ValueError, match="oversized"):
        decode_reproduction(bomb)
