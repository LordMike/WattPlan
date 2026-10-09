"""Forecast-only policy evaluation shared by inexpensive experimental planners.

This is an explicit policy objective, not a reimplementation of the HiGHS MILP
objective. Battery target shortfall is lexicographically prior to economic score.
Unspent terminal energy gets no invented credit; final levels remain visible.
"""

from dataclasses import dataclass
from typing import Sequence

from ..models import OptimizationParams
from ..physics import EPSILON, POLICIES, Transition, transition
from .contract import PlannerResult


@dataclass(frozen=True)
class Evaluation:
    steps: tuple[Transition, ...]
    tariff_cost: float
    throughput_kwh: float
    mode_switches: int
    target_violations: tuple[dict, ...]
    reserve_shortfalls: tuple[dict, ...]
    objective: float

    @property
    def target_miss_kwh(self) -> float:
        return sum(item["miss_kwh"] for item in self.target_violations)

    @property
    def score(self) -> tuple[float, float]:
        """Minimize target misses first, then tariff plus preference penalties."""
        return self.target_miss_kwh, self.objective


def target_miss(target, level: float) -> float:
    low, high = (
        target.soc_kwh - target.tolerance_kwh,
        target.soc_kwh + target.tolerance_kwh,
    )
    if target.mode == "at_least":
        miss = max(low - level, 0.0)
    elif target.mode == "at_most":
        miss = max(level - high, 0.0)
    else:
        miss = max(low - level, level - high, 0.0)
    return miss if miss > EPSILON else 0.0


def evaluate_schedule(
    params: OptimizationParams, policies: Sequence[Sequence[str]]
) -> Evaluation:
    """Replay battery-major policies against only this request's forecasts.

    ``policies[battery_index][slot]`` must span the supplied horizon. Reserves
    constrain discharge; an initially low reserve is reported without creating
    energy. Throughput is AC charge input plus delivered discharge. Switching
    counts changes in actual charge/idle/discharge behavior (idle initially),
    using action_deadband_kwh, rather than changes in policy names.
    """
    if params.comfort_entities:
        raise ValueError("Shared policy evaluation does not model comfort loads")
    batteries, horizon = params.battery_entities, len(params.usage_kwh)
    if len(policies) != len(batteries) or any(len(row) != horizon for row in policies):
        raise ValueError("Policies must cover every battery and forecast slot")
    for battery, row in zip(batteries, policies, strict=True):
        if any(mode not in POLICIES for mode in row):
            raise ValueError("Unknown battery policy")
        if not battery.can_charge_from & 1 and "grid_charge" in row:
            raise ValueError("Grid-charge action is forbidden for this battery")
    levels = [b.initial_kwh for b in batteries]
    reserve = [
        {"battery": b.name, "initial_shortfall_kwh": b.minimum_kwh - level}
        for b, level in zip(batteries, levels, strict=True)
        if level < b.minimum_kwh - EPSILON
    ]
    steps, violations = [], []
    cost, throughput, switches = 0.0, 0.0, 0
    previous = [0] * len(batteries)
    deadband = params.action_deadband_kwh
    for slot in range(horizon):
        step = transition(
            batteries,
            levels,
            [row[slot] for row in policies],
            usage=params.usage_kwh[slot],
            pv=params.solar_input_kwh[slot],
        )
        steps.append(step)
        levels = step.levels
        cost += step.grid_import * params.grid_import_price_per_kwh[slot]
        cost -= step.grid_export * params.grid_export_price_per_kwh[slot]
        throughput += step.charge + sum(step.discharge)
        for index, battery in enumerate(batteries):
            charge = step.grid_charge[index] + step.pv_charge[index]
            mode = (
                1
                if charge > EPSILON and charge >= deadband
                else -1
                if step.discharge[index] > EPSILON and step.discharge[index] >= deadband
                else 0
            )
            switches += mode != previous[index]
            previous[index] = mode
            target = battery.target
            if target is not None and target.timeslot == slot:
                violations.append(
                    {
                        "battery": battery.name,
                        "timeslot": slot,
                        "soc_kwh": levels[index],
                        "mode": target.mode,
                        "target_kwh": target.soc_kwh,
                        "tolerance_kwh": target.tolerance_kwh,
                        "miss_kwh": target_miss(target, levels[index]),
                    }
                )
    return Evaluation(
        tuple(steps),
        cost,
        throughput,
        switches,
        tuple(violations),
        tuple(reserve),
        cost
        + throughput * params.throughput_cost_per_kwh
        + switches * params.mode_switch_cost,
    )


def build_result(
    params: OptimizationParams,
    policies: Sequence[Sequence[str]],
    *,
    evaluation: Evaluation | None = None,
    state: str | None = None,
    diagnostics: dict | None = None,
) -> PlannerResult:
    """Build the battery simulation result; HA promotion requires its full API.

    A supplied evaluation must correspond to these exact params and policies.
    This avoids replaying the accepted candidate in a bounded search loop.
    """
    evaluation = evaluation or evaluate_schedule(params, policies)
    return {
        "entities": [
            {
                "name": battery.name,
                "type": "battery",
                "schedule": [
                    {"state": mode, "level": evaluation.steps[slot].levels[index]}
                    for slot, mode in enumerate(policies[index])
                ],
            }
            for index, battery in enumerate(params.battery_entities)
        ],
        "state": state,
        "projections": {
            "projected_cost": evaluation.tariff_cost,
            "final_soc_kwh": {
                b.name: evaluation.steps[-1].levels[index]
                for index, b in enumerate(params.battery_entities)
            },
        },
        "diagnostics": {
            **(diagnostics or {}),
            "evaluation": {
                "objective_kind": "target_miss_then_tariff_and_preferences",
                "score": list(evaluation.score),
                "throughput_kwh": evaluation.throughput_kwh,
                "mode_switches": evaluation.mode_switches,
                "target_checks": list(evaluation.target_violations),
                "reserve_shortfalls": list(evaluation.reserve_shortfalls),
                "terminal_energy_credit": 0.0,
                "optional_loads": "advisory inputs excluded from battery cost",
            },
        },
    }
