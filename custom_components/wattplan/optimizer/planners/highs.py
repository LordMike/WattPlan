"""Expose the original HiGHS model-predictive planner through the factory API.

Implementation and differences:
    This file is an adapter, not a self-contained solver. ``prefix_planner``
    manages full replans and near-term refreshes; ``mpc_power_optimizer`` builds
    native HiGHS mixed-integer models and produces the result. The model uses
    continuous battery power with discrete operating constraints, unlike the
    alternatives' search over three executable battery policies. Battery targets
    use soft penalties rather than the alternatives' target-first tuple score.

Properties and limits:
    Batteries are modeled together, making this the most expressive battery
    allocation model here. That does not establish a measured advantage for
    many batteries: model size, integer decisions and repeated solves can make
    long horizons expensive. Continuous model precision also does not guarantee
    the same precision when the published inverter policies execute.
    Native time limits apply per solve, not to the whole planning call. A solve
    with no usable solution raises an error; soft targets and bounded solves do
    not guarantee feasibility or a globally optimal receding-horizon outcome.

Loads and ordering:
    Comfort starts from a constraint-driven schedule. Bounded placement prices
    moves under the baseline battery policies, then may replan batteries once
    around the changed schedule; this is not unrestricted joint load scheduling.
    Optional appliances are independent recommendations priced after the plan,
    with earlier starts breaking equal-cost ties. They are neither automatically
    run first nor jointly reserved against other optional appliances.

Configuration:
    ``highs`` retains request settings and legacy state. ``highs-8/16/24``
    use the same factory with fixed lookahead slots, not hours.
    Named presets are defined in ``presets.py``. See
    ``docs/optimizer-profiles.md`` for user-facing choices and limitations.
"""

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
