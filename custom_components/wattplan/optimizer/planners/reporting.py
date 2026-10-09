"""Solver-neutral projections and advisory load ranking from accepted physics.

Reporting does not optimize the schedule again. Optional loads keep the published
battery policies fixed and include their downstream effect on battery energy.
"""

from time import perf_counter
from typing import TYPE_CHECKING, Sequence

from ..models import OptimizationParams
from ..physics import EPSILON, transition

if TYPE_CHECKING:
    from .evaluation import Evaluation


# Preserve the established API value for an undefined import-weighted price.
AVG_PRICE_SENTINEL = 1000.0


def cost_projection(
    params: OptimizationParams, projected_cost_per_slot: Sequence[float]
) -> dict:
    """Compare accepted costs with the same forecast without controlled loads.

    Comfort finalizers pass the original params here to exclude comfort from
    the baseline, while keeping it in the accepted projected cost.
    """
    per_slot = []
    for usage, pv, buy, sell, projected in zip(
        params.usage_kwh,
        params.solar_input_kwh,
        params.grid_import_price_per_kwh,
        params.grid_export_price_per_kwh,
        projected_cost_per_slot,
        strict=True,
    ):
        baseline = max(usage - pv, 0.0) * buy - max(pv - usage, 0.0) * sell
        savings = baseline - projected
        per_slot.append(
            {
                "baseline_cost": baseline,
                "projected_cost": projected,
                "projected_savings_cost": savings,
                "projected_savings_pct": (
                    100.0 * savings / baseline if baseline > EPSILON else 0.0
                ),
            }
        )
    baseline = sum(slot["baseline_cost"] for slot in per_slot)
    projected = sum(projected_cost_per_slot)
    savings = baseline - projected
    return {
        "baseline_cost": baseline,
        "projected_cost": projected,
        "projected_savings_cost": savings,
        "projected_savings_pct": (
            100.0 * savings / baseline if baseline > EPSILON else 0.0
        ),
        "per_slot": per_slot,
    }


def optional_entity_options(
    params: OptimizationParams,
    policies: Sequence[Sequence[str]],
    evaluation: "Evaluation",
) -> list[dict]:
    """Rank appliance starts by fixed-policy incremental full-horizon cost.

    Every candidate starts from the accepted prefix state. After the appliance
    ends, an exactly reconverged battery state allows reuse of the remaining
    accepted suffix. Approximate equality is deliberately insufficient: a tiny
    energy difference can change a later grid-charge eligibility decision.
    """
    if not params.optional_entities:
        return []
    horizon = len(evaluation.steps)
    modes = [tuple(row[slot] for row in policies) for slot in range(horizon)]
    baseline_costs = [
        step.grid_import * buy - step.grid_export * sell
        for step, buy, sell in zip(
            evaluation.steps,
            params.grid_import_price_per_kwh,
            params.grid_export_price_per_kwh,
            strict=True,
        )
    ]
    options = []
    for entity in params.optional_entities:
        duration = entity.duration_timeslots
        candidates = []
        latest = min(entity.start_before_timeslot, horizon) - duration
        for start in range(entity.start_after_timeslot, latest + 1):
            levels = (
                evaluation.steps[start - 1].levels
                if start
                else [b.initial_kwh for b in params.battery_entities]
            )
            incremental = 0.0
            end = start + duration
            for slot in range(start, horizon):
                extra = entity.energy_kwh[slot - start] if slot < end else 0.0
                step = transition(
                    params.battery_entities,
                    levels,
                    modes[slot],
                    usage=params.usage_kwh[slot] + extra,
                    pv=params.solar_input_kwh[slot],
                )
                incremental += (
                    step.grid_import * params.grid_import_price_per_kwh[slot]
                    - step.grid_export * params.grid_export_price_per_kwh[slot]
                    - baseline_costs[slot]
                )
                levels = step.levels
                if slot + 1 >= end and levels == evaluation.steps[slot].levels:
                    break
            candidates.append((start, incremental))
        candidates.sort(key=lambda item: (item[1], item[0]))
        gap = max(
            entity.min_option_gap_timeslots,
            0 if entity.allow_overlapping_options else duration,
        )
        selected = []
        for start, cost in candidates:
            if any(abs(start - previous) < gap for previous, _ in selected):
                continue
            selected.append((start, cost))
            if len(selected) == entity.options:
                break
        best = selected[0][1] if selected else 0.0
        options.append(
            {
                "name": entity.name,
                "options": [
                    {
                        "start_timeslot": start,
                        "end_timeslot": start + duration,
                        "incremental_cost": cost,
                        "delta_from_best": cost - best,
                    }
                    for start, cost in selected
                ],
            }
        )
    return options


def enrich_result(
    params: OptimizationParams,
    policies: Sequence[Sequence[str]],
    evaluation: "Evaluation",
    result: dict,
) -> dict:
    """Add the HA reporting contract without fabricated solve counters.

    ``overconstrained`` preserves the existing API's unmet-plan meaning; it is
    not a proof of infeasibility. The caller measures complete execution_time.
    """
    started = perf_counter()
    costs = []
    flows = []
    reserve_misses = []
    for slot, step in enumerate(evaluation.steps):
        costs.append(
            step.grid_import * params.grid_import_price_per_kwh[slot]
            - step.grid_export * params.grid_export_price_per_kwh[slot]
        )
        flows.append(
            {
                "load_kwh": params.usage_kwh[slot] + step.charge - sum(step.discharge),
                "base_load_kwh": params.usage_kwh[slot],
                "comfort_load_kwh": 0.0,
                "grid_import_kwh": step.grid_import,
                "grid_export_kwh": step.grid_export,
                "grid_charge_kwh": sum(step.grid_charge),
            }
        )
        for battery, level in zip(params.battery_entities, step.levels, strict=True):
            if level < battery.minimum_kwh - EPSILON:
                reserve_misses.append(
                    {
                        "battery": battery.name,
                        "timeslot": slot,
                        "soc_kwh": level,
                        "minimum_kwh": battery.minimum_kwh,
                        "miss_kwh": battery.minimum_kwh - level,
                    }
                )
    reasons = set(result.get("suboptimal_reasons", []))
    if reserve_misses:
        reasons.add("battery_min_unmet")
    if any(check["miss_kwh"] > EPSILON for check in evaluation.target_violations):
        reasons.add("battery_target_unmet")
    reasons = sorted(reasons)
    imported = sum(step.grid_import for step in evaluation.steps)
    optional_started = perf_counter()
    options = optional_entity_options(params, policies, evaluation)
    optional_seconds = perf_counter() - optional_started
    result.update(
        fitness=evaluation.objective,
        avg_price=(
            evaluation.tariff_cost / imported
            if imported > EPSILON
            else AVG_PRICE_SENTINEL
        ),
        projections={**result.get("projections", {}), **cost_projection(params, costs)},
        energy_flows={"per_slot": flows},
        optional_entity_options=options,
        suboptimal=bool(reasons),
        overconstrained=bool(reasons),
        suboptimal_reasons=reasons,
        problems=reasons.copy(),
    )
    reporting = result.setdefault("diagnostics", {}).setdefault("evaluation", {})
    reporting.update(
        reserve_checks=reserve_misses,
        optional_loads="advisory fixed-policy incremental cost; excluded from plan cost",
        fitness_kind="tariff_plus_reserve_recovery_and_throughput_and_switch_preferences",
        constraint_status="observed_plan_violations_not_infeasibility_proof",
    )
    result["diagnostics"]["reporting"] = {
        "optional_ranking_seconds": optional_seconds,
        "reporting_seconds": perf_counter() - started,
    }
    return result
