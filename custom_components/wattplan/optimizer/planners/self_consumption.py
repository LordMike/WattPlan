"""Deterministic physical baseline, without price or deadline optimization."""

from .contract import PlannerCapabilities
from .evaluation import build_result


def factory():
    def plan(params):
        return build_result(
            params,
            [["self_consume"] * len(params.usage_kwh) for _ in params.battery_entities],
            diagnostics={
                "termination_reason": "deterministic_baseline",
                "evaluations": 1,
            },
        )

    plan.capabilities = PlannerCapabilities(targets="observed")
    return plan
