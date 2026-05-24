"""Pure historical cost simulation logic."""

from __future__ import annotations

from dataclasses import dataclass


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
    return (grid_import * import_price) - (grid_export * export_price)


def no_battery_flows(*, usage: float, pv: float) -> tuple[float, float]:
    """Return grid import/export if no battery existed."""
    return max(usage - pv, 0.0), max(pv - usage, 0.0)


def no_battery_cost(
    *,
    usage: float,
    pv: float,
    import_price: float,
    export_price: float,
) -> float:
    """Return no-battery scenario cost for one slot."""
    grid_import, grid_export = no_battery_flows(usage=usage, pv=pv)
    return actual_cost(
        grid_import=grid_import,
        grid_export=grid_export,
        import_price=import_price,
        export_price=export_price,
    )


def simulate_self_consumption_slot(
    *,
    usage: float,
    pv: float,
    batteries: list[BatterySimulationConfig],
    soc_by_battery: dict[str, float],
) -> SelfConsumptionSimulationResult:
    """Simulate one PV-first self-consumption slot."""
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
        max_output = battery.max_discharge_kwh * efficiency
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
