"""Compose the live features shared by the eight non-HiGHS battery planners.

The individual implementation modules own battery search, not the entire plan.
Their factories return this wrapper, so direct factory calls and registry calls
both receive comfort scheduling, full reporting and continuity handling. HiGHS
uses its native orchestration instead, sharing the comfort constraint constructor.

Processing order and its consequences:
    First, ``comfort.prepare_comfort`` constructs and adjusts comfort schedules
    using bounded moves priced by battery self-consumption. That consumption is
    added to the battery request. The chosen battery algorithm then searches
    around fixed comfort demand; it does not jointly change comfort start times.
    ``evaluation.build_result`` calls ``reporting`` for HA projections and
    optional-appliance recommendations. The comfort finalizer restores the base
    demand baseline, separates energy flows and carries comfort state forward.

    Each optional appliance is priced independently against the same accepted
    battery/comfort plan. Options sort by incremental full-horizon cost, then
    earlier start, subject to that appliance's option-spacing rules. Several
    appliances can consequently recommend the same cheap slot. They are not
    reserved against each other, added to the accepted demand or switched on.
    Scheduling comfort first is a computation order, not a rule that every
    comfort or optional load must run before the batteries.

Shared physical and budget limitations:
    Policy replay allocates PV to permitted batteries in input order and shares
    discharge proportionally to available power. Individual search strategies do
    not optimize that physical allocation rule. Policies cannot request arbitrary
    fractional grid charging; target misses are observations, not infeasibility
    proofs. Reported electricity cost gives no credit for leftover stored energy,
    so comparisons also need deadline outcomes and final battery levels.
    Search budgets do not include a hard bound on this whole wrapper:
    comfort placement, mandatory replay and appliance ranking add work. Diagnostics
    separate these phases and preserve the algorithm's own stopping reason.
"""

from dataclasses import replace
from functools import wraps
from time import perf_counter

from .contract import PlannerCapabilities


def wrap_battery_planner(implementation):
    """Add comfort preparation/continuity and honest whole-call diagnostics.

    Factories use this layer directly, so registry calls and direct factory
    calls support the same input and output contract. Opaque registry ownership
    still encloses the implementation's battery/comfort state.
    """
    original = getattr(implementation, "capabilities", PlannerCapabilities())
    capabilities = replace(
        original,
        comfort=True,
        optional_loads="supported",
        ignored_preferences=tuple(
            key for key in original.ignored_preferences
            if key != "rolling_window_slots"
        ),
    )

    @wraps(implementation)
    def plan(params):
        from .comfort import prepare_comfort

        started = perf_counter()
        notes = capabilities.validate(params)
        prepared = prepare_comfort(params)
        prepared_at = perf_counter()
        result = implementation(prepared.params)
        planned_at = perf_counter()
        result = prepared.finalize(result)
        finished = perf_counter()
        result["execution_time"] = finished - started
        diagnostics = result.setdefault("diagnostics", {})
        diagnostics["capability_notes"] = notes
        diagnostics["ignored_preferences"] = list(capabilities.ignored_preferences)
        diagnostics["timing"] = {
            **diagnostics.get("timing", {}),
            "comfort_seconds": prepared_at - started + finished - planned_at,
            "battery_and_reporting_seconds": planned_at - prepared_at,
            "total_seconds": finished - started,
        }
        return result

    plan.capabilities = capabilities
    return plan
