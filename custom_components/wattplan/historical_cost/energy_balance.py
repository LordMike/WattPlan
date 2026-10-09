"""Signed meter balance diagnostics, independent of costs and simulations."""

from __future__ import annotations

import math

ENERGY_BALANCE_KEYS = (
    "grid_import",
    "grid_export",
    "usage",
    "pv",
    "battery_charge",
    "battery_discharge",
)


def energy_balance_delta(
    previous: dict[str, float | None],
    current: dict[str, float | None],
) -> float | None:
    """Return unaccounted kWh over a common window, or None for invalid meters."""
    deltas = {}
    for key in ENERGY_BALANCE_KEYS:
        before, after = previous.get(key), current.get(key)
        if before is None or after is None:
            return None
        try:
            before, after = float(before), float(after)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(before) or not math.isfinite(after):
            return None
        delta = after - before
        if not math.isfinite(delta) or delta < 0:
            return None
        deltas[key] = delta
    try:
        result = math.fsum(
            (
                deltas["grid_import"],
                deltas["pv"],
                deltas["battery_discharge"],
                -deltas["grid_export"],
                -deltas["usage"],
                -deltas["battery_charge"],
            )
        )
    except OverflowError:
        return None
    return result if math.isfinite(result) else None
