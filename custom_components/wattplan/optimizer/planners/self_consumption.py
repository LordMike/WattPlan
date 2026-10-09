"""Passive battery self-consumption baseline with the full live result contract.

Implementation and differences:
    Every battery receives ``self_consume`` in every slot. Shared physics absorbs
    permitted PV, supplies household deficits from energy above reserve, and
    applies curves and efficiencies. There is no battery search, tariff shifting,
    forced grid charging, or native solver. This is the control baseline against
    which the optimizing planners' cost and computation can be compared.

Properties and limits:
    One physical forecast pass makes battery planning cheap and predictable for
    one or several batteries; reporting and appliance ranking add their own work.
    It cannot save energy for expensive hours or react economically to negative
    prices. Targets are observed, not pursued, and a reserve deficit can recover
    only through permitted PV. Misses produce health diagnostics. Battery wear
    and switching preferences do not change this fixed policy.
    Multiple batteries share discharge proportionally to available power. PV is
    allocated in input order by shared physics, so batteries are not economically
    prioritized and changing their order can change PV allocation.

Loads and ordering:
    ``shared.wrap_battery_planner`` still schedules comfort constraints and
    performs bounded price placement before the passive battery replay. Thus
    "passive" describes the battery, not an absence of comfort features.
    ``reporting`` ranks optional appliances independently under the accepted
    battery policies, by incremental cost then start time. Recommendations do not
    turn loads on or jointly schedule them; the first-listed load is not run first.

Evidence:
    The October 2026 benchmark found this the fastest full replay baseline, but
    it missed the synthetic battery deadline. Ending at the same final energy as
    another planner does not erase a missed earlier deadline. See
    ``docs/planners/self-consumption.md`` and ``parity-comparison.md`` there.
"""

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
