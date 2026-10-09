"""Search battery policies forward in time, retaining a bounded beam of prefixes.

Implementation and differences:
    Expand joint battery actions one slot at a time using shared physical
    transitions. Merge only exactly equal energy/previous-mode states, then keep
    the best ``beam_width`` prefixes according to accrued score plus forecast
    demand value and optimistic target reachability. Unlike DP, nearby energy
    levels are not binned together. Surviving complete schedules are independently
    replayed by the common evaluator before the best one is returned.

Properties and ordering biases:
    Keeping several prefixes can preserve an initially expensive charging choice
    that greedy improvement would discard. It is structurally well suited to one
    or a few batteries, where joint action enumeration is small; with B grid-
    chargeable batteries the uncapped action count is 3**B per prefix and slot.
    ``max_joint_actions`` tries coordinated actions and individual battery changes
    before the Cartesian remainder, so truncation favors those patterns and
    earlier battery indices. Reserve-recovery actions receive priority.
    Beam pruning can discard the only feasible or ultimately cheapest path.
    Guidance pools future demand and cannot perfectly value competing batteries.
    Once the expansion/time budget expires, a single chosen prefix is completed
    with heuristic tail actions, rather than searching the remaining slots.
    Tail completion, final replay and shared reporting still take time afterward.

Loads and shared behavior:
    ``shared`` fixes comfort consumption before beam expansion, using a bounded
    self-consumption placement estimate. Optional appliances are independently
    ranked after the battery plan, by incremental cost then start time; they are
    advisory and do not participate in the beam's joint action space.
    Target miss ranks before economic cost, but limited search and discrete modes
    provide no feasibility/optimality guarantee. There is no previous battery
    frontier reuse and no native solver dependency.

Configuration:
    Named presets are defined in ``presets.py``. See
    ``docs/optimizer-profiles.md`` for user-facing choices and limitations.
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
class _Node:
    levels: tuple[float, ...]
    previous: tuple[int, ...]
    cost: float = 0.0
    throughput: float = 0.0
    switches: int = 0
    miss: float = 0.0
    parent: "_Node | None" = None
    actions: tuple[str, ...] = ()
    reserve_penalty: float = 0.0


def _joint_actions(batteries, limit):
    """Coordinated actions and single changes precede a lazy Cartesian tail."""
    choices = [
        ("self_consume", "preserve", "grid_charge")
        if b.can_charge_from & 1 else ("self_consume", "preserve")
        for b in batteries
    ]
    seen = set()

    def seeds():
        for mode in ("self_consume", "preserve", "grid_charge"):
            yield tuple(mode if mode in row else "preserve" for row in choices)
        base = tuple("self_consume" for _ in choices)
        for index, row in enumerate(choices):
            for mode in row[1:]:
                yield (*base[:index], mode, *base[index + 1:])
        yield from product(*choices)

    for actions in seeds():
        if actions in seen:
            continue
        seen.add(actions)
        yield actions
        if len(seen) >= limit:
            break


def _advance(params, node, actions, slot):
    step = transition(
        params.battery_entities, node.levels, actions,
        usage=params.usage_kwh[slot], pv=params.solar_input_kwh[slot],
    )
    modes = tuple(
        1 if charge > EPSILON and charge >= params.action_deadband_kwh
        else -1 if discharge > EPSILON and discharge >= params.action_deadband_kwh
        else 0
        for charge, discharge in zip(
            (g + p for g, p in zip(step.grid_charge, step.pv_charge, strict=True)),
            step.discharge, strict=True,
        )
    )
    return _Node(
        tuple(step.levels), modes,
        node.cost + step.grid_import * params.grid_import_price_per_kwh[slot]
        - step.grid_export * params.grid_export_price_per_kwh[slot],
        node.throughput + step.charge + sum(step.discharge),
        node.switches + sum(a != b for a, b in zip(modes, node.previous, strict=True)),
        node.miss + sum(
            target_miss(b.target, level)
            for b, level in zip(params.battery_entities, step.levels, strict=True)
            if b.target is not None and b.target.timeslot == slot
        ),
        node, actions,
        node.reserve_penalty + reserve_shortfall_cost(params.battery_entities, step.levels),
    )


def _objective(params, node):
    return (node.cost + node.throughput * params.throughput_cost_per_kwh
            + node.switches * params.mode_switch_cost + node.reserve_penalty)


def _recovery_actions(batteries, node, actions):
    """Try restoring deficient batteries first, within the existing action cap."""
    if not any(level < b.minimum_kwh - EPSILON
               for b, level in zip(batteries, node.levels, strict=True)):
        return actions
    recovery = tuple(
        ("grid_charge" if b.can_charge_from & 1 else "preserve")
        if level < b.minimum_kwh - EPSILON else "self_consume"
        for b, level in zip(batteries, node.levels, strict=True)
    )
    return (recovery, *(joint for joint in actions if joint != recovery))[:len(actions)]


def _guidance(params, lookahead):
    """Precompute bounded economic buckets and optimistic deadline bounds."""
    horizon = len(params.usage_kwh)
    deficit = [max(u - p, 0.0) for u, p in zip(
        params.usage_kwh, params.solar_input_kwh, strict=True)]
    surplus = [max(p - u, 0.0) for u, p in zip(
        params.usage_kwh, params.solar_input_kwh, strict=True)]
    buckets = [sorted(
        ((max(params.grid_import_price_per_kwh[t] - params.throughput_cost_per_kwh, 0.0),
          deficit[t]) for t in range(slot + 1, min(horizon, slot + 1 + lookahead))),
        reverse=True,
    ) for slot in range(horizon)]
    targets = {}
    for index, b in enumerate(params.battery_entities):
        if b.target is None:
            continue
        end = b.target.timeslot
        charge, discharge, cheapest = 0.0, 0.0, math.inf
        rows = [(0.0, 0.0, 0.0)] * horizon
        for slot in range(end, -1, -1):
            rows[slot] = (charge, discharge, 0.0 if cheapest == math.inf else cheapest)
            charge += (max(b.charge_curve_kwh) if b.can_charge_from & 1
                       else min(max(b.charge_curve_kwh), surplus[slot])
                       if b.can_charge_from & 2 else 0.0) * b.charge_efficiency
            discharge += min(max(b.discharge_curve_kwh), deficit[slot]) / b.discharge_efficiency
            cheapest = min(cheapest, params.grid_import_price_per_kwh[slot]
                           / b.charge_efficiency + params.throughput_cost_per_kwh)
        targets[index] = rows
    return buckets, targets


def _rank(params, node, slot, guidance, scale):
    buckets, targets = guidance
    unavoidable, target_cost = node.miss, 0.0
    available = 0.0
    for index, (b, level) in enumerate(zip(params.battery_entities, node.levels, strict=True)):
        available += min(max(level - b.minimum_kwh, 0.0) * b.discharge_efficiency,
                         max(b.discharge_curve_kwh) * len(buckets[slot]))
        target = b.target
        if target is not None and target.timeslot > slot:
            charge, discharge, cheapest = targets[index][slot]
            low, high = target.soc_kwh - target.tolerance_kwh, target.soc_kwh + target.tolerance_kwh
            if target.mode != "at_most":
                unavoidable += max(low - min(b.capacity_kwh, level + charge), 0.0)
                target_cost += max(low - level, 0.0) * max(cheapest, 0.0)
            if target.mode != "at_least":
                unavoidable += max(max(min(level, b.minimum_kwh), level - discharge) - high, 0.0)
    value = 0.0
    for price, demand in buckets[slot]:
        delivered = min(available, demand)
        value += delivered * price
        available -= delivered
        if available <= EPSILON:
            break
    future_miss = unavoidable - node.miss
    unavoidable = node.miss + (future_miss if future_miss > EPSILON else 0.0)
    return unavoidable, _objective(params, node) + scale * (target_cost - value)


def _tail_actions(params, node, slot, guidance, scale):
    """One cheap forecast/target-aware joint choice, without candidate search."""
    buckets, targets = guidance
    future_price = buckets[slot][0][0] if buckets[slot] else 0.0
    future_demand = sum(demand for _, demand in buckets[slot])
    price = params.grid_import_price_per_kwh[slot]
    actions = []
    for index, (b, level) in enumerate(zip(params.battery_entities, node.levels, strict=True)):
        mode = "self_consume"
        available = min(max(level - b.minimum_kwh, 0.0) * b.discharge_efficiency,
                        max(b.discharge_curve_kwh) * len(buckets[slot]))
        replacement = price / (b.charge_efficiency * b.discharge_efficiency)
        if price < 0:
            mode = "preserve"
        elif scale and future_price > price and available > 0:
            mode = "preserve"
        if b.can_charge_from & 1 and (price < 0 or (
            scale and max(b.discharge_curve_kwh) > EPSILON
            and future_demand > available + EPSILON
            and replacement + 2 * params.throughput_cost_per_kwh < future_price
        )):
            mode = "grid_charge"
        target = b.target
        if target is not None and target.timeslot >= slot:
            charge, discharge, _ = targets[index][slot]
            low = target.soc_kwh - target.tolerance_kwh
            high = target.soc_kwh + target.tolerance_kwh
            if target.mode != "at_most" and level < low - charge + EPSILON:
                mode = "grid_charge" if b.can_charge_from & 1 else "preserve"
            elif target.mode != "at_least" and level > high + EPSILON:
                mode = "self_consume"
            elif target.mode != "at_most" and level <= low + EPSILON:
                mode = "preserve"
            elif target.mode != "at_least" and level >= high - EPSILON:
                mode = "preserve"
        if level < b.minimum_kwh - EPSILON:
            mode = "grid_charge" if b.can_charge_from & 1 else "preserve"
        actions.append(mode)
    return tuple(actions)

def _policies(node, count, horizon):
    rows = [[] for _ in range(count)]
    while node.parent is not None:
        for row, action in zip(rows, node.actions, strict=True):
            row.append(action)
        node = node.parent
    for row in rows:
        row.reverse()
        assert len(row) == horizon
    return rows


def factory(*, beam_width=16, max_expansions=30000, max_joint_actions=81,
            guidance_slots=24, guidance_scale=1.0, max_seconds=None):
    """Options bound search; completing/evaluating the accepted tail is mandatory."""
    for name, value in (("beam_width", beam_width), ("max_expansions", max_expansions),
                        ("max_joint_actions", max_joint_actions), ("guidance_slots", guidance_slots)):
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    if type(guidance_scale) not in {int, float} or not math.isfinite(guidance_scale) or guidance_scale < 0:
        raise ValueError("guidance_scale must be finite and nonnegative")
    # Validate the optional cooperative deadline at factory construction.
    SearchBudget(max_expansions, max_seconds)
    capabilities = PlannerCapabilities()

    def plan(params):
        notes = capabilities.validate(params)
        budget = SearchBudget(max_expansions, max_seconds)
        batteries, horizon = params.battery_entities, len(params.usage_kwh)
        beam = [_Node(tuple(b.initial_kwh for b in batteries), (0,) * len(batteries))]
        guidance = _guidance(params, guidance_slots)
        actions = tuple(_joint_actions(batteries, min(max_joint_actions, max_expansions)))
        action_space = math.prod(3 if b.can_charge_from & 1 else 2 for b in batteries)
        pruned, searched, tail = 0, 0, 0
        reason = "search_complete"
        for slot in range(horizon):
            candidates = {}
            stopped = False
            for node in beam:
                for joint in _recovery_actions(batteries, node, actions):
                    if not budget.take():
                        stopped = True
                        break
                    candidate = _advance(params, node, joint, slot)
                    # Exact state identity avoids merging physically distinct trajectories.
                    key = candidate.levels, candidate.previous
                    old = candidates.get(key)
                    if old is None or (candidate.miss, _objective(params, candidate)) < (old.miss, _objective(params, old)):
                        candidates[key] = candidate
                if stopped:
                    break
            if candidates:
                ordered = sorted(candidates.values(), key=lambda n: _rank(params, n, slot, guidance, guidance_scale))
                beam = ordered[:beam_width]
                pruned += max(len(ordered) - beam_width, 0)
                searched = slot + 1
            if stopped:
                reason = budget.termination_reason
                # Complete one selected prefix without consuming search expansions.
                node = beam[0]
                start = slot + 1 if candidates else slot
                for remaining in range(start, horizon):
                    joint = _tail_actions(params, node, remaining, guidance, guidance_scale)
                    node = _advance(params, node, joint, remaining)
                    tail += 1
                beam = [node]
                break
        best = None
        full_evaluations = 0
        for node in beam:
            policies = _policies(node, len(batteries), horizon)
            evaluation = evaluate_schedule(params, policies)
            full_evaluations += 1
            # Prefix accumulation is independently checked against the canonical evaluator.
            if not (math.isclose(node.miss, evaluation.target_miss_kwh, abs_tol=1e-9)
                    and math.isclose(_objective(params, node), evaluation.objective, abs_tol=1e-9)):
                raise RuntimeError("Beam prefix scoring disagrees with shared evaluation")
            if best is None or evaluation.score < best[0].score:
                best = evaluation, policies
        evaluation, policies = best
        return build_result(params, policies, evaluation=evaluation, diagnostics={
            "evaluations": full_evaluations, "expansions": budget.evaluations,
            "tail_transitions": tail, "termination_reason": reason,
            "search_seconds": budget.diagnostics()["search_seconds"],
            "beam_width": beam_width, "max_expansions": max_expansions,
            "max_joint_actions": max_joint_actions, "joint_action_space": action_space,
            "joint_actions_considered": len(actions), "joint_actions_pruned_per_node": action_space - len(actions),
            "beam_candidates_pruned": pruned, "searched_slots": searched,
            "guidance_slots": guidance_slots, "guidance_scale": guidance_scale,
            "forecast_horizon_slots": horizon, "ignored_preferences": list(capabilities.ignored_preferences),
            "capability_notes": notes,
        })

    plan.capabilities = capabilities
    return wrap_battery_planner(plan)
