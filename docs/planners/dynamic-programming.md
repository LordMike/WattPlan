# Experimental SoC dynamic programming

Factory: `custom_components.wattplan.optimizer.planners.dynamic_programming:factory`.
It is stateless and starts a fresh forecast-only search on every call.

At each supplied forecast slot, the planner applies permitted joint battery policies
using the shared physical transition. Its Bellman table is keyed by the tuple of
SoC cells and each battery's previous actual charge/idle/discharge behavior. A cell
retains the trajectory with the smallest accumulated lexicographic score: target
misses first, then tariff cost plus throughput, physical switching, and per-slot
reserve-shortfall penalties. The shared reserve penalty prioritizes replenishing
initially deficient batteries without inventing energy or charging billed cost.
Targets are checked at the end of their inclusive deadline slot, with the shared
exact/at-least/at-most semantics and tolerance.

`soc_step_kwh` groups states; it **never rounds carried energy**. Each retained
trajectory contains its real levels, its parent, and the executable actions that
produced it. Result levels and tariff cost come from a final shared evaluator replay
of those exact actions. Curves, ingress, efficiency, reserve and initial levels below
reserve consequently follow the same physics as the other experimental planners.

This is approximate state aggregation, not an optimality guarantee. Different real
levels in the same cell have different future feasibility and cost; discarding one
can lose a better solution. The table includes prior physical behavior because
switching penalties otherwise invalidate state identity. When there are too many
cells, forecast/target heuristics retain `max_states` cells. This separate pruning
uses a bounded window of forecast demand sorted by value and full-deadline
reachability bounds; these are guidance, not
terminal energy credit or a proof of an admissible full-cost bound.

| Keyword-only option | Default | Meaning |
| --- | ---: | --- |
| `soc_step_kwh` | 0.25 | Finite positive SoC cell width, in kWh |
| `max_states` | 32 | Positive integer retained cells per slot |
| `max_expansions` | 20000 | Positive integer physical transitions during search |
| `max_joint_actions` | 81 | Positive integer joint policy choices per state |
| `value_horizon_slots` | 24 | Positive integer forecast slots used to value stored energy during pruning/completion |
| `max_seconds` | null | Optional finite positive cooperative search deadline |

For initially deficient batteries, an ingress-permitted reserve recovery action
comes first, including under a one-transition budget. Coordinated all-self-consume,
all-preserve and permitted all-grid-charge policies,
then single-battery changes, precede a lazily generated Cartesian action tail.
Preparation stops at the smaller of `max_joint_actions` and `max_expansions`; no
Cartesian state grid is allocated. With many batteries, some joint actions are
explicitly excluded. Search work is at most `max_expansions` transitions. Retained
history is bounded by the horizon and frontier limit; temporary cells are bounded
by the transition budget. Deterministic counts and tie ordering give repeatable
schedules; a wall deadline is hardware-dependent.

The full supplied horizon is returned for every battery. If the count or cooperative
time budget expires, a single retained prefix is completed by a forecast/target-aware
policy which also prioritizes reserve recovery. This mandatory completion is outside the search budget and costs one physical
transition per remaining slot; the final evaluator also replays every supplied slot.
`expansions`, `tail_transitions`, `replay_transitions`, `evaluations`, `searched_slots`,
merge/prune counts and excluded joint-action counts report these costs and approximations
separately. `termination_reason` identifies exhaustion. Tiny budgets therefore still
return full physical schedules but can have poor target and economic outcomes.

The legacy `lookahead_slots`, solver-deadline and preserve-inference
preferences are ignored and disclosed. `value_horizon_slots` limits heuristic
valuation only; the Bellman recursion and target scoring still use the full
supplied horizon. The [shared comfort layer](comfort-parity.md) handles rolling
targets, run constraints, history, and continuity, then passes modeled comfort
consumption into DP. Full HA reporting includes comfort actions, cost and flow
projections, unmet target/reserve diagnostics, and optional-appliance windows
priced under the selected battery policy. Direct factory and registry calls use
the same wrapper. There is no battery export, curtailment, invented
load or monetary value assigned to final SoC. Compare realized replay cost alongside
final stored energy, target misses, coverage and total replay time.

Tests use the shared conformance helper, tiny exhaustive policy oracles with a fine
unpruned table, coarse cells with unrounded heterogeneous physical trajectories,
physical switching/deadband penalties, all deadline modes including infeasibility,
negative prices, PV-only charging and a 20-battery/672-slot tiny-budget case.

The selected [benchmark configuration](dynamic-programming-benchmark.json) contains
a fast profile and a stronger profile using a longer valuation window. Initial
two-day tuning with archived forecasts and a companion
causal persistence usage/PV forecast is evidence for comparison, rather than a
forecast accuracy or optimality claim. These replays have no battery targets;
conformance/oracle tests and the public synthetic target fixture test that behavior.
Reports and private data remain outside git. Each configuration/repeat is limited
to 120 seconds for the entire replay, with CPU work serialized by the shared lock.

| Selected profile | Archived 48h cost / replay time | Causal 48h cost / replay time |
| --- | --- | --- |
| Fast | 65.877327 DKK / 10.379 s | 68.821431 DKK / 9.715 s |
| Strong | 64.801996 DKK / 28.800 s | 67.733586 DKK / 31.648 s |

These are initial single runs on one workstation, before independent integrated
benchmarking. All four completed 192 decisions and ended at 1 kWh from 1.05 kWh;
coverage is 172718.187962 seconds after the initial capture delay. All reported
zero target misses because those private inputs have no targets. Neither profile
needed a fallback tail. Forecasts affect realized rankings: the intermediate
24-state refinement improved archived cost slightly but worsened causal cost and
was rejected. An earlier coarse-cell fast candidate ended with 1.936111 kWh and
was rejected; its cost is not compared as an equal-reserve result. No timed-out
configuration was retried or given a larger total replay budget.

The public synthetic two-battery target replay completed 96 decisions and the full
86400 seconds under both profiles, with zero target misses and no fallback tail.
The vehicle deadline ended at 4.879936679 kWh under both. Fast took 10.948 seconds,
cost 1.967313, and ended at house 0.7 / vehicle 5 kWh. Strong took 24.271 seconds,
cost 5.585262, and ended at house 4.652458 / vehicle 5 kWh. Strong consequently
carries 3.952458 kWh more house energy at that fixture's endpoint: those costs are
**not equal-reserve comparisons**. The longer valuation window is not universally
economical; no terminal monetary credit is added to hide this difference.
