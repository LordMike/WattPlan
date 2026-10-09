"""Approximate dynamic programming with a bounded frontier of battery states.

Implementation and differences:
    Expand joint policy actions slot by slot and group states by energy bins and
    previous physical operating modes. Keep one representative per cell, ranked
    first by target miss and then economic objective; prune the frontier using
    future-demand value and target bounds. Unlike beam search, nearby energy
    states can merge. Bins choose representatives only: carried energy and every
    physical transition remain unrounded. The winning path is fully replayed.

Properties and ordering biases:
    State merging avoids repeatedly exploring similar energy histories, making
    this a natural fit for one or a few batteries with manageable joint state
    dimensions. It is approximate, not an exact Bellman optimality guarantee:
    merging can discard a slightly fuller state needed for a later deadline,
    while frontier and action limits can discard other useful paths.
    Both joint state dimensions and up to 3**B policy combinations grow
    with battery count. Coordinated actions and single-battery changes precede
    the remaining combinations; an action cap can favor those patterns and early
    battery indices. Below-reserve batteries get an initial recovery action.
    Forecast value is a pruning estimate, not a terminal-energy credit in costs.
    On budget exhaustion, only one prefix receives a heuristic tail; its replay
    and shared reporting still run outside the expansion budget.

Loads and shared behavior:
    ``shared`` schedules comfort first using self-consumption cost estimates.
    Comfort is then fixed demand for this battery-state recursion. Optional
    appliances are independently ranked afterward under the accepted policies,
    with earlier starts breaking cost ties; they are not joint DP decisions or
    automatically executed loads. Discrete actions and bounded search can miss
    targets that continuous HiGHS reaches. The frontier is rebuilt each request.

Evidence:
    The strong preset was cheapest on both October 2026 one-battery HA fixtures.
    It retained much more house-battery energy in the two-battery synthetic case,
    making raw cross-planner cost comparisons there unequal. This establishes
    neither multi-battery superiority nor a universal best preset. See
    ``docs/planners/dynamic-programming.md`` and ``parity-comparison.md`` there.
"""

from dataclasses import dataclass
from itertools import product
import math

from ..physics import EPSILON, transition
from .budget import SearchBudget
from .contract import PlannerCapabilities
from .evaluation import build_result, evaluate_schedule, target_miss
from .recovery import reserve_shortfall_cost
from .shared import wrap_battery_planner


@dataclass(frozen=True, slots=True)
class _State:
    levels: tuple[float, ...]
    previous: tuple[int, ...]
    miss: float = 0.0
    objective: float = 0.0
    parent: "_State | None" = None
    actions: tuple[str, ...] = ()

    @property
    def score(self):
        return self.miss, self.objective


def _advance(params, state, actions, slot):
    step = transition(
        params.battery_entities,
        state.levels,
        actions,
        usage=params.usage_kwh[slot],
        pv=params.solar_input_kwh[slot],
    )
    modes = tuple(
        (
            1
            if charge > EPSILON and charge >= params.action_deadband_kwh
            else -1
            if discharge > EPSILON and discharge >= params.action_deadband_kwh
            else 0
        )
        for charge, discharge in zip(
            (g + p for g, p in zip(step.grid_charge, step.pv_charge, strict=True)),
            step.discharge, strict=True,
        )
    )
    switches = sum(a != b for a, b in zip(modes, state.previous, strict=True))
    miss = sum(
        target_miss(b.target, level)
        for b, level in zip(params.battery_entities, step.levels, strict=True)
        if b.target is not None and b.target.timeslot == slot
    )
    objective = (
        step.grid_import * params.grid_import_price_per_kwh[slot]
        - step.grid_export * params.grid_export_price_per_kwh[slot]
        + (step.charge + sum(step.discharge)) * params.throughput_cost_per_kwh
        + switches * params.mode_switch_cost
        + reserve_shortfall_cost(params.battery_entities, step.levels)
    )
    return _State(
        tuple(step.levels),
        modes,
        state.miss + miss,
        state.objective + objective,
        state,
        actions,
    )


def _bin(level, quantum):
    scaled = level / quantum
    if math.isfinite(scaled):
        return math.floor(scaled)
    numerator, denominator = level.as_integer_ratio()
    q_numerator, q_denominator = float(quantum).as_integer_ratio()
    return numerator * q_denominator // (denominator * q_numerator)


def _key(state, quantum):
    # Bins identify cells only. Carried levels and every transition remain exact.
    return tuple(_bin(level, quantum) for level in state.levels), state.previous


def _actions(batteries, limit):
    """Bounded joint actions, with coordinated/single-battery moves first."""
    choices = [
        ("self_consume", "preserve", "grid_charge")
        if b.can_charge_from & 1
        else ("self_consume", "preserve")
        for b in batteries
    ]
    base = tuple("self_consume" for _ in batteries)

    def ordered():
        if any(b.initial_kwh < b.minimum_kwh - EPSILON for b in batteries):
            # Even a one-transition budget starts with an executable recovery
            # candidate. Healthy batteries keep their normal baseline action.
            yield tuple(
                ("grid_charge" if b.can_charge_from & 1 else "preserve")
                if b.initial_kwh < b.minimum_kwh - EPSILON else "self_consume"
                for b in batteries
            )
        for mode in ("self_consume", "preserve", "grid_charge"):
            yield tuple(mode if mode in row else "preserve" for row in choices)
        for index, row in enumerate(choices):
            for mode in row[1:]:
                yield (*base[:index], mode, *base[index + 1:])
        yield from product(*choices)

    seen = set()
    for actions in ordered():
        if actions not in seen:
            seen.add(actions)
            yield actions
            if len(seen) == limit:
                break


def _forecast_bounds(params, value_horizon):
    """Bounded future-demand buckets and optimistic target reachability."""
    horizon = len(params.usage_kwh)
    deficit = [
        max(u - p, 0.0)
        for u, p in zip(params.usage_kwh, params.solar_input_kwh, strict=True)
    ]
    surplus = [
        max(p - u, 0.0)
        for u, p in zip(params.usage_kwh, params.solar_input_kwh, strict=True)
    ]
    targets = {}
    for index, battery in enumerate(params.battery_entities):
        if battery.target is None:
            continue
        rows = [(0.0, 0.0, 0.0)] * horizon
        charge, discharge, cheapest = 0.0, 0.0, math.inf
        for slot in range(battery.target.timeslot, -1, -1):
            rows[slot] = charge, discharge, 0.0 if cheapest == math.inf else cheapest
            charge += (
                max(battery.charge_curve_kwh)
                if battery.can_charge_from & 1
                else min(max(battery.charge_curve_kwh), surplus[slot])
                if battery.can_charge_from & 2
                else 0.0
            ) * battery.charge_efficiency
            discharge += (
                min(max(battery.discharge_curve_kwh), deficit[slot])
                / battery.discharge_efficiency
            )
            cheapest = min(
                cheapest,
                params.grid_import_price_per_kwh[slot] / battery.charge_efficiency
                + params.throughput_cost_per_kwh,
            )
        targets[index] = rows
    buckets = [
        sorted(
            (
                (
                    max(
                        params.grid_import_price_per_kwh[t]
                        - params.throughput_cost_per_kwh,
                        0.0,
                    ),
                    deficit[t],
                )
                for t in range(slot + 1, min(horizon, slot + 1 + value_horizon))
            ),
            reverse=True,
        )
        for slot in range(horizon)
    ]
    return targets, buckets


def _rank(params, state, slot, bounds):
    """Heuristic for frontier pruning.

    Bellman cell selection uses the accumulated prefix score.
    """
    targets, buckets = bounds
    miss, charge_cost, available = state.miss, 0.0, 0.0
    for index, (battery, level) in enumerate(
        zip(params.battery_entities, state.levels, strict=True)
    ):
        available += min(
            max(level - battery.minimum_kwh, 0.0) * battery.discharge_efficiency,
            max(battery.discharge_curve_kwh) * len(buckets[slot]),
        )
        target = battery.target
        if target is None or target.timeslot <= slot:
            continue
        charge, discharge, cheapest = targets[index][slot]
        low, high = (
            target.soc_kwh - target.tolerance_kwh,
            target.soc_kwh + target.tolerance_kwh,
        )
        if target.mode != "at_most":
            miss += max(low - min(battery.capacity_kwh, level + charge), 0.0)
            charge_cost += max(low - level, 0.0) * max(cheapest, 0.0)
        if target.mode != "at_least":
            miss += max(
                max(min(level, battery.minimum_kwh), level - discharge) - high,
                0.0,
            )
    future_miss = miss - state.miss
    miss = state.miss + (future_miss if future_miss > EPSILON else 0.0)
    value = 0.0
    for future_price, deficit in buckets[slot]:
        delivered = min(available, deficit)
        value += delivered * future_price
        available -= delivered
        if available <= EPSILON:
            break
    return miss, state.objective + charge_cost - value


def _tail_actions(params, state, slot, bounds):
    """One forecast-aware physical completion per slot after search exhaustion."""
    targets, buckets = bounds
    current = params.grid_import_price_per_kwh[slot]
    future_price = buckets[slot][0][0] if buckets[slot] else 0.0
    future_demand = sum(deficit for _, deficit in buckets[slot])
    actions = []
    for index, (battery, level) in enumerate(
        zip(params.battery_entities, state.levels, strict=True)
    ):
        available = max(level - battery.minimum_kwh, 0.0) * battery.discharge_efficiency
        mode = "preserve" if current < 0 or future_price > current else "self_consume"
        replacement = current / (battery.charge_efficiency * battery.discharge_efficiency)
        if battery.can_charge_from & 1 and (
            current < 0
            or (
                max(battery.discharge_curve_kwh) > EPSILON
                and future_demand > available + EPSILON
                and replacement + 2 * params.throughput_cost_per_kwh < future_price
            )
        ):
            mode = "grid_charge"
        target = battery.target
        if target is not None and target.timeslot >= slot:
            charge, _, _ = targets[index][slot]
            low, high = (
                target.soc_kwh - target.tolerance_kwh,
                target.soc_kwh + target.tolerance_kwh,
            )
            if target.mode != "at_most" and level < low - charge + EPSILON:
                mode = "grid_charge" if battery.can_charge_from & 1 else "preserve"
            elif target.mode != "at_least" and level > high + EPSILON:
                mode = "self_consume"
            elif target.mode != "at_most" and level <= low + EPSILON:
                mode = "preserve"
            elif target.mode != "at_least" and level >= high - EPSILON:
                mode = "preserve"
        if level < battery.minimum_kwh - EPSILON:
            mode = "grid_charge" if battery.can_charge_from & 1 else "preserve"
        actions.append(mode)
    return tuple(actions)


def _policies(state, count, horizon):
    rows = [[] for _ in range(count)]
    while state.parent is not None:
        for row, action in zip(rows, state.actions, strict=True):
            row.append(action)
        state = state.parent
    for row in rows:
        row.reverse()
        assert len(row) == horizon
    return rows


def factory(
    *,
    soc_step_kwh=0.25,
    max_states=32,
    max_expansions=20000,
    max_joint_actions=81,
    value_horizon_slots=24,
    max_seconds=None,
):
    """Approximate Bellman recursion in joint SoC/physical-mode cells."""
    if (
        type(soc_step_kwh) not in {int, float}
        or not math.isfinite(soc_step_kwh)
        or soc_step_kwh <= 0
    ):
        raise ValueError("soc_step_kwh must be finite and positive")
    for name, value in (
        ("max_states", max_states),
        ("max_expansions", max_expansions),
        ("max_joint_actions", max_joint_actions),
        ("value_horizon_slots", value_horizon_slots),
    ):
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    SearchBudget(max_expansions, max_seconds)
    capabilities = PlannerCapabilities()

    def plan(params):
        notes = capabilities.validate(params)
        budget = SearchBudget(max_expansions, max_seconds)
        batteries, horizon = params.battery_entities, len(params.usage_kwh)
        frontier = [
            _State(tuple(b.initial_kwh for b in batteries), (0,) * len(batteries))
        ]
        bounds = _forecast_bounds(params, value_horizon_slots)
        actions = tuple(_actions(batteries, min(max_joint_actions, max_expansions)))
        action_space = math.prod(3 if b.can_charge_from & 1 else 2 for b in batteries)
        merges, pruned, peak, searched, tail = 0, 0, 1, 0, 0
        reason = "search_complete"
        for slot in range(horizon):
            cells = {}
            stopped = False
            for state in frontier:
                for joint in actions:
                    if not budget.take():
                        stopped = True
                        break
                    candidate = _advance(params, state, joint, slot)
                    key = _key(candidate, soc_step_kwh)
                    previous = cells.get(key)
                    if previous is not None:
                        merges += 1
                    if previous is None or candidate.score < previous.score:
                        cells[key] = candidate
                if stopped:
                    break
            peak = max(peak, len(cells))
            if cells:
                ordered = sorted(
                    cells.values(),
                    key=lambda state: _rank(params, state, slot, bounds),
                )
                frontier = ordered[:max_states]
                pruned += max(len(ordered) - max_states, 0)
                searched = slot + 1
            if stopped:
                reason = budget.termination_reason
                state = frontier[0]
                for remaining in range(slot + 1 if cells else slot, horizon):
                    state = _advance(
                        params,
                        state,
                        _tail_actions(params, state, remaining, bounds),
                        remaining,
                    )
                    tail += 1
                frontier = [state]
                break
        state = min(frontier, key=lambda state: state.score)
        policies = _policies(state, len(batteries), horizon)
        evaluation = evaluate_schedule(params, policies)
        if not (
            math.isclose(state.miss, evaluation.target_miss_kwh, abs_tol=1e-9)
            and math.isclose(state.objective, evaluation.objective, abs_tol=1e-9)
        ):
            raise RuntimeError("DP prefix score disagrees with shared evaluation")
        return build_result(
            params,
            policies,
            evaluation=evaluation,
            diagnostics={
                "algorithm": "soc_state_dynamic_programming",
                "approximate": True,
                "soc_step_kwh": soc_step_kwh,
                "max_states": max_states,
                "value_horizon_slots": value_horizon_slots,
                "max_expansions": max_expansions,
                "expansions": budget.evaluations,
                "evaluations": 1,
                "replay_transitions": horizon,
                "tail_transitions": tail,
                "termination_reason": reason,
                "search_seconds": budget.diagnostics()["search_seconds"],
                "merged_candidates": merges,
                "frontier_cells_pruned": pruned,
                "peak_candidate_cells": peak,
                "joint_action_space": action_space,
                "joint_actions_considered": len(actions),
                "joint_actions_pruned_per_state": action_space - len(actions),
                "max_joint_actions": max_joint_actions,
                "searched_slots": searched,
                "forecast_horizon_slots": horizon,
                "ignored_preferences": list(capabilities.ignored_preferences),
                "capability_notes": notes,
            },
        )

    plan.capabilities = capabilities
    return wrap_battery_planner(plan)
