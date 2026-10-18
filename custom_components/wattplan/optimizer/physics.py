"""Shared AC-side battery accounting for named inverter policies.

Curves specify energy per *original planner slot*. A fractional step scales
power limits, but not the full-slot grid-charge headroom eligibility test.
Levels below the reserve are legal; discharging must never manufacture energy
by clamping such a level up to the reserve.
"""

from dataclasses import dataclass, replace
import math
from typing import Literal, Protocol, Sequence

EPSILON = 1e-6
MIN_GRID_CHARGE_SLOT_FRACTION = 0.5
POLICIES = frozenset({"self_consume", "preserve", "grid_charge"})


class Battery(Protocol):
    capacity_kwh: float
    minimum_kwh: float
    charge_curve_kwh: Sequence[float]
    discharge_curve_kwh: Sequence[float]
    charge_efficiency: float
    discharge_efficiency: float
    can_charge_from: int


def _interpolate(curve: Sequence[float], fraction: float) -> float:
    position = min(max(fraction, 0.0), 1.0) * (len(curve) - 1)
    lower = int(position)
    upper = min(lower + 1, len(curve) - 1)
    return float(curve[lower] + (curve[upper] - curve[lower]) * (position - lower))


def battery_power_limits(entity: Battery, level: float) -> tuple[float, float]:
    """Return charge input and delivered discharge limits in kWh per slot."""
    fraction = float(level) / max(float(entity.capacity_kwh), EPSILON)
    return (
        max(0.0, _interpolate(entity.charge_curve_kwh, fraction)),
        max(0.0, _interpolate(entity.discharge_curve_kwh, fraction)),
    )


def grid_charge_headroom_sufficient(
    entity: Battery, level: float, limit: float
) -> bool:
    room = max(float(entity.capacity_kwh) - level, 0.0) / entity.charge_efficiency
    return limit > EPSILON and room + EPSILON >= MIN_GRID_CHARGE_SLOT_FRACTION * limit


@dataclass(frozen=True)
class Transition:
    levels: list[float]
    grid_import: float
    grid_export: float
    pv_charge: list[float]
    grid_charge: list[float]
    discharge: list[float]
    loss: float
    external_energy: list[float] | None = None
    external_shortfall: list[float] | None = None
    levels_before_external: list[float] | None = None

    @property
    def charge(self) -> float:
        return sum(self.pv_charge) + sum(self.grid_charge)


def transition(
    batteries: Sequence[Battery],
    levels: Sequence[float],
    modes: Sequence[str],
    *,
    usage: float,
    pv: float,
    slot_fraction: float = 1.0,
    allocation: Literal["proportional", "sequential"] = "proportional",
    grid_eligible: Sequence[bool] | None = None,
    connected: Sequence[bool] | None = None,
) -> Transition:
    """Apply ordered PV charging, deficit discharge, then allowed grid charging.

    ``grid_eligible`` may latch the full-slot headroom test at an action boundary
    when a caller splits that action at measurement or replanning boundaries.
    Historical self-consumption explicitly uses its legacy sequential allocation.
    Remaining PV surplus prevents grid charging: a single site cannot import
    grid-labeled battery energy while exporting that surplus. Netting the flows
    would instead charge a grid-only battery from a forbidden PV source.
    """
    if len(batteries) != len(levels) or len(batteries) != len(modes):
        raise ValueError("Battery state and policy dimensions differ")
    if allocation not in {"proportional", "sequential"}:
        raise ValueError("Unknown discharge allocation")
    if any(mode not in POLICIES for mode in modes):
        raise ValueError("Unknown battery policy")
    if not all(math.isfinite(float(v)) for v in (usage, pv, slot_fraction, *levels)):
        raise ValueError("Simulation inputs must be finite")
    if usage < 0 or pv < 0 or not 0 < slot_fraction <= 1:
        raise ValueError("Invalid energy or step duration")
    if any(
        level < 0 or level > b.capacity_kwh + EPSILON
        for b, level in zip(batteries, levels)
    ):
        raise ValueError("Battery level outside physical capacity")
    next_levels = [float(level) for level in levels]
    if connected is not None and (len(connected) != len(batteries)
            or any(type(v) is not bool for v in connected)):
        raise ValueError("Battery connection mask must contain one boolean per battery")
    limits = [battery_power_limits(b, level) for b, level in zip(batteries, levels)]
    if connected is not None:
        limits = [limit if on else (0.0, 0.0) for limit, on in zip(limits, connected)]
    eligible = (
        grid_eligible
        if grid_eligible is not None
        else [
            grid_charge_headroom_sufficient(b, level, limit[0])
            for b, level, limit in zip(batteries, levels, limits)
        ]
    )
    if len(eligible) != len(batteries):
        raise ValueError("Battery eligibility dimensions differ")
    surplus, deficit = max(pv - usage, 0.0), max(usage - pv, 0.0)
    pv_charge = [0.0] * len(batteries)
    grid_charge = [0.0] * len(batteries)
    discharge = [0.0] * len(batteries)
    for i, b in enumerate(batteries):
        if b.can_charge_from & 2 and surplus > EPSILON and (connected is None or connected[i]):
            pv_charge[i] = min(
                surplus,
                limits[i][0] * slot_fraction,
                max(b.capacity_kwh - next_levels[i], 0.0) / b.charge_efficiency,
            )
            next_levels[i] += pv_charge[i] * b.charge_efficiency
            surplus -= pv_charge[i]
    if deficit > EPSILON:
        requests = [
            min(
                battery_power_limits(b, next_levels[i])[1] * slot_fraction,
                max(next_levels[i] - b.minimum_kwh, 0.0) * b.discharge_efficiency,
            )
            if modes[i] == "self_consume"
            and (connected is None or connected[i])
            else 0.0
            for i, b in enumerate(batteries)
        ]
        total = sum(requests)
        scale = min(1.0, deficit / total) if total > EPSILON else 0.0
        for i, b in enumerate(batteries):
            discharge[i] = (
                requests[i] * scale
                if allocation == "proportional"
                else min(requests[i], deficit)
            )
            if discharge[i] > 0:
                next_levels[i] = max(
                    b.minimum_kwh,
                    next_levels[i] - discharge[i] / b.discharge_efficiency,
                )
                deficit = max(0.0, deficit - discharge[i])
    for i, b in enumerate(batteries):
        if (
            modes[i] == "grid_charge"
            and b.can_charge_from & 1
            and (connected is None or connected[i])
            and eligible[i]
            and surplus <= EPSILON
        ):
            grid_charge[i] = min(
                max(limits[i][0] * slot_fraction - pv_charge[i], 0.0),
                max(b.capacity_kwh - next_levels[i], 0.0) / b.charge_efficiency,
            )
            next_levels[i] += grid_charge[i] * b.charge_efficiency
        next_levels[i] = min(next_levels[i], b.capacity_kwh)
    loss = sum(
        (pv_charge[i] + grid_charge[i]) * (1 - b.charge_efficiency)
        + discharge[i] * (1 / b.discharge_efficiency - 1)
        for i, b in enumerate(batteries)
    )
    return Transition(
        next_levels,
        deficit + sum(grid_charge),
        surplus,
        pv_charge,
        grid_charge,
        discharge,
        loss,
    )


def transition_at(batteries, levels, modes, *, slot: int, usage, pv,
                  slot_fraction=1.0, apply_external=True, grid_eligible=None):
    """Apply slot availability, household exchange, then one boundary DC draw.

    Fractional callers set apply_external only when ending the original slot.
    External energy is never household import, inverter throughput or loss.
    Departure requirements inspect levels_before_external, before traction.
    """
    if type(slot) is not int or slot < 0:
        raise ValueError("slot must be a nonnegative integer")
    connected = [getattr(b, "connected_slots", None) for b in batteries]
    mask = [values[slot] if values is not None else True for values in connected]
    draws = [(getattr(b, "external_energy_kwh", None) or ())[slot]
             if getattr(b, "external_energy_kwh", None) is not None and apply_external else 0.0
             for b in batteries]
    if any(type(value) is not bool for value in mask):
        raise ValueError("Battery connection mask must contain booleans")
    if any(type(draw) not in {int, float} or not math.isfinite(draw) or draw < 0 for draw in draws):
        raise ValueError("External energy draws must be finite and nonnegative")
    step = transition(batteries, levels, modes, usage=usage, pv=pv,
        slot_fraction=slot_fraction, grid_eligible=grid_eligible,
        connected=mask if not all(mask) else None)
    if all(mask) and not any(draws):
        return step
    before = step.levels
    served = [min(level, draw) for level, draw in zip(before, draws)]
    return replace(step, levels=[level - draw for level, draw in zip(before, served)],
        levels_before_external=before, external_energy=served,
        external_shortfall=[draw - taken for draw, taken in zip(draws, served)])
