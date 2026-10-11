"""Bounded tariff seed selection with an explicit forecast-risk margin.

The three default seeds match local-search-3's useful construction: target and
reserve recovery, preservation, and a minimum-price tariff schedule. Grid trades
must improve the forecast objective by a configurable margin per additional AC
kWh bought. This discourages marginal trades under uncertain forecasts without
changing the inputs, physical replay, deadline priority, or reserve penalties.
An optional preservation-only seed tests holding cheap-slot energy without buying.

Every candidate uses the full supplied horizon and exact shared battery physics,
including signed import/export tariffs, charge/discharge curves, efficiencies,
source permissions, grid headroom and PV competition. Battery target misses rank
before economics; initially low reserves use the existing recovery construction
and penalty. Discrete policies and a small seed set can miss reachable exact
targets or better coordinated fleet trades. The target/reserve baseline is always
scored first, while tariff seeds apply independent thresholds to each battery and
are then replayed jointly. No arbitrary action neighborhoods or restarts run.

Comfort placement and continuity come from the shared wrapper before battery
selection. Reporting ranks optional loads afterward as independent advisory
recommendations, not a jointly optimized appliance execution schedule. Three or
four full replays bound ordinary search work; target requests also score pure
self-consumption to retain an exact target that recovery construction overshoots.
No optimizer schedule is cached across
requests. The price quantiles and acceptance margin are explicit heuristics,
without forecast calibration or any access to future actual observations. Their
benefit depends on tariff shape, forecast accuracy and the battery configuration.
"""

import math

from .budget import SearchBudget
from .contract import PlannerCapabilities
from .evaluation import build_result, evaluate_schedule
from .local_search import _initial_policy
from .shared import wrap_battery_planner


def factory(*, grid_margin_per_kwh=0.0, preserve_fraction=None, tariff_fraction=0.0):
    """Score three seeds, plus an optional quantile preservation-only seed.

    The nonnegative margin uses the same monetary units as input tariffs. It
    applies only to increased grid charging, never to a target improvement.
    ``preserve_fraction`` and ``tariff_fraction`` are price quantiles in [0, 1],
    not time fractions. The latter replaces the minimum-price buy threshold.
    """
    if (
        isinstance(grid_margin_per_kwh, bool)
        or not isinstance(grid_margin_per_kwh, (int, float))
        or not math.isfinite(grid_margin_per_kwh)
        or grid_margin_per_kwh < 0
    ):
        raise ValueError("grid_margin_per_kwh must be finite and nonnegative")
    if preserve_fraction is not None and (
        isinstance(preserve_fraction, bool)
        or not isinstance(preserve_fraction, (int, float))
        or not math.isfinite(preserve_fraction)
        or not 0 <= preserve_fraction <= 1
    ):
        raise ValueError("preserve_fraction must be None or a finite price quantile")
    if (
        isinstance(tariff_fraction, bool)
        or not isinstance(tariff_fraction, (int, float))
        or not math.isfinite(tariff_fraction)
        or not 0 <= tariff_fraction <= 1
    ):
        raise ValueError("tariff_fraction must be a finite price quantile")

    def plan(params):
        horizon = len(params.usage_kwh)
        policies = _initial_policy(params)
        has_target = any(battery.target is not None for battery in params.battery_entities)
        budget = SearchBudget(3 + int(preserve_fraction is not None) + int(has_target))
        budget.take()
        best = evaluate_schedule(params, policies)
        seen = {tuple(tuple(row) for row in policies)}
        evaluated = ["target-or-reserve"]
        accepted, rejected_margin = 0, 0

        def grid_energy(evaluation):
            return sum(sum(step.grid_charge) for step in evaluation.steps)

        def consider(label, candidate):
            nonlocal policies, best, accepted, rejected_margin
            key = tuple(tuple(row) for row in candidate)
            if key in seen or not budget.take():
                return
            seen.add(key)
            evaluated.append(label)
            evaluation = evaluate_schedule(params, candidate)
            if evaluation.score >= best.score:
                return
            extra_grid = max(grid_energy(evaluation) - grid_energy(best), 0.0)
            if (
                evaluation.target_miss_kwh == best.target_miss_kwh
                and best.objective - evaluation.objective
                <= grid_margin_per_kwh * extra_grid
            ):
                rejected_margin += 1
                return
            policies, best = candidate, evaluation
            accepted += 1

        if params.battery_entities:
            if has_target:
                # Deadline construction can overshoot an exact target that PV
                # and normal self-consumption would meet. Preserve that feasible
                # seed before considering tariff schedules, as local search does.
                consider("self-consumption", [["self_consume"] * horizon
                                              for _ in params.battery_entities])
            consider("preserve", [["preserve"] * horizon for _ in params.battery_entities])
            prices = params.grid_import_price_per_kwh
            threshold = sorted(prices)[int(tariff_fraction * (horizon - 1))]
            candidate = [
                [
                    "grid_charge"
                    if battery.can_charge_from & 1 and price <= threshold
                    else "self_consume"
                    if price * battery.charge_efficiency * battery.discharge_efficiency
                    > threshold + params.throughput_cost_per_kwh
                    else "preserve"
                    for price in prices
                ]
                for battery in params.battery_entities
            ]
            consider(f"tariff-{tariff_fraction}", candidate)
            if preserve_fraction is not None:
                threshold = sorted(prices)[int(preserve_fraction * (horizon - 1))]
                candidate = [
                    ["preserve" if price <= threshold else "self_consume"
                     for price in prices]
                    for _ in params.battery_entities
                ]
                consider("preservation-only", candidate)
        return build_result(
            params, policies, evaluation=best,
            diagnostics={
                **budget.diagnostics("bounded_seeds_complete"),
                "grid_margin_per_kwh": grid_margin_per_kwh,
                "preserve_fraction": preserve_fraction,
                "tariff_fraction": tariff_fraction,
                "seeds_evaluated": evaluated,
                "accepted": accepted,
                "rejected_grid_margin": rejected_margin,
                "scored_horizon_slots": horizon,
                "state_reuse": "stateless; current request evaluated independently",
            },
        )

    plan.capabilities = PlannerCapabilities()
    return wrap_battery_planner(plan)
