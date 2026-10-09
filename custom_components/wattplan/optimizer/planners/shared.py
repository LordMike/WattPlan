"""Common live features around a solver-independent battery search."""

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
