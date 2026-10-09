# Charge-window search

`charge-window` (or `custom_components.wattplan.optimizer.planners.charge_window:factory`)
constructs and searches complete battery policy schedules. This is experimental,
stateless, original code inspired by the general idea of charging windows in
Predbat's public documentation. No Predbat source is copied, no native solver is
called, and no past solution or forecast score is reused on the next decision.

The first counted candidate starts from self-consumption, with physically
constructed target actions and immediate reserve recovery when configured.
Healthy batteries without targets retain the original baseline. A single target
construction pass uses actual battery curves, efficiency and available load;
`target_seed_transitions` reports that work separately from scored evaluations.
Tariff-threshold and additional target seeds follow. Deadline-anchored charge,
preserve and release intervals are evaluated
before economic refinement. Economic candidates include an atomic contiguous
charge block, preservation until a later contiguous release block, uniform
window changes and shifts of existing policy boundaries. Interval lengths descend
geometrically to single slots; tariff ranking retains a bounded number of cheap
and valuable windows rather than allocating all horizon-squared intervals.
Charging windows are ranked before each release so a future cheap period cannot displace the buy opportunity. Ranked anchors are separated to cover distinct tariff periods, and evaluation allowances reserve work for finer scales. Search changes one battery at a time, while seed policies and every evaluation
include all batteries and the entire supplied horizon.

Every candidate goes through `planners.evaluation.evaluate_schedule`, using only
the current request forecasts and neutral battery physics. Target misses at the
inclusive deadline are minimized lexicographically before net tariff cost,
throughput cost, physical behavior switching cost, and per-slot reserve-shortfall
penalties. Reserve penalties prioritize recovery but never appear in billed cost.
The accepted evaluation is
reused by `build_result`: final projections incur no uncounted replay. Ingress,
PV sharing, reserve, nonlinear charge/discharge curves and efficiencies have the
same execution semantics as the simulator. Policies are `self_consume`,
`preserve` and ingress-permitted `grid_charge`; energy cannot be exported from a
battery and no PV curtailment or partial-power action is invented.

Options are strict keyword arguments:

| Option | Default | Meaning |
| --- | ---: | --- |
| `max_evaluations` | 128 | Positive integer; includes baseline and seeds. |
| `max_seconds` | `None` | Optional finite positive cooperative search deadline. |
| `window_candidates` | 3 | Integer 1–16; retained cheap/release windows per scale. |

A request work cap further limits evaluations to
`max(1, 196608 // (horizon_slots * max(1, battery_count)))`. Candidate attempts
are bounded at eight times that effective evaluation limit, including duplicate
policies. Ranking uses linear prefix arrays and a bounded number of linear scans; it does not build
an exhaustive window or joint-battery action list. The first complete baseline
is always physically evaluated and counted, even when a tiny wall deadline has
already elapsed. Search deadlines are checked between candidates; a single
physical replay may exceed them. The simulator independently enforces its
whole-replay wall limit.

The planner ignores legacy `lookahead_slots`,
`solver_time_limit_seconds` and `infer_battery_preserve_policy`; it scores and
returns the full supplied horizon. The [shared comfort layer](comfort-parity.md)
honors rolling targets, run constraints, history, and runtime continuity, then
includes accepted comfort consumption in the window search. Full HA projections,
flows, unmet-constraint status, and fixed-policy optional-appliance recommendations
are returned by direct factory and registry calls. Optional loads remain advisory
and excluded from the battery objective. Battery search stays stateless; comfort
continuity uses the shared opaque state wrapper.

Diagnostics include requested/effective evaluation limits, counted evaluations,
termination reason, seeds, candidate attempts, visited scales, accepted move
counts, baseline score, target checks and final SoC. With fixed count options,
results are deterministic; wall deadlines depend on the machine. A bounded
heuristic cannot guarantee target feasibility or a global optimum. An exact
continuous target can be unattainable using discrete policy actions even when a
partial-power controller could reach it. Report target misses and terminal SoC
alongside realized cost, and use the shared conformance cases plus closed-loop
replays when comparing configurations.


Initial tuning selected **one** configuration, in
[`charge-window-comparison.json`](charge-window-comparison.json):
`max_evaluations=96`, `window_candidates=3`, no wall deadline or preference
overrides. In a single 192-cycle private 48-hour replay, the archived forecasts
produced 69.967282 DKK in 18.895 s; causal persistence usage/PV forecasts produced
70.655554 DKK in 17.613 s. Both ended at 1 kWh and had no configured targets.
Self-consumption was 72.314373 DKK in both scenarios with the same terminal SoC.
These are workstation tuning observations, not independent validation or proof
of a general advantage. The existing beam-search configurations had lower cost
on this sample. The 48-evaluation candidate retained 4.142747 kWh and cost
78.041809 DKK with causal forecasts; the 160-evaluation candidate provided no
useful improvement over 96. These candidates are excluded from the selected
configuration. Original failed tuning runs and all corrected reports, options,
action counts and input/source hashes are retained outside git under the
`window2-*` paths in the private planner lab.
