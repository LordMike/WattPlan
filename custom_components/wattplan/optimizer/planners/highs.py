"""Adapter preserving the production HiGHS result and legacy state format."""

from .contract import PlannerCapabilities


def factory():
    from .. import optimize

    def plan(params):
        return optimize(params)

    plan.capabilities = PlannerCapabilities(
        comfort=True, optional_loads="supported", ignored_preferences=()
    )
    return plan
