# Experimental genetic planner

Factory: `custom_components.wattplan.optimizer.planners.genetic:factory`.
This implementation runs a fresh generational genetic
algorithm on every cycle. A chromosome is a complete battery-major sequence of
`self_consume`, `preserve`, and (where ingress permits it) `grid_charge` actions.
It uses no solver, external optimization package, previous-cycle plan or future
actual data. Home Assistant offers genetic presets alongside the default HiGHS.

The initial population includes self consumption, preserve, permitted grid
charging, and forecast/target-aware seeds. Seed construction follows the shared
physical transition once per forecast slot; it favors cheap charging and protects
energy for valuable later deficits within `guidance_slots` (default 24). This
economic seed window limits setup work to O(horizon * guidance_slots +
batteries * horizon) and does not shorten scoring or target deadlines. Deadline guidance uses current physical
levels, the inclusive deadline, permitted ingress, efficiency and an optimistic
remaining charge bound. It protects attained lower targets and discharges above
upper targets. This is a heuristic seed, not a guarantee of target feasibility.
The mandatory first chromosome restores any initial reserve deficit using legal
ingress before continuing its self-consumption policies. Its full evaluation is
counted even with a one-evaluation or already-expired cooperative budget. Recovery
construction transitions are included in `seed_transitions` and broken out as
`reserve_seed_transitions`. Healthy requests retain the original baseline.

Tournament selection chooses parents. Temporal window crossover exchanges
coherent schedule sections, independently across batteries. Mutations replace a
contiguous mode block, exchange temporal windows, or change one action. Elites
survive each generation. Duplicate chromosomes are skipped; initial filling and
each generation have finite attempt caps. A small action space or disabled
variation therefore cannot make population filling hang. The best evaluated
chromosome is always retained, including the reserve-aware baseline.

Every candidate is scored against the **entire supplied forecast horizon** by
`evaluate_schedule`. Target miss in kWh is minimized lexicographically before
tariff cost plus reserve recovery, throughput and physical mode-switch preferences. `build_result`
reuses that evaluation, so final projected levels and tariff cost follow the
canonical physical model. The model owns PV allocation, reserves, efficiencies,
charge/discharge curves and grid-charge headroom. There is no terminal energy
credit or invented energy. The shared wrapper schedules comfort constraints and
includes comfort consumption in the battery forecast. Full HA reporting exposes
costs, separate base/comfort energy flows, observed constraint violations, and
fixed-policy optional appliance recommendations. Factory and registry calls have
the same features. Missed constraints degrade the accepted plan without claiming
infeasibility. Shared `lookahead_slots`, `solver_time_limit_seconds` and
`infer_battery_preserve_policy` preferences are unused and reported.

## Options and diagnostics

All options are keyword-only. Unknown options fail. Positive integer evaluation,
population and selection budgets reject booleans; population size is at least 2,
elite count is positive and smaller than the population, and tournament size is
positive and no larger than the population. Generations is a nonnegative integer.
Seed is an integer. Mutation and crossover rates are finite probabilities in
`[0, 1]`. Optional `max_seconds` is finite and positive.

Defaults: `max_evaluations=48`, `population_size=8`, `generations=8`,
`elite_count=2`, `tournament_size=3`, `mutation_rate=0.9`,
`crossover_rate=0.8`, `guidance_slots=24`, `seed=0`, `max_seconds=None`.
`guidance_slots` is a positive integer number of original forecast slots.

One complete baseline evaluation is mandatory, including with an already expired
cooperative wall budget or no batteries. Every full seed and offspring evaluation
counts against `max_evaluations`. Physical seed construction is separate,
explicitly reported as `seed_transitions`; it does not compute an objective.
Candidate generation attempts are capped at eight times the evaluation budget;
population filling and each generation additionally stop after eight times the
population size. The complete selected schedule is returned even when search
stops early. Deadlines are cooperative between candidates; the simulator enforces
its separate whole-run limit.

Diagnostics include termination reason, evaluations, structured seed evaluations,
seed transitions, attempted and unique candidates, completed generations,
population diversity, peak diversity, crossover/mutation counts, fixed seed,
forecast horizon, elapsed search time and the baseline score. Fixed seed and
fixed evaluation budgets reproduce schedules and objective values; optional
wall limits depend on hardware. Battery search emits no persistent state; the
shared wrapper carries comfort continuity and measures complete execution time.

## Validation and limits

Tests exercise shared conformance, economic windows, negative tariffs,
144-slot deadlines, feasible/infeasible exact and inequality targets, saturated
and PV-only batteries, heterogeneous equipment, physical projections,
preferences, deterministic seeds, strict options and tiny/duplicate budgets.

Named actions are discrete full-rate policies. An exact target reachable by a
continuous solver may be unreachable by these policies. A bounded GA also cannot
certify global optimality or target feasibility. More evaluations improve the
best forecast objective but need not improve realized receding-horizon economics.
Compare realized cost together with target checks and terminal stored energy.


## Preliminary tuning and retained profiles

[Runnable configuration](genetic-benchmark.json) retains two fixed-seed profiles:
`genetic-fast` uses 16 evaluations, population 6 and 24-slot economic guidance;
`genetic-standard` uses 48 evaluations, population 8 and 144-slot guidance.
Both evaluate all supplied slots and honor all target deadlines. Other variation
and selection options retain the defaults. Seed 0 was fixed before tuning;
seed 11 provides an independent robustness check.

The archived 48-hour sample contains 192 cycles, one battery and 144-slot
forecasts, without targets. Each replay had a strict 120-second whole-run cap;
all retained runs completed. These are preliminary serialized WSL measurements,
not results from the final integrated comparison. Costs are DKK. All retained
runs start at 1.05 kWh and end at 1.0 kWh.

| Forecasts | Profile | Seed | Run seconds | Realized cost DKK |
| --- | --- | ---: | ---: | ---: |
| Recorded | fast | 0 | 7.77 | 68.981421 |
| Recorded | fast | 11 | 7.36 | 68.840390 |
| Recorded | standard | 0 | 13.80 | 67.979279 |
| Recorded | standard | 11 | 13.75 | 68.938676 |
| Causal | fast | 0 | 6.70 | 69.476598 |
| Causal | fast | 11 | 7.11 | 70.520445 |
| Causal | standard | 0 | 14.14 | 69.111255 |
| Causal | standard | 11 | 14.15 | 69.901045 |

The larger 96-evaluation budget and shorter standard-profile guidance did not
improve realized economics consistently. Fast 144-slot guidance was rejected:
one causal replay ended at 2.1076 kWh and cost 71.3596 DKK, so its cost comparison
would also have unequal terminal energy. Increasing budget improves the best
forecast objective, not necessarily closed-loop outcomes. The retained profiles
improve this sample's self-consumption costs, but beam-search has lower costs on
both forecast versions. These results do not justify replacing the production
planner.

The independent 96-cycle synthetic fixture has two heterogeneous batteries,
negative tariffs, PV and an absolute vehicle deadline. Its currency is TEST,
not DKK. Fast 24-slot guidance completed with zero misses on both seeds:
2.092129 TEST, terminal house 0.7 kWh and vehicle 5.0 kWh, vehicle deadline
level 4.274785 kWh. Standard 144-slot guidance also completed with zero misses:
3.279315 TEST, the same terminal levels, and vehicle deadline level 4.879937 kWh.
Standard guidance therefore has worse realized synthetic economics despite its
improvement on the archived real sample. Tests independently cover infeasible and exact targets;
there is no claim of universal target feasibility.

Run with an independent, empty output directory:

```bash
python scripts/simulate_planner.py INPUT --config docs/planners/genetic-benchmark.json --output OUTPUT
```
