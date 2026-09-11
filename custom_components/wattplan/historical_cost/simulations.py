"""Pure historical cost simulation logic."""

from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True, slots=True)
class BatterySimulationConfig:
    """Battery settings used by historical self-consumption simulation."""

    subentry_id: str
    minimum_kwh: float
    capacity_kwh: float
    max_charge_kwh: float
    max_discharge_kwh: float
    charge_efficiency: float
    discharge_efficiency: float
    can_charge_from_pv: bool

    def __post_init__(self) -> None:
        """Reject invalid numeric configuration before simulation."""
        numeric_values = (
            self.minimum_kwh,
            self.capacity_kwh,
            self.max_charge_kwh,
            self.max_discharge_kwh,
            self.charge_efficiency,
            self.discharge_efficiency,
        )
        if not all(math.isfinite(float(value)) for value in numeric_values):
            raise ValueError("Battery simulation configuration must be finite")
        if self.minimum_kwh < 0.0 or self.capacity_kwh < self.minimum_kwh:
            raise ValueError("Battery simulation energy bounds are invalid")
        if self.max_charge_kwh < 0.0 or self.max_discharge_kwh < 0.0:
            raise ValueError("Battery simulation power limits must be nonnegative")
        if not 0.0 < self.charge_efficiency <= 1.0:
            raise ValueError("Battery simulation charge efficiency must be in (0, 1]")
        if not 0.0 < self.discharge_efficiency <= 1.0:
            raise ValueError("Battery simulation discharge efficiency must be in (0, 1]")


@dataclass(frozen=True, slots=True)
class SelfConsumptionSimulationResult:
    """Result of one self-consumption simulation slot."""

    grid_import: float
    grid_export: float
    soc_by_battery: dict[str, float]


def actual_cost(
    *,
    grid_import: float,
    grid_export: float,
    import_price: float,
    export_price: float,
) -> float:
    """Return measured net cost for one slot."""
    values = (grid_import, grid_export, import_price, export_price)
    if not all(math.isfinite(float(value)) for value in values):
        raise ValueError("Historical cost inputs must be finite")
    result = (grid_import * import_price) - (grid_export * export_price)
    if not math.isfinite(result):
        raise ValueError("Historical cost result must be finite")
    return result


def grid_only_cost(
    *,
    usage: float,
    import_price: float,
) -> float:
    """Return grid-only scenario cost for one slot."""
    if not math.isfinite(float(usage)) or not math.isfinite(float(import_price)):
        raise ValueError("Historical cost inputs must be finite")
    result = usage * import_price
    if not math.isfinite(result):
        raise ValueError("Historical cost result must be finite")
    return result


def simulate_self_consumption_slot(
    *,
    usage: float,
    pv: float,
    batteries: list[BatterySimulationConfig],
    soc_by_battery: dict[str, float],
) -> SelfConsumptionSimulationResult:
    """Simulate one PV-first self-consumption slot."""
    if not math.isfinite(float(usage)) or not math.isfinite(float(pv)):
        raise ValueError("Historical simulation inputs must be finite")
    if usage < 0.0 or pv < 0.0:
        raise ValueError("Historical simulation energy inputs must be nonnegative")
    if any(
        not math.isfinite(float(value))
        for value in soc_by_battery.values()
    ):
        raise ValueError("Historical simulation state must be finite")
    surplus = max(pv - usage, 0.0)
    deficit = max(usage - pv, 0.0)
    next_soc = dict(soc_by_battery)

    for battery in batteries:
        if surplus <= 0.0 or not battery.can_charge_from_pv:
            continue
        current_soc = _clamp_soc(next_soc.get(battery.subentry_id, battery.minimum_kwh), battery)
        efficiency = max(battery.charge_efficiency, 0.000001)
        capacity_room_input = max(battery.capacity_kwh - current_soc, 0.0) / efficiency
        charge_input = min(surplus, battery.max_charge_kwh, capacity_room_input)
        if charge_input <= 0.0:
            next_soc[battery.subentry_id] = current_soc
            continue
        next_soc[battery.subentry_id] = min(
            battery.capacity_kwh,
            current_soc + (charge_input * efficiency),
        )
        surplus -= charge_input

    for battery in batteries:
        if deficit <= 0.0:
            break
        current_soc = _clamp_soc(next_soc.get(battery.subentry_id, battery.minimum_kwh), battery)
        efficiency = max(battery.discharge_efficiency, 0.000001)
        available_output = max(current_soc - battery.minimum_kwh, 0.0) * efficiency
        max_output = battery.max_discharge_kwh
        output = min(deficit, available_output, max_output)
        if output <= 0.0:
            next_soc[battery.subentry_id] = current_soc
            continue
        next_soc[battery.subentry_id] = max(
            battery.minimum_kwh,
            current_soc - (output / efficiency),
        )
        deficit -= output

    for battery in batteries:
        if battery.subentry_id in next_soc:
            next_soc[battery.subentry_id] = _clamp_soc(
                next_soc[battery.subentry_id],
                battery,
            )

    return SelfConsumptionSimulationResult(
        grid_import=max(deficit, 0.0),
        grid_export=max(surplus, 0.0),
        soc_by_battery=next_soc,
    )


def _clamp_soc(value: float, battery: BatterySimulationConfig) -> float:
    """Clamp a battery SoC to configured bounds."""
    return max(battery.minimum_kwh, min(battery.capacity_kwh, float(value)))
