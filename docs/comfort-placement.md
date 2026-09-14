# Bounded Comfort Placement

`optimizer/comfort_placement.py` is the bounded placement component used by
production planning. It does not construct a joint comfort and battery
optimization model.

## Contract

The caller supplies one `ComfortPlacementInput` per comfort entity, including an
existing schedule, ordered rolling history, runtime limits, and any active
current-state commitment. The existing schedule must be feasible. Invalid input
schedules return `status="infeasible"` with explicit violations and are never
priced or modified.

`place_comfort_schedules()` receives a cost callback whose only argument is the
tuple of all comfort schedules in input order. Production prices the complete
candidate load set using direct net-site accounting when no battery exists, or
fixed-policy replay of the current baseline battery plan. The component contains
no battery simulator and never calls the MILP solver.

## Work Bound

The search makes one pass over the comfort entities. A candidate relocates one
existing ON run without changing that entity's total ON slots. Each candidate is
checked against rolling-window targets, minimum ON/OFF runs, maximum OFF time,
initial commitments, and terminal run boundaries before cost evaluation.

Work is bounded by `max_candidates_per_comfort` and `max_total_candidates`. The
cost callback is called at most once for the initial schedule plus once per
feasible candidate. A cancellation callback is checked between candidates.

## Limitations

The result explicitly reports `optimality="heuristic"`. The search does not
enumerate combinations, split or merge runtime blocks deliberately, revisit an
earlier comfort after accepting a later move, or prove a global optimum. A
bounded sample can miss a better relocation on a long horizon. These constraints
keep the component deterministic, reviewable, and cheap enough for production
planning to call only when comfort flexibility is actually useful.

## Production Orchestration

Requests without comfort entities bypass placement, candidate costing, and any
additional planning pass. Other requests first build the existing feasible
constraint-driven baseline. Production considers at most 16 candidates per
comfort, 48 in total, and stops placement after a 0.1-second search budget so
time remains for final planning.

When placement changes demand, production discards reusable battery control
arrays and rebuilds the plan once with comfort fixed. Preserve probes therefore
see the accepted comfort demand rather than independently rescheduling it. The
replay estimate is not treated as final savings: actual rebuilt tariff cost and
hard battery/comfort constraints are compared with the baseline. A worsened or
invalid result falls back to the baseline before optional suggestions, opaque
state, prefix receipts, and published schedules are created.

The response's `comfort_placement` object reports status, cost mode, candidate
and callback counts, whether demand changed, additional primary solve count,
actual baseline/final projected cost, explicit violations, and any fallback
reason. `optimality="heuristic"` remains explicit.
