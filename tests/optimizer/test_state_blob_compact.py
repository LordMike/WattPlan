"""The persisted state blob stores comfort history as a recomputable seed."""

import base64
import json

import numpy as np

from custom_components.wattplan.optimizer import OptimizationParams, optimize
from custom_components.wattplan.optimizer.models import (
    decode_state_blob,
    encode_state_blob,
)


def _request(horizon=48, window=24):
    return {
        "grid_import_price_per_kwh": [0.2 + 0.1 * (i % 5) for i in range(horizon)],
        "grid_export_price_per_kwh": [0.0] * horizon,
        "solar_input_kwh": [0.0] * horizon,
        "usage_kwh": [0.2] * horizon,
        "lookahead_slots": horizon,
        "rolling_window_slots": window,
        "battery_entities": [],
        "comfort_entities": [
            {
                "name": name, "power_usage_kwh": 0.2,
                "target_on_slots_per_rolling_window": 6,
                "min_consecutive_on_slots": 2, "min_consecutive_off_slots": 2,
                "max_consecutive_off_slots": 12,
                "on_history": [i % 3 == 0 for i in range(window - 1)],
                "is_on_now": False, "off_streak_slots_now": 3,
            }
            for name in ("heat", "water")
        ],
    }


def _raw(blob):
    return json.loads(base64.urlsafe_b64decode(blob).decode("utf-8"))


def test_comfort_history_is_not_persisted_but_decodes_identically():
    state = optimize(OptimizationParams(**_request()))["state"]
    raw = _raw(state)
    assert "comfort_history" not in raw
    decoded = decode_state_blob(state)
    history = np.asarray(decoded["comfort_history"])
    assert history.shape == (2, 49, 23)
    assert np.array_equal(history[:, 0, :], np.asarray(raw["comfort_history_initial"]))
    assert decode_state_blob(encode_state_blob(decoded)) == decoded


def test_old_format_blob_with_full_history_still_decodes_and_reuses():
    request = _request()
    state = optimize(OptimizationParams(**request))["state"]
    decoded = decode_state_blob(state)
    old = json.dumps(decoded, separators=(",", ":"), sort_keys=True).encode("utf-8")
    old_blob = base64.urlsafe_b64encode(old).decode("ascii")
    assert "comfort_history_initial" not in _raw(old_blob)
    assert decode_state_blob(old_blob) == decoded
    assert len(old_blob) > len(state)

    request["state"] = old_blob
    result = optimize(OptimizationParams(**request))
    assert result["successful_solves"] == 0


def test_inconsistent_history_is_stored_in_full():
    decoded = decode_state_blob(optimize(OptimizationParams(**_request()))["state"])
    decoded["comfort_history"][0][5][0] ^= 1
    blob = encode_state_blob(decoded)
    assert "comfort_history" in _raw(blob)
    assert decode_state_blob(blob) == decoded
