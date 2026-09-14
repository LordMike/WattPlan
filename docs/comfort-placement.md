# Comfort Placement Preparatory Component

`optimizer/comfort_placement.py` is an independent preparatory component. It is
not wired into production planning and does not construct a joint comfort and
battery optimization model.

## Contract

The caller supplies one `ComfortPlacementInput` per comfort entity, including an
existing schedule, ordered rolling history, runtime limits, and any active
current-state commitment. The existing schedule must be feasible. Invalid input
schedules return `status="infeasible"` with explicit violations and are never
priced or modified.

`place_comfort_schedules()` receives a cost callback whose only argument is the
tuple of all comfort schedules in input order. This lets later integration price
the complete accepted load set using direct net-site accounting or one replay of
an existing fixed battery policy. The component contains no battery simulator.

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
keep the component deterministic, reviewable, and cheap enough for later planner
integration to call only when comfort flexibility is actually useful.
