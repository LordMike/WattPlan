"""Four complete causal fleet seeds, with joint physical replay throughout."""

from ..physics import EPSILON, transition_at
from .contract import PlannerCapabilities
from .evaluation import build_result, evaluate_schedule
from .hybrid_policy import factory as hybrid_factory
from .mobility_policy import (
    _connected, _draw, _mobile, _need, _service_actions,
    factory as fleet_factory,
)
from .shared import wrap_battery_planner


def _joint_schedule(params, stationary, *, tariff):
    """Recompute mobile control from shared levels, never splice trajectories."""
    horizon = len(params.usage_kwh)
    levels = [battery.initial_kwh for battery in params.battery_entities]
    policies = [[] for _ in levels]
    for slot in range(horizon):
        actions = list(_service_actions(params, levels, slot))
        for index, battery in enumerate(params.battery_entities):
            if not _mobile(battery):
                actions[index] = stationary[index][slot]
            elif tariff and actions[index] == "grid_charge":
                deadlines = [r.timeslot for r in battery.departure_requirements
                    if r.timeslot >= slot]
                deadlines.extend(t for t in range(slot, horizon) if _draw(battery, t) > 0)
                deadline = min(deadlines, default=horizon - 1)
                future = [t for t in range(slot + 1, deadline + 1)
                    if _connected(battery, t)]
                recharge = len(future) * max(battery.charge_curve_kwh) * battery.charge_efficiency
                cheaper = any(params.grid_import_price_per_kwh[t]
                    < params.grid_import_price_per_kwh[slot] for t in future)
                if cheaper and levels[index] + recharge >= _need(battery, slot, horizon) - EPSILON:
                    actions[index] = "preserve"
        for row, action in zip(policies, actions, strict=True):
            row.append(action)
        levels = transition_at(params.battery_entities, levels, actions, slot=slot,
            usage=params.usage_kwh[slot], pv=params.solar_input_kwh[slot]).levels
    return policies


def factory(*, energy_intervals=4):
    """Compare passive/hybrid stationary × first-available/tariff mobile seeds."""
    hybrid = hybrid_factory(energy_intervals=energy_intervals).__wrapped__
    fleet = fleet_factory(beam_width=4, max_expansions=1500).__wrapped__

    def plan(params):
        if any(_mobile(battery) and battery.target is not None
            for battery in params.battery_entities):
            result = fleet(params)
            result["diagnostics"].update(
                mobility_hybrid_fallback="ordinary_mobile_target",
                mobility_hybrid_search="bounded whole-fleet beam; target/service pursuit is not guaranteed",
            )
            return result

        horizon = len(params.usage_kwh)
        indices = [i for i, battery in enumerate(params.battery_entities) if not _mobile(battery)]
        passive = {i: ["self_consume"] * horizon for i in indices}
        stationary = passive
        stationary_diagnostics = None
        if indices:
            batteries = [params.battery_entities[i].model_copy(update={
                "connected_slots": None, "external_energy_kwh": None,
                "departure_requirements": [],
            }) for i in indices]
            local = params.model_copy(update={"battery_entities": batteries, "state": None,
                "comfort_entities": [], "optional_entities": []})
            result = hybrid(local)
            schedules = {entity["name"]: [entry["state"] for entry in entity["schedule"]]
                for entity in result["entities"] if entity["type"] == "battery"}
            stationary = {i: schedules[params.battery_entities[i].name] for i in indices}
            stationary_diagnostics = result.get("diagnostics")

        best = policies = selected = None
        scores = []
        for name, fixed, tariff in (
            ("passive_first_available", passive, False),
            ("passive_tariff", passive, True),
            ("hybrid_first_available", stationary, False),
            ("hybrid_tariff", stationary, True),
        ):
            candidate = _joint_schedule(params, fixed, tariff=tariff)
            evaluation = evaluate_schedule(params, candidate)
            scores.append({"seed": name, "score": list(evaluation.score)})
            if best is None or evaluation.score < best.score:
                best, policies, selected = evaluation, candidate, name
        return build_result(params, policies, evaluation=best, diagnostics={
            "termination_reason": "fixed_four_candidate_search", "evaluations": 4,
            "mobility_service_seed_retained": True, "selected_seed": selected,
            "candidate_scores": scores, "stationary_hybrid": stationary_diagnostics,
            "stationary_hybrid_scope": (
                "counterfactual stationary seed before mobile PV allocation; accepted fleet evaluation is authoritative"
                if stationary_diagnostics is not None else None
            ),
            "energy_intervals": energy_intervals,
            "mobility_hybrid_fallback": None,
            "mobility_feasibility": "observed forecast service; four seeds do not certify feasibility or optimality",
            "joint_allocation": "mobile actions recomputed from joint shared-physics states for every candidate",
        })

    plan.capabilities = PlannerCapabilities(mobility=True)
    return wrap_battery_planner(plan)
