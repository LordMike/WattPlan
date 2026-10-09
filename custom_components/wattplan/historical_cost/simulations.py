"""Pure historical cost simulation logic."""

from __future__ import annotations

from dataclasses import dataclass
import math

from ..optimizer.physics import transition


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

    @property
    def charge_curve_kwh(self) -> tuple[float]:
        return (self.max_charge_kwh,)

    @property
    def discharge_curve_kwh(self) -> tuple[float]:
        return (self.max_discharge_kwh,)

    @property
    def can_charge_from(self) -> int:
        return 2 if self.can_charge_from_pv else 0

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
    # Retain historical sequential discharge and its minimum-level seed/clamp.
    # Planner counterfactuals use proportional discharge and do not clamp to reserve.
    levels = [_clamp_soc(soc_by_battery.get(b.subentry_id, b.minimum_kwh), b) for b in batteries]
    step = transition(batteries, levels, ["self_consume"] * len(batteries),
                      usage=usage, pv=pv, allocation="sequential")
    next_soc = dict(soc_by_battery)
    surplus, deficit = max(pv - usage, 0.0), max(usage - pv, 0.0)
    for i, b in enumerate(batteries):
        if b.subentry_id in next_soc or (surplus > 0 and b.can_charge_from_pv) or deficit > 0:
            next_soc[b.subentry_id] = step.levels[i]
        surplus = max(0.0, surplus - step.pv_charge[i])
        deficit = max(0.0, deficit - step.discharge[i])
    return SelfConsumptionSimulationResult(
        grid_import=step.grid_import, grid_export=step.grid_export, soc_by_battery=next_soc,
    )


def _clamp_soc(value: float, battery: BatterySimulationConfig) -> float:
    """Clamp a battery SoC to configured bounds."""
    return max(battery.minimum_kwh, min(battery.capacity_kwh, float(value)))
