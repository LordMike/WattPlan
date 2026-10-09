"""Learn categorical policy probabilities with bounded cross-entropy search.

Implementation and differences:
    Evaluate structured tariff/target seeds, then sample complete battery
    schedules from per-battery, per-slot action probabilities. Retain elite
    schedules and blend their action frequencies into the next distribution.
    A probability floor maintains exploration while forbidden grid charging
    remains masked out. Unlike the genetic planner, this fits distributions
    instead of crossing parent chromosomes; unlike local search, it samples
    complete replacements. Every scored candidate uses shared physical replay.

Properties and ordering biases:
    Sharing random draws across short temporal blocks encourages coherent runs
    without forcing every slot in a block to have the same action. Per-slot
    probabilities are compact, but they do not learn a joint distribution across
    batteries or arbitrary long-range action dependencies. Elite marginals can
    therefore lose combinations that only work together; large coupled fleets
    are not an established strength. Fixed budgets also cover less of a longer
    battery/horizon schedule, and a slot-battery cap reduces evaluations further.
    When targets exist, the compulsory first seed is a deadline recipe, with
    reserve recovery added when needed. Other seeds consume budget before random
    samples, so very small configurations may perform no distribution updates.
    Seeds approximate charging needs and can miss feasible targets. A fixed seed
    reproduces count-limited runs; changing the seed can materially change cost.
    Cooperative deadlines do not cap initial replay and shared reporting.

Loads and shared behavior:
    ``shared`` fixes comfort consumption before CEM using bounded placement under
    self-consumption. Comfort actions are not learned by the distribution.
    Optional appliances are independently ranked afterward under fixed battery
    policies, by incremental cost then start. They are advisory, not automatically
    run first or jointly scheduled. Shared target-first scoring does not overcome
    all discrete-action or search-budget limits. Search restarts every request.

Configuration:
    Named presets are defined in ``presets.py``. See
    ``docs/optimizer-profiles.md`` for user-facing choices and limitations.
"""

import math
import random

from ..physics import EPSILON
from .budget import SearchBudget
from .contract import PlannerCapabilities
from .evaluation import build_result, evaluate_schedule
from .recovery import reserve_recovery_seed
from .shared import wrap_battery_planner

_ACTIONS = ("self_consume", "preserve", "grid_charge")
_MAX_SLOT_BATTERY_EVALUATIONS = 196_608


def _seeds(params):
    """Forecast/target constructions, without hidden candidate evaluations."""
    horizon = len(params.usage_kwh)
    batteries = params.battery_entities
    prices = params.grid_import_price_per_kwh
    ordered = sorted(prices)
    yield "self-consumption", [["self_consume"] * horizon for _ in batteries]
    yield "preserve", [["preserve"] * horizon for _ in batteries]
    for fraction in (0.15, 0.35, 0.6, 0.8):
        threshold = ordered[int(fraction * (horizon - 1))]
        # Future high-value demand and PV are computed once, never per sample.
        future_demand = [0.0] * (horizon + 1)
        for slot in range(horizon - 1, -1, -1):
            deficit = max(0.0, params.usage_kwh[slot] - params.solar_input_kwh[slot])
            future_demand[slot] = future_demand[slot + 1] + (
                deficit if prices[slot] > threshold else 0.0
            )
        rows = []
        for battery in batteries:
            efficiency = battery.charge_efficiency * battery.discharge_efficiency
            row = []
            for slot, price in enumerate(prices):
                buy_price = (
                    threshold * efficiency
                    - params.throughput_cost_per_kwh * (1 + efficiency)
                )
                if (
                    battery.can_charge_from & 1
                    and (price < 0 or future_demand[slot + 1] > 0)
                    and price < buy_price
                ):
                    mode = "grid_charge"
                elif price >= threshold or slot == horizon - 1:
                    mode = "self_consume"
                else:
                    mode = "preserve"
                row.append(mode)
            rows.append(row)
        yield f"tariff-{fraction}", rows
        if any(b.target for b in batteries):
            for extra in (0, 1):
                target_rows = [row[:] for row in rows]
                for index, battery in enumerate(batteries):
                    target = battery.target
                    if target is None:
                        continue
                    end = target.timeslot + 1
                    above_target = (
                        battery.initial_kwh > target.soc_kwh + target.tolerance_kwh
                    )
                    if target.mode == "at_most" or (target.mode == "exact" and above_target):
                        target_rows[index][:end] = ["self_consume"] * end
                        continue
                    target_rows[index][:end] = ["preserve"] * end
                    if battery.can_charge_from & 1:
                        rate = max(battery.charge_curve_kwh) * battery.charge_efficiency
                        needed = max(target.soc_kwh - battery.initial_kwh, 0)
                        count = min(end, math.ceil(needed / rate) + extra) if rate else 0
                        cheap_slots = sorted(
                            range(end), key=lambda slot: (prices[slot], slot)
                        )
                        for slot in cheap_slots[:count]:
                            target_rows[index][slot] = "grid_charge"
                yield f"target-{fraction}-{extra}", target_rows
    if any(b.target for b in batteries):
        maximum = []
        for battery in batteries:
            row = ["self_consume"] * horizon
            target = battery.target
            if target and target.mode != "at_most":
                mode = "grid_charge" if battery.can_charge_from & 1 else "preserve"
                row[:target.timeslot + 1] = [mode] * (target.timeslot + 1)
            maximum.append(row)
        yield "target-maximum", maximum


def _update(probabilities, elite, *, smoothing, floor, allowed):
    """Smoothed elite categorical maximum-likelihood estimate, with a floor."""
    for index, row in enumerate(probabilities):
        modes = allowed[index]
        for slot, old in enumerate(row):
            counts = [0] * 3
            for _, candidate in elite:
                counts[_ACTIONS.index(candidate[index][slot])] += 1
            mixed = [
                (1 - smoothing) * old[mode] + smoothing * counts[mode] / len(elite)
                for mode in modes
            ]
            total = sum(mixed)
            # Affine floor preserves normalization and never unmasks grid ingress.
            for mode in range(3):
                old[mode] = 0.0
            for mode, value in zip(modes, mixed, strict=True):
                old[mode] = floor + (1 - len(modes) * floor) * value / total


def _categorical(probabilities, draw, allowed):
    cumulative = 0.0
    for mode in allowed:
        cumulative += probabilities[mode]
        if draw < cumulative:
            return mode
    # A rounding shortfall at one must never unmask forbidden grid charging.
    return allowed[-1]


def factory(
    *,
    max_evaluations=64,
    population_size=12,
    elite_fraction=0.25,
    smoothing=0.7,
    probability_floor=0.02,
    block_slots=4,
    guidance=0.85,
    seed=0,
    max_seconds=None,
):
    """Fresh fixed-seed CEM each cycle; full horizon and lexicographic objective."""
    SearchBudget(max_evaluations, max_seconds)
    for name, value, low, high in (
        ("population_size", population_size, 2, 4096),
        ("block_slots", block_slots, 1, 672),
    ):
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{name} must be an integer between {low} and {high}")
    for name, value, low, high in (
        ("elite_fraction", elite_fraction, 0, 1),
        ("smoothing", smoothing, 0, 1),
        ("probability_floor", probability_floor, 0, 1 / 3),
        ("guidance", guidance, 0, 1),
    ):
        if (
            type(value) not in {int, float}
            or not math.isfinite(value)
            or not low < value <= high
        ):
            raise ValueError(f"{name} must be finite in ({low}, {high}]")
    if type(seed) is not int:
        raise ValueError("seed must be an integer")

    def plan(params):
        horizon, batteries = len(params.usage_kwh), params.battery_entities
        physical_work = max(1, horizon * len(batteries))
        effective = min(
            max_evaluations,
            max(1, _MAX_SLOT_BATTERY_EVALUATIONS // physical_work),
        )
        # The baseline is compulsory even with an already elapsed tiny deadline.
        budget = SearchBudget(effective)
        budget.take()
        budget.max_seconds = max_seconds
        baseline = [["self_consume"] * horizon for _ in batteries]
        initial_name = "self-consumption"
        if any(b.target for b in batteries):
            # Put a deadline construction into the compulsory first evaluation.
            # This scans seed recipes only; it performs no uncounted scoring.
            initial_name, baseline = next(
                (name, candidate) for name, candidate in _seeds(params)
                if name.startswith("target-")
            )
        baseline = reserve_recovery_seed(params, baseline)
        if any(b.initial_kwh < b.minimum_kwh - EPSILON for b in batteries):
            initial_name += "+reserve-recovery"
        best_policies = baseline
        best = evaluate_schedule(params, baseline)
        baseline_score = list(best.score)
        evaluated_seeds = [(best.score, baseline)]
        names = [initial_name]
        seen = {tuple(tuple(row) for row in baseline)}
        for name, candidate in _seeds(params):
            key = tuple(tuple(row) for row in candidate)
            if key in seen:
                continue
            if not budget.take():
                break
            seen.add(key)
            evaluation = evaluate_schedule(params, candidate)
            names.append(name)
            evaluated_seeds.append((evaluation.score, candidate))
            if evaluation.score < best.score:
                best, best_policies = evaluation, candidate
        allowed = [tuple(range(3 if b.can_charge_from & 1 else 2)) for b in batteries]
        probabilities = []
        for index, policies in enumerate(best_policies):
            modes = allowed[index]
            row = []
            for policy in policies:
                favorite = _ACTIONS.index(policy)
                distribution = [0.0] * 3
                for mode in modes:
                    initial = (1 - guidance) / len(modes) + guidance * (mode == favorite)
                    distribution[mode] = (
                        probability_floor + (1 - len(modes) * probability_floor) * initial
                    )
                row.append(distribution)
            probabilities.append(row)
        rng = random.Random(seed)
        generations, attempts = 0, 0
        attempt_limit = max(8, effective * 8)
        # Elite retention preserves the best score across every generation.
        elite_count = max(1, math.ceil(population_size * elite_fraction))
        retained = sorted(evaluated_seeds, key=lambda item: item[0])[:elite_count]
        while not budget.termination_reason and attempts < attempt_limit and batteries:
            population = list(retained)
            sampled = 0
            while (
                sampled < population_size
                and not budget.termination_reason
                and attempts < attempt_limit
            ):
                attempts += 1
                candidate = []
                # Shared uniforms within blocks create temporal coherence while
                # every slot keeps its own learned categorical distribution.
                offset = rng.randrange(block_slots)
                for index, row in enumerate(probabilities):
                    result = []
                    draw = rng.random()
                    for slot, probabilities_at_slot in enumerate(row):
                        if (slot + offset) % block_slots == 0:
                            draw = rng.random()
                        mode = _categorical(probabilities_at_slot, draw, allowed[index])
                        result.append(_ACTIONS[mode])
                    candidate.append(result)
                key = tuple(tuple(row) for row in candidate)
                if key in seen:
                    continue
                if not budget.take():
                    break
                seen.add(key)
                evaluation = evaluate_schedule(params, candidate)
                population.append((evaluation.score, candidate))
                sampled += 1
                if evaluation.score < best.score:
                    best, best_policies = evaluation, candidate
            if not sampled:
                break
            retained = sorted(population, key=lambda item: item[0])[:elite_count]
            _update(
                probabilities, retained, smoothing=smoothing,
                floor=probability_floor, allowed=allowed,
            )
            generations += 1
        if attempts >= attempt_limit:
            reason = "candidate_attempt_limit"
        elif not batteries:
            reason = "empty_equipment"
        else:
            reason = "cem_complete"
        return build_result(
            params,
            best_policies,
            evaluation=best,
            diagnostics={
                **budget.diagnostics(reason),
                "effective_max_evaluations": effective,
                "seed": seed,
                "seed_candidates": names,
                "seed_construction": "target recipe and physical reserve preparation before counted evaluation",
                "baseline_score": baseline_score,
                "generations": generations,
                "candidate_attempts": attempts,
                "candidate_attempt_limit": attempt_limit,
                "population_size": population_size,
                "block_slots": block_slots,
                "forecast_horizon_slots": horizon,
                "lookahead_slots_used": False,
                "target_miss_kwh": best.target_miss_kwh,
            },
        )

    plan.capabilities = PlannerCapabilities()
    return wrap_battery_planner(plan)
