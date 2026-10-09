"""Forecast-only generational genetic search over complete inverter schedules."""

import math
import random

from ..physics import EPSILON, battery_power_limits, transition
from .budget import SearchBudget
from .contract import PlannerCapabilities
from .evaluation import build_result, evaluate_schedule


def _guided_seed(params, scale, guidance_slots):
    """Construct one forecast heuristic, without searching/scoring alternatives.

    Physical replay supplies levels for target protection and tariff windows.
    It is counted separately from the full objective evaluations in diagnostics.
    """
    batteries = params.battery_entities
    horizon = len(params.usage_kwh)
    prices = params.grid_import_price_per_kwh
    deficits = [max(u - p, 0.0) for u, p in zip(
        params.usage_kwh, params.solar_input_kwh, strict=True)]
    surplus = [max(p - u, 0.0) for u, p in zip(
        params.usage_kwh, params.solar_input_kwh, strict=True)]
    surplus_prefix = [0.0]
    for value in surplus:
        surplus_prefix.append(surplus_prefix[-1] + value)
    deadline_prices = []
    for battery in batteries:
        row = [math.inf] * horizon
        if battery.target is not None:
            cheapest = math.inf
            for slot in range(battery.target.timeslot, -1, -1):
                cheapest = min(cheapest, prices[slot])
                row[slot] = cheapest
        deadline_prices.append(row)
    levels = [b.initial_kwh for b in batteries]
    rows = [[] for _ in batteries]
    capacity = sum(max(b.capacity_kwh - b.minimum_kwh, EPSILON) for b in batteries)
    for slot, price in enumerate(prices):
        # A full-horizon seed still needs only one linear physical pass. These
        # buckets provide useful contiguous policies rather than random genes.
        end = min(horizon, slot + 1 + guidance_slots)
        future = range(slot + 1, end)
        future_peak = max((prices[t] for t in future if deficits[t] > EPSILON), default=price)
        valuable_demand = sum(deficits[t] for t in future if prices[t] > price / scale)
        modes = []
        for index, (b, level) in enumerate(zip(batteries, levels, strict=True)):
            share = max(b.capacity_kwh - b.minimum_kwh, EPSILON) / capacity
            available = max(level - b.minimum_kwh, 0.0) * b.discharge_efficiency
            valuable = valuable_demand * share
            future_pv = (
                (surplus_prefix[end] - surplus_prefix[slot + 1]) * share
                if b.can_charge_from & 2 else 0.0
            )
            mode = "self_consume"
            if price < 0 or (valuable > EPSILON and valuable >= available - EPSILON):
                mode = "preserve"
            replacement = price / (b.charge_efficiency * b.discharge_efficiency)
            if b.can_charge_from & 1 and (price < 0 or (
                replacement + 2 * params.throughput_cost_per_kwh < scale * future_peak
                and valuable > available + future_pv + EPSILON
            )):
                mode = "grid_charge"
            target = b.target
            if target is not None and slot <= target.timeslot:
                low = target.soc_kwh - target.tolerance_kwh
                high = target.soc_kwh + target.tolerance_kwh
                charge_limit, discharge_limit = battery_power_limits(b, level)
                if target.mode != "at_least" and level > high + EPSILON:
                    mode = "self_consume"
                elif target.mode != "at_most":
                    if level < low - EPSILON:
                        mode = "preserve"
                        remaining = target.timeslot - slot
                        optimistic = remaining * max(b.charge_curve_kwh) * b.charge_efficiency
                        cheapest = deadline_prices[index][slot]
                        if b.can_charge_from & 1 and (
                            price <= cheapest + EPSILON or level + optimistic < low + EPSILON
                        ):
                            # Avoid an exact-target overshoot when preserving is closer.
                            gain = min(charge_limit * b.charge_efficiency, b.capacity_kwh - level)
                            if target.mode == "at_least" or abs(level + gain - low) < low - level:
                                mode = "grid_charge"
                    elif (
                        level - min(discharge_limit, deficits[slot]) / b.discharge_efficiency
                        < low - EPSILON
                    ):
                        mode = "preserve"
                    else:
                        mode = "self_consume"
            modes.append(mode)
        step = transition(batteries, levels, modes,
                          usage=params.usage_kwh[slot], pv=params.solar_input_kwh[slot])
        levels = step.levels
        for row, mode in zip(rows, modes, strict=True):
            row.append(mode)
    return tuple(tuple(row) for row in rows)


def _mutate(chromosome, choices, rng, horizon):
    """Block replacement, window exchange and single-gene edits keep structure."""
    rows = [list(row) for row in chromosome]
    index = rng.randrange(len(rows))
    row = rows[index]
    start = rng.randrange(horizon)
    stop = min(horizon, start + rng.randint(1, max(1, horizon // 4)))
    kind = rng.randrange(3)
    if kind == 0:
        row[start:stop] = [rng.choice(choices[index])] * (stop - start)
    elif kind == 1:
        other = rng.randrange(horizon - (stop - start) + 1)
        length = stop - start
        first, second = row[start:stop], row[other:other + length]
        row[start:stop], row[other:other + length] = second, first
    else:
        row[start] = rng.choice(choices[index])
    return tuple(tuple(row) for row in rows)


def _cross(first, second, rng, horizon):
    start = rng.randrange(horizon)
    stop = rng.randrange(start + 1, horizon + 1)
    return tuple(a[:start] + b[start:stop] + a[stop:]
                 if rng.random() < 0.75 else b
                 for a, b in zip(first, second, strict=True))


def factory(*, max_evaluations=48, population_size=8, generations=8,
            elite_count=2, tournament_size=3, mutation_rate=0.9,
            crossover_rate=0.8, guidance_slots=24, seed=0, max_seconds=None):
    """Return a stateless GA; full-horizon evaluations are the work budget."""
    SearchBudget(max_evaluations, max_seconds)
    for name, value, minimum in (("population_size", population_size, 2),
                                 ("generations", generations, 0),
                                 ("elite_count", elite_count, 1),
                                 ("tournament_size", tournament_size, 1),
                                 ("guidance_slots", guidance_slots, 1)):
        if type(value) is not int or value < minimum:
            raise ValueError(f"{name} must be an integer >= {minimum}")
    if elite_count >= population_size:
        raise ValueError("elite_count must be smaller than population_size")
    if tournament_size > population_size:
        raise ValueError("tournament_size must not exceed population_size")
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    for name, value in (("mutation_rate", mutation_rate), ("crossover_rate", crossover_rate)):
        if type(value) not in {int, float} or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f"{name} must be finite and in [0, 1]")
    capabilities = PlannerCapabilities()

    def plan(params):
        notes = capabilities.validate(params)
        rng = random.Random(seed)
        batteries, horizon = params.battery_entities, len(params.usage_kwh)
        choices = [("self_consume", "preserve", "grid_charge") if b.can_charge_from & 1
                   else ("self_consume", "preserve") for b in batteries]
        baseline = tuple(("self_consume",) * horizon for _ in batteries)
        # A complete baseline is mandatory even if the cooperative deadline has
        # elapsed while preparing the request. Reserve its one evaluation first.
        budget = SearchBudget(max_evaluations)
        budget.take()
        first = evaluate_schedule(params, baseline)
        budget.max_seconds = max_seconds
        population = [(first, baseline)]
        best = population[0]
        seen = {baseline}
        attempts, seed_evaluations, seed_transitions = 0, 1, 0
        completed, crossovers, mutations = 0, 0, 0
        peak_diversity = 1
        attempt_limit = max_evaluations * 8
        reason = "generation_limit"

        def assess(candidate):
            nonlocal attempts, best
            attempts += 1
            if candidate in seen or not budget.take():
                return None
            seen.add(candidate)
            evaluated = evaluate_schedule(params, candidate)
            individual = evaluated, candidate
            if evaluated.score < best[0].score:
                best = individual
            return individual

        if batteries:
            # Include structured economic and deadline policies before filling
            # with mutations. Every scored seed consumes the normal budget.
            for scale in (0.85, 0.65, 1.05):
                if budget.termination_reason or len(population) >= population_size:
                    break
                candidate = _guided_seed(params, scale, guidance_slots)
                seed_transitions += horizon
                individual = assess(candidate)
                if individual is not None:
                    population.append(individual)
                    seed_evaluations += 1
            for mode in ("preserve", "grid_charge"):
                if budget.termination_reason or len(population) >= population_size:
                    break
                candidate = tuple(
                    (mode if mode in row else "preserve",) * horizon for row in choices
                )
                individual = assess(candidate)
                if individual is not None:
                    population.append(individual)
                    seed_evaluations += 1
            fill_attempts = 0
            while len(population) < population_size and not budget.termination_reason:
                if fill_attempts >= population_size * 8 or attempts >= attempt_limit:
                    break
                fill_attempts += 1
                candidate = _mutate(rng.choice(population)[1], choices, rng, horizon)
                individual = assess(candidate)
                if individual is not None:
                    population.append(individual)
            peak_diversity = len(population)
            for _ in range(generations):
                if budget.termination_reason or attempts >= attempt_limit:
                    break
                population.sort(key=lambda item: item[0].score)
                next_population = population[:min(elite_count, len(population))]
                generation_attempts = 0

                def select():
                    return min(rng.sample(population, min(tournament_size, len(population))),
                               key=lambda item: item[0].score)[1]

                while len(next_population) < population_size and not budget.termination_reason:
                    if generation_attempts >= population_size * 8 or attempts >= attempt_limit:
                        break
                    generation_attempts += 1
                    candidate = select()
                    if rng.random() < crossover_rate:
                        candidate = _cross(candidate, select(), rng, horizon)
                        crossovers += 1
                    if rng.random() < mutation_rate:
                        candidate = _mutate(candidate, choices, rng, horizon)
                        mutations += 1
                    individual = assess(candidate)
                    if individual is not None:
                        next_population.append(individual)
                # Preserve elites and retain old diverse individuals if a small
                # action space prevents filling with unseen offspring.
                known = {item[1] for item in next_population}
                for item in population:
                    if len(next_population) >= population_size:
                        break
                    if item[1] not in known:
                        next_population.append(item)
                        known.add(item[1])
                population = next_population
                completed += 1
                peak_diversity = max(peak_diversity, len(known))
                if generation_attempts >= population_size * 8:
                    reason = "diversity_attempt_limit"
                    break
        else:
            reason = "no_batteries"
        if attempts >= attempt_limit:
            reason = "candidate_attempt_limit"
        evaluation, policies = best
        return build_result(params, policies, evaluation=evaluation, diagnostics={
            **budget.diagnostics(reason), "seed": seed,
            "max_evaluations": max_evaluations, "population_size": population_size,
            "generations": completed, "max_generations": generations,
            "elite_count": elite_count, "tournament_size": tournament_size,
            "seed_evaluations": seed_evaluations, "seed_transitions": seed_transitions,
            "candidate_attempts": attempts, "candidate_attempt_limit": attempt_limit,
            "diversity": len({item[1] for item in population}),
            "peak_diversity": peak_diversity, "unique_evaluated": len(seen),
            "crossovers": crossovers, "mutations": mutations,
            "baseline_score": list(first.score), "forecast_horizon_slots": horizon,
            "guidance_slots": guidance_slots,
            "ignored_preferences": list(capabilities.ignored_preferences),
            "capability_notes": notes,
        })

    plan.capabilities = capabilities
    return plan
