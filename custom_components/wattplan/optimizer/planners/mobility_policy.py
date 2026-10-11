"""Mobility-aware passive control and bounded joint fleet economic search.

Only supplied connection windows, departure requirements and end-slot DC draws
enter planning. A deterministic first-available charging seed remains a complete
candidate even when beam pruning/budgets discard useful prefixes. Joint actions
use the shared physical allocation rules, rather than independently allocating
the same PV to several batteries. Neither model proves service feasibility or
optimality; reported departure misses/unserved draws remain authoritative.
"""

from dataclasses import dataclass
from itertools import product
import math

from ..physics import EPSILON, transition_at
from .budget import SearchBudget
from .contract import PlannerCapabilities
from .evaluation import build_result, evaluate_schedule, target_miss
from .recovery import reserve_shortfall_cost
from .shared import wrap_battery_planner


def _mobile(battery):
    # Explicit inert arrays still identify mobile control in a horizon with no
    # visible trip; legacy capability checks retain their separate active rule.
    return (battery.connected_slots is not None or battery.external_energy_kwh is not None
            or bool(battery.departure_requirements))


def _connected(battery, slot):
    return battery.connected_slots is None or battery.connected_slots[slot]


def _draw(battery, slot):
    return 0.0 if battery.external_energy_kwh is None else battery.external_energy_kwh[slot]


def _need(battery, slot, horizon):
    """Energy for the next departure and intervening draws, or remaining draws."""
    departures = [r for r in battery.departure_requirements if r.timeslot >= slot]
    if departures:
        deadline = min(r.timeslot for r in departures)
        minimum = max(r.minimum_kwh for r in departures if r.timeslot == deadline)
        return max(battery.minimum_kwh,
            minimum + sum(_draw(battery, t) for t in range(slot, deadline)),
            sum(_draw(battery, t) for t in range(slot, deadline+1)))
    return max(battery.minimum_kwh, sum(_draw(battery, t) for t in range(slot, horizon)))


def _service_actions(params, levels, slot):
    actions = []
    for battery, level in zip(params.battery_entities, levels, strict=True):
        if not _connected(battery, slot):
            actions.append("preserve")
        elif _mobile(battery):
            need = _need(battery, slot, len(params.usage_kwh))
            actions.append("grid_charge" if level < need - EPSILON and battery.can_charge_from & 1 else "preserve")
        else:
            actions.append("self_consume")
    return tuple(actions)


def _service_schedule(params):
    levels = [b.initial_kwh for b in params.battery_entities]
    policies = [[] for _ in levels]
    for slot in range(len(params.usage_kwh)):
        actions = _service_actions(params, levels, slot)
        for row, action in zip(policies, actions, strict=True):
            row.append(action)
        levels = transition_at(params.battery_entities, levels, actions, slot=slot,
            usage=params.usage_kwh[slot], pv=params.solar_input_kwh[slot]).levels
    return policies


@dataclass(frozen=True, slots=True)
class _Node:
    levels: tuple[float, ...]
    previous: tuple[int, ...]
    mobility_miss: float = 0.0
    target_miss: float = 0.0
    cost: float = 0.0
    parent: "_Node | None" = None
    actions: tuple[str, ...] = ()


def _advance(params, node, actions, slot):
    step = transition_at(params.battery_entities, node.levels, actions, slot=slot,
        usage=params.usage_kwh[slot], pv=params.solar_input_kwh[slot])
    modes = tuple(1 if charge > EPSILON and charge >= params.action_deadband_kwh
        else -1 if discharge > EPSILON and discharge >= params.action_deadband_kwh else 0
        for charge, discharge in zip((g+p for g,p in zip(step.grid_charge, step.pv_charge, strict=True)), step.discharge, strict=True))
    before = step.levels_before_external if step.levels_before_external is not None else step.levels
    mobility = sum(step.external_shortfall or ()) + sum(
        max(requirement.minimum_kwh - before[index], 0.0)
        for index, battery in enumerate(params.battery_entities)
        for requirement in battery.departure_requirements if requirement.timeslot == slot
        if requirement.minimum_kwh - before[index] > EPSILON)
    missed = sum(target_miss(b.target, step.levels[index])
        for index,b in enumerate(params.battery_entities) if b.target is not None and b.target.timeslot == slot)
    objective = (step.grid_import*params.grid_import_price_per_kwh[slot]
        - step.grid_export*params.grid_export_price_per_kwh[slot]
        + (step.charge+sum(step.discharge))*params.throughput_cost_per_kwh
        + sum(a != b for a,b in zip(modes,node.previous,strict=True))*params.mode_switch_cost
        + reserve_shortfall_cost(params.battery_entities, step.levels))
    return _Node(tuple(step.levels), modes, node.mobility_miss+mobility,
        node.target_miss+missed, node.cost+objective, node, actions)


def _joint_actions(params, node, slot, limit):
    choices = [("preserve",) if not _connected(b, slot) else
        ("self_consume", "preserve", "grid_charge") if b.can_charge_from & 1 else
        ("self_consume", "preserve") for b in params.battery_entities]
    seed = _service_actions(params, node.levels, slot)
    seen = set()
    def candidates():
        yield seed
        for mode in ("self_consume", "preserve", "grid_charge"):
            yield tuple(mode if mode in row else "preserve" for row in choices)
        for index,row in enumerate(choices):
            for mode in row:
                yield (*seed[:index], mode, *seed[index+1:])
        yield from product(*choices)
    for actions in candidates():
        if actions not in seen:
            seen.add(actions)
            yield actions
            if len(seen) >= limit:
                return


def _guidance(params):
    """Optimistic recharge opportunities; not a joint feasibility certificate."""
    horizon = len(params.usage_kwh)
    rows = []
    for slot in range(horizon):
        batteries = []
        for battery in params.battery_entities:
            charge = drawn = 0.0
            requirements = []
            cheapest = math.inf
            for future in range(slot+1, horizon):
                if _connected(battery, future):
                    possible = max(battery.charge_curve_kwh)
                    if battery.can_charge_from & 1:
                        cheapest = min(cheapest, params.grid_import_price_per_kwh[future]/battery.charge_efficiency)
                    elif battery.can_charge_from & 2:
                        possible = min(possible, max(params.solar_input_kwh[future]-params.usage_kwh[future],0.0))
                    else:
                        possible = 0.0
                    charge += possible*battery.charge_efficiency
                    if battery.can_charge_from & 2 and params.solar_input_kwh[future] > params.usage_kwh[future]:
                        cheapest = min(cheapest, 0.0)
                for requirement in battery.departure_requirements:
                    if requirement.timeslot == future:
                        requirements.append((requirement.minimum_kwh+drawn, charge))
                drawn += _draw(battery, future)
                if _draw(battery, future):
                    requirements.append((drawn, charge))
            batteries.append((requirements, 0.0 if cheapest == math.inf else cheapest))
        # Household energy value is only heuristic; fleet allocation remains joint.
        demand = sum(max(u-p,0.0) for u,p in zip(params.usage_kwh[slot+1:],params.solar_input_kwh[slot+1:],strict=True))
        price = max((max(p,0.0) for p in params.grid_import_price_per_kwh[slot+1:]), default=0.0)
        rows.append((batteries,demand,price))
    return rows


def _rank(params, node, slot, guidance):
    batteries, demand, price = guidance[slot]
    unavoidable = node.mobility_miss
    replacement = available = 0.0
    for index,(battery,level) in enumerate(zip(params.battery_entities,node.levels,strict=True)):
        requirements, cheapest = batteries[index]
        # Optimistic independent bounds deliberately omit saturation/competition.
        unavoidable += sum(max(required-level-charge,0.0) for required,charge in requirements)
        need = _need(battery,slot+1,len(params.usage_kwh))
        if _mobile(battery):
            replacement += max(need-level,0.0)*max(cheapest,0.0)
        available += max(level-(need if _mobile(battery) else battery.minimum_kwh),0.0)*battery.discharge_efficiency
    return (unavoidable,node.target_miss,node.cost+replacement-min(available,demand)*price)


def _policies(node, count):
    actions = []
    while node.parent is not None:
        actions.append(node.actions)
        node = node.parent
    return [[joint[index] for joint in reversed(actions)] for index in range(count)]


def factory(*, beam_width=8, max_expansions=6000, max_joint_actions=27, max_seconds=None):
    """Bounded economic search, retaining a complete mobility-first seed."""
    for name,value in (("beam_width",beam_width),("max_expansions",max_expansions),("max_joint_actions",max_joint_actions)):
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    SearchBudget(max_expansions,max_seconds)
    def plan(params):
        policies = _service_schedule(params)
        best = evaluate_schedule(params,policies)
        budget = SearchBudget(max_expansions,max_seconds)
        horizon = len(params.usage_kwh)
        beam = [_Node(tuple(b.initial_kwh for b in params.battery_entities),(0,)*len(params.battery_entities))]
        guidance = _guidance(params)
        pruned = tail = searched = 0
        for slot in range(horizon):
            candidates = {}
            stopped = False
            for node in beam:
                for actions in _joint_actions(params,node,slot,max_joint_actions):
                    if not budget.take():
                        stopped = True
                        break
                    candidate = _advance(params,node,actions,slot)
                    key = candidate.levels,candidate.previous
                    old = candidates.get(key)
                    if old is None or (candidate.mobility_miss,candidate.target_miss,candidate.cost) < (old.mobility_miss,old.target_miss,old.cost):
                        candidates[key] = candidate
                if stopped:
                    break
            if candidates:
                ordered = sorted(candidates.values(),key=lambda n:_rank(params,n,slot,guidance))
                beam = ordered[:beam_width]
                pruned += max(len(ordered)-beam_width,0)
                searched = slot+1
            if stopped:
                node = beam[0]
                for remaining in range(slot+1 if candidates else slot,horizon):
                    node = _advance(params,node,_service_actions(params,node.levels,remaining),remaining)
                    tail += 1
                beam = [node]
                break
        evaluations = 1
        for node in beam:
            candidate = _policies(node,len(params.battery_entities))
            evaluation = evaluate_schedule(params,candidate)
            evaluations += 1
            if evaluation.score < best.score:
                best,policies = evaluation,candidate
        return build_result(params,policies,evaluation=best,diagnostics={
            "termination_reason":budget.termination_reason or "search_complete",
            "expansions":budget.evaluations,"evaluations":evaluations,
            "beam_width":beam_width,"max_expansions":max_expansions,
            "max_joint_actions":max_joint_actions,"searched_slots":searched,
            "tail_transitions":tail,"beam_candidates_pruned":pruned,
            "mobility_service_seed_retained":True,
            "mobility_feasibility":"observed forecast service; pruning and discrete modes do not certify feasibility",
            "future_guidance":"optimistic independent recharge bounds; shared physics controls joint fleet allocation",
            "search_seconds":budget.diagnostics()["search_seconds"]})
    plan.capabilities = PlannerCapabilities(mobility=True)
    return wrap_battery_planner(plan)


def baseline_factory():
    """House passive; mobile batteries charge first available, then preserve."""
    def plan(params):
        return build_result(params,_service_schedule(params),diagnostics={
            "termination_reason":"deterministic_mobility_baseline","evaluations":1,
            "battery_policy":"house self-consume; mobile first-available grid charging to next service need, then preserve",
            "mobility_feasibility":"observed forecast service; no feasibility or economic optimality guarantee"})
    plan.capabilities = PlannerCapabilities(mobility=True,targets="observed",
        economic_scope="none",reserve_recovery=False,
        ignored_preferences=(*PlannerCapabilities().ignored_preferences,
            "throughput_cost_per_kwh","mode_switch_cost","action_deadband_kwh"))
    return wrap_battery_planner(plan)


def _tariff_schedule(params):
    """One cheap-slot candidate; full evaluation checks optimistic delay choices."""
    levels = [b.initial_kwh for b in params.battery_entities]
    policies = [[] for _ in levels]
    horizon = len(params.usage_kwh)
    for slot in range(horizon):
        actions = list(_service_actions(params, levels, slot))
        for index,battery in enumerate(params.battery_entities):
            if actions[index] != "grid_charge":
                continue
            service = [r.timeslot for r in battery.departure_requirements if r.timeslot >= slot]
            service.extend(t for t in range(slot,horizon) if _draw(battery,t)>0)
            deadline = min(service,default=horizon-1)
            future = [t for t in range(slot+1,deadline+1) if _connected(battery,t)]
            recharge = len(future)*max(battery.charge_curve_kwh)*battery.charge_efficiency
            need = _need(battery,slot,horizon)
            cheaper = any(params.grid_import_price_per_kwh[t] < params.grid_import_price_per_kwh[slot] for t in future)
            if cheaper and levels[index]+recharge >= need-EPSILON:
                actions[index] = "preserve"
        for row,action in zip(policies,actions,strict=True):
            row.append(action)
        levels = transition_at(params.battery_entities,levels,actions,slot=slot,
            usage=params.usage_kwh[slot],pv=params.solar_input_kwh[slot]).levels
    return policies


def tariff_factory():
    """Choose the service seed or one tariff-aware seed using two full replays."""
    def plan(params):
        baseline = _service_schedule(params)
        economic = _tariff_schedule(params)
        best = evaluate_schedule(params,baseline)
        candidate = evaluate_schedule(params,economic)
        accepted = candidate.score < best.score
        return build_result(params,economic if accepted else baseline,
            evaluation=candidate if accepted else best,diagnostics={
                "termination_reason":"fixed_two_candidate_search","evaluations":2,
                "mobility_service_seed_retained":True,"tariff_seed_accepted":accepted,
                "mobility_feasibility":"observed forecast service; optimistic delay is rejected if the baseline scores better",
                "battery_policy":"house passive; mobile first-available or cheaper connected-slot charging before supplied service"})
    plan.capabilities = PlannerCapabilities(mobility=True,targets="observed",
        economic_scope="mobility",reserve_recovery=False)
    return wrap_battery_planner(plan)
