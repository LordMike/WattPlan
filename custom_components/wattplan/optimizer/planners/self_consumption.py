"""Deterministic physical baseline, without price or deadline optimization."""

from .contract import PlannerCapabilities
from .evaluation import build_result
from .shared import wrap_battery_planner


def factory():
    def plan(params):
        return build_result(
            params,
            [["self_consume"] * len(params.usage_kwh) for _ in params.battery_entities],
            diagnostics={
                "termination_reason": "deterministic_baseline",
                "evaluations": 1,
                "battery_policy": "self_consume in every slot; no tariff or target search",
                "reserve_recovery": "PV only; no forced grid charging",
            },
        )

    plan.capabilities = PlannerCapabilities(
        targets="observed",
        ignored_preferences=(
            *PlannerCapabilities().ignored_preferences,
            "throughput_cost_per_kwh", "mode_switch_cost", "action_deadband_kwh",
        ),
    )
    return wrap_battery_planner(plan)
