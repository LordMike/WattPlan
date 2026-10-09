"""Adapter preserving the production HiGHS result and legacy state format."""

from .contract import PlannerCapabilities


def factory(*, lookahead_slots=None):
    from .. import optimize

    if lookahead_slots is not None and (
        type(lookahead_slots) is not int or not 2 <= lookahead_slots <= 672
    ):
        raise ValueError("lookahead_slots must be an integer between 2 and 672")

    def plan(params):
        if lookahead_slots is not None:
            params = params.model_copy(update={"lookahead_slots": lookahead_slots})
        return optimize(params)

    plan.capabilities = PlannerCapabilities(
        comfort=True, optional_loads="supported", ignored_preferences=()
    )
    return plan
