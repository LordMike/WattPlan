"""Bounded, deterministic policy hill climbing with no native solver dependency."""

import math
import random

from ..models import OptimizationParams
from ..physics import EPSILON
from .budget import SearchBudget
from .contract import PlannerCapabilities
from .evaluation import build_result, evaluate_schedule
from .recovery import reserve_recovery_seed
from .shared import wrap_battery_planner


# Limit replay work as well as candidate count on long/many-battery requests.
_MAX_SLOT_BATTERY_EVALUATIONS = 196_608
_ACTIONS = ("self_consume", "preserve", "grid_charge")


def _seeds(params):
    """Cheap policy construction only: no hidden physical/scoring replay."""
    horizon = len(params.usage_kwh)
    batteries = params.battery_entities
    yield "self-consumption", [["self_consume"] * horizon for _ in batteries]
    yield "preserve", [["preserve"] * horizon for _ in batteries]
    prices = params.grid_import_price_per_kwh
    ordered = sorted(prices)
    # Thresholds deliberately span a broad range rather than fitting a dataset.
    for fraction in (0.0, 0.15, 0.35, 0.6):
        threshold = ordered[int(fraction * (horizon - 1))]
        rows = []
        for battery in batteries:
            efficiency = battery.charge_efficiency * battery.discharge_efficiency
            rows.append(
                [
                    "grid_charge"
                    if battery.can_charge_from & 1 and price <= threshold
                    else "self_consume"
                    if price * efficiency > threshold + params.throughput_cost_per_kwh
                    else "preserve"
                    for price in prices
                ]
            )
        yield f"tariff-{fraction}", rows
        # These seeds protect target energy, then choose cheap charging slots.
        # Curves/PV/discharge interaction is always judged by the shared evaluator.
        for extra_slots in (0, 1):
            target_rows = [row.copy() for row in rows]
            for index, battery in enumerate(batteries):
                target = battery.target
                if target is None:
                    continue
                end = target.timeslot + 1
                if target.mode == "at_most":
                    target_rows[index][:end] = ["self_consume"] * end
                    continue
                target_rows[index][:end] = ["preserve"] * end
                if battery.can_charge_from & 1:
                    rate = max(battery.charge_curve_kwh) * battery.charge_efficiency
                    needed = max(target.soc_kwh - battery.initial_kwh, 0.0)
                    count = (
                        min(end, math.ceil(needed / rate) + extra_slots) if rate else 0
                    )
                    cheap = sorted(range(end), key=lambda slot: (prices[slot], slot))
                    for slot in cheap[:count]:
                        target_rows[index][slot] = "grid_charge"
            yield f"target-{fraction}-{extra_slots}", target_rows
    if any(b.target for b in batteries):
        yield (
            "target-maximum",
            [
                [
                    ("grid_charge" if b.can_charge_from & 1 else "preserve")
                    if b.target is not None and slot <= b.target.timeslot
                    else "self_consume"
                    for slot in range(horizon)
                ]
                if b.target is not None and b.target.mode != "at_most"
                else ["self_consume"] * horizon
                for b in batteries
            ],
        )


def _copy(policies):
    return [row.copy() for row in policies]


def _initial_policy(params):
    """Construct a deadline-aware first candidate without scoring hidden seeds.

    Charge-count estimates are only guidance; the counted full replay decides
    their physical result. Starting here gives even small search budgets a
    feasible immediate-target candidate before exploring tariff opportunities.
    """
    horizon = len(params.usage_kwh)
    rows = [["self_consume"] * horizon for _ in params.battery_entities]
    for index, battery in enumerate(params.battery_entities):
        target = battery.target
        if target is None:
            continue
        end = target.timeslot + 1
        if target.mode == "at_most" or (
            target.mode == "exact"
            and battery.initial_kwh > target.soc_kwh + target.tolerance_kwh
        ):
            continue
        rows[index][:end] = ["preserve"] * end
        rate = max(battery.charge_curve_kwh) * battery.charge_efficiency
        needed = max(target.soc_kwh - target.tolerance_kwh - battery.initial_kwh, 0.0)
        if not battery.can_charge_from & 1 or rate <= EPSILON or needed <= EPSILON:
            continue
        count = min(end, math.ceil((needed - EPSILON) / rate))
        cheap = sorted(range(end), key=lambda slot: (params.grid_import_price_per_kwh[slot], slot))
        for slot in cheap[:count]:
            rows[index][slot] = "grid_charge"
    return reserve_recovery_seed(params, rows)


def factory(*, max_evaluations=96, max_seconds=None, seed=0, max_restarts=2):
    """Score complete horizons; legacy lookahead/solver preferences are unused.

    Evaluation and optional cooperative time limits include initial seeds.
    Fixed seeds/count budgets are reproducible; wall deadlines are not.
    """
    SearchBudget(max_evaluations, max_seconds)  # Validate once, not during planning.
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    if type(max_restarts) is not int or max_restarts < 0:
        raise ValueError("max_restarts must be a nonnegative integer")

    def plan(params: OptimizationParams):
        horizon = len(params.usage_kwh)
        batteries = params.battery_entities
        work = horizon * max(1, len(batteries))
        effective_limit = min(
            max_evaluations, max(1, _MAX_SLOT_BATTERY_EVALUATIONS // work)
        )
        budget = SearchBudget(effective_limit)
        rng = random.Random(seed)
        best, best_eval = None, None
        seen = set()
        seeds_evaluated, improvements, restarts = [], 0, 0
        attempts, attempt_limit = 1, effective_limit * 8

        def evaluate(candidate):
            nonlocal best, best_eval, improvements, attempts
            attempts += 1
            key = tuple(tuple(row) for row in candidate)
            if key in seen or not budget.take():
                return None
            seen.add(key)
            evaluation = evaluate_schedule(params, candidate)
            if best_eval is None or evaluation.score < best_eval.score:
                best, best_eval = candidate, evaluation
                improvements += 1
            return evaluation

        # Always reserve the baseline before starting any seed construction. The
        # first unavoidable replay establishes a complete result even with tiny
        # time limits; it is counted and may cooperatively overrun that deadline.
        baseline = _initial_policy(params)
        budget.take()
        budget.max_seconds = max_seconds
        best_eval = evaluate_schedule(params, baseline)
        best = baseline
        seen.add(tuple(tuple(row) for row in baseline))
        baseline_score = best_eval.score
        seeds_evaluated.append(
            "target-or-reserve" if any(
                b.target or b.initial_kwh < b.minimum_kwh - EPSILON for b in batteries
            ) else "self-consumption"
        )
        if batteries:
            for label, candidate in _seeds(params):
                if budget.termination_reason is not None or attempts >= attempt_limit:
                    break
                if evaluate(candidate) is not None:
                    seeds_evaluated.append(label)

        current, current_eval = best, best_eval
        # Interleave coordinate moves with blocks and price/energy transfers.
        # Randomness only determines a deterministic exploration order.
        coordinates = [
            (index, slot) for slot in range(horizon) for index in range(len(batteries))
        ]
        rng.shuffle(coordinates)
        coordinates.sort(
            key=lambda item: (
                0
                if batteries[item[0]].target
                and item[1] <= batteries[item[0]].target.timeslot
                else 1,
                -abs(params.grid_import_price_per_kwh[item[1]]),
            )
        )
        cursor, stalled, iteration = 0, 0, 0
        max_block = max(1, min(horizon, 32))
        block_lengths = [1] + [
            length for length in (2, 4, 8, 16, 32) if length <= max_block
        ]
        while (
            batteries and budget.termination_reason is None and attempts < attempt_limit
        ):
            candidate = _copy(current)
            index, slot = coordinates[cursor % len(coordinates)]
            cursor += 1
            allowed = _ACTIONS if batteries[index].can_charge_from & 1 else _ACTIONS[:2]
            if iteration % 4 == 2:
                # Exchange two actions: release cheap-load energy for a dearer
                # slot or move a charge without needing an improving midpoint.
                other = rng.randrange(horizon)
                candidate[index][slot], candidate[index][other] = (
                    candidate[index][other],
                    candidate[index][slot],
                )
            else:
                length = rng.choice(block_lengths) if iteration % 4 == 3 else 1
                action = rng.choice(
                    [mode for mode in allowed if mode != current[index][slot]]
                )
                for offset in range(slot, min(slot + length, horizon)):
                    candidate[index][offset] = action
            evaluation = evaluate(candidate)
            iteration += 1
            stalled += 1
            if evaluation is not None and evaluation.score < current_eval.score:
                current, current_eval = candidate, evaluation
                stalled = 0
            if stalled >= max(32, min(128, len(coordinates) * 2)):
                if restarts >= max_restarts:
                    break
                # A bounded perturbation may cross a local basin; only the best
                # globally evaluated result is returned.
                candidate = _copy(best)
                index = rng.randrange(len(batteries))
                slot = rng.randrange(horizon)
                action = rng.choice(
                    _ACTIONS if batteries[index].can_charge_from & 1 else _ACTIONS[:2]
                )
                for offset in range(
                    slot, min(slot + rng.choice(block_lengths), horizon)
                ):
                    candidate[index][offset] = action
                evaluation = evaluate(candidate) if attempts < attempt_limit else None
                if evaluation is not None:
                    current, current_eval = candidate, evaluation
                restarts += 1
                stalled = 0
        return build_result(
            params,
            best,
            evaluation=best_eval,
            diagnostics={
                **budget.diagnostics(
                    "candidate_attempt_limit"
                    if attempts >= attempt_limit
                    else "local_search_stalled"
                    if batteries
                    else "no_batteries"
                ),
                "candidate_attempts": attempts,
                "candidate_attempt_limit": attempt_limit,
                "requested_max_evaluations": max_evaluations,
                "effective_max_evaluations": effective_limit,
                "slot_battery_work_limit": _MAX_SLOT_BATTERY_EVALUATIONS,
                "seed": seed,
                "seeds_evaluated": seeds_evaluated,
                "restarts": restarts,
                "improvements": improvements,
                "baseline_score": list(baseline_score),
                "reserve_seed_construction_passes": int(any(
                    b.initial_kwh < b.minimum_kwh - EPSILON for b in batteries
                )),
                "scored_horizon_slots": horizon,
                "lookahead_semantics": "full supplied horizon; lookahead_slots ignored",
                "state_reuse": "stateless; current request evaluated independently",
            },
        )

    plan.capabilities = PlannerCapabilities()
    return wrap_battery_planner(plan)
