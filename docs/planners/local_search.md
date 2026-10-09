# Bounded local-search planner

Factory: `custom_components.wattplan.optimizer.planners.local_search:factory`.
This is an offline battery policy experiment. Production HiGHS and the planner
registry are unchanged. There is no native solver dependency or fallback.

## Policy and objective

The planner evaluates complete battery-major schedules against the current
request's forecasts using `evaluate_schedule`, then passes its accepted
`Evaluation` to `build_result` without another replay. Shared physics handles
ordered PV allocation, proportional discharge, ingress masks, efficiencies,
SoC curves, capacity and reserve. It cannot suppress PV charging, export battery
energy, or invent load. An initially low reserve remains visible.

Scores minimize target miss kWh before tariff cost plus the request's throughput
and switching penalties. Target deadlines include the end of their slot;
`at_least`, `at_most`, `exact`, tolerances and infeasible misses use the shared
objective. No terminal energy credit is assigned. An approximate search may
miss a feasible or better schedule; reporting a target does not guarantee it.

**The search and objective cover the full supplied forecast horizon.**
`lookahead_slots` is ignored, as are `rolling_window_slots`,
`solver_time_limit_seconds` and `infer_battery_preserve_policy`; these appear in
capability diagnostics. The legacy HiGHS reference uses its own lookahead and
scalar soft-target objective. Different horizons/objectives confound a direct
algorithm-only attribution of realized cost differences. Comfort is rejected;
optional recommendations are explicitly ignored. Throughput, switching and
physical action deadband preferences participate in the shared evaluation.

Each call starts from current SoC, equipment, forecasts and targets. No request
is mutated, no actuals enter planning, and state is `None`. There is no cached
schedule or score to invalidate across timestamp/equipment changes.

## Search and bounds

Seeds include self-consumption, preserve, four broad tariff thresholds,
cheap-before-deadline target protection with adjacent charge-count alternatives,
and a maximum target-charge prefix. Seed construction does not replay physics
or silently evaluate candidates. Curves and coupled batteries are judged only
by the shared evaluator.

The loop interleaves single-action edits, contiguous blocks of 2/4/8/16/32 slots
and two-slot exchanges. Coordinates prioritize target prefixes and large signed
price magnitudes with seeded tie order. A bounded block perturbation can restart
search from the best policy; the returned global best always preserves strict
lexicographic improvements over the self-consumption baseline. Duplicate
candidates are skipped without consuming an evaluation, but do consume a
separate attempt budget so exhausted small spaces cannot spin indefinitely.

| Factory option | Default | Meaning |
| --- | ---: | --- |
| `max_evaluations` | 96 | Positive integer, includes the initial baseline and every scored seed/mutation/restart |
| `max_seconds` | `None` | Optional finite positive cooperative deadline checked between candidates |
| `seed` | 0 | Integer controlling deterministic mutation/tie order |
| `max_restarts` | 2 | Nonnegative integer limiting attempted perturbation restarts |

Unknown options fail. The effective evaluation limit is the smaller of the
requested limit and `max(1, 196608 // (horizon * max(1, battery_count)))`.
For 672 slots and 16 batteries this permits 18 full replays. The final schedule
still covers all 672 slots for every battery. This work guard bounds forecast
replay cost rather than shortening the horizon or silently dropping targets.
Diagnostics disclose requested/effective limits and work cap. Candidate attempts
are capped at eight times the effective limit, including skipped duplicates.

One counted baseline replay is reserved before enabling the optional deadline
so even a tiny deadline returns a complete physical result. Like any cooperative
budget, that replay or a subsequent in-flight evaluation can overrun the time
bound; the simulator independently enforces its whole-call timeout. There is no
unbudgeted final replay. Fixed count/seed runs are reproducible; wall deadlines
are hardware-dependent. Search stops at evaluation, time or attempt limits, or
after its permitted stalled restarts.

## Initial tuning ledger (2026-10-09)

Foundation: `35806f88a3959f4f3e2402bce2af287ecf8ab946`.
Inputs and reports are outside the repository under
`N:\Personal\WattPlan-planner-lab\local-search` (WSL
`/mnt/n/Personal/WattPlan-planner-lab/local-search`).
`tuning-ledger.json` retains full precision outcomes, input/config/source hashes,
settings, latency, target checks and evaluation counts. Each report also retains
its own source hashes, revision/worktree state, packages and input provenance.
`commands.json` records the exact commands and working directory.

The immutable synthetic input is a UTC day with 96 replan cycles, 24-hour
forecasts, two heterogeneous batteries, signed import/export prices and an
absolute deadline. Usage/PV actuals are independently perturbed. Every normal
synthetic run below had net cost **1.967312906110017 TEST**, import
**18.23750486390461 kWh**, export **13.933656764017906 kWh**, no target misses,
and final SoC **house 0.7 / vehicle 5.0 kWh**. The vehicle deadline reached
4.879936678869873 kWh. These equal realized outcomes do not mean the full forecast
policies/objective scores were identical.

| Report / variant | Evaluations minâ€“max / total | CPU total s | Wall total s | CPU / wall p95 s |
| --- | ---: | ---: | ---: | ---: |
| initial: 96, seed 0 | 96â€“96 / 9216 | 8.762104 | 8.327988 | 0.104948 / 0.099758 |
| initial: 384, seed 0 | 281â€“384 / 32386 | 30.374737 | 28.860494 | 0.369315 / 0.350740 |
| initial: 768, seed 0 | 281â€“768 / 35902 | 33.268374 | 31.589623 | 0.573112 / 0.544614 |
| initial: 384, seed 17 | 254â€“384 / 31692 | 29.654485 | 28.170044 | 0.372707 / 0.354355 |
| final: 24, seed 0 | 24â€“24 / 2304 | 2.131516 | 2.021420 | 0.027380 / 0.025965 |
| final: default 96, seed 0 | 96â€“96 / 9216 | 8.565920 | 8.133310 | 0.099445 / 0.094521 |
| final: 96, seed 17 | 96â€“96 / 9216 | 8.557700 | 8.133786 | 0.098971 / 0.094062 |

Initial code used a 384 default. Raising 96 to 384/768 increased runtime without
improving realized outcomes here, so the final default is 96. The 24 budget also
worked on this one fixture, but spends most of its work on seeds; 96 retains
moderate local exploration for requests with different curves and equipment.
Seed 17 confirms the outcome is not unique to seed 0 on this fixture. No
thresholds, work cap or physical behavior were tuned to actuals. Broad seeds,
blocks and exchanges address ordinary dispatch decisions; duplicate/long-horizon
caps address bounded-cost requirements. No suffix cache was justified by the
measured sub-0.1-second normal p95, so the loop retains the canonical evaluator.

The initial HiGHS reference (`../synthetic-reference-initial`) had net cost
2.11641791930286, zero target misses and the same final 5.7 kWh. Self-consumption
had 4.891220216763088, one 2.75 kWh deadline miss and the same final SoC.
Full-horizon and objective differences above prevent attributing this local-search
result solely to search quality. This is one synthetic day, not a deployment
qualification or an optimality claim.

Additional final-default runs:

| Dataset / report | Net cost | Import / export kWh | Target misses | Final SoC kWh | CPU / wall total s | CPU / wall p95 s | Evaluations |
| --- | ---: | ---: | ---: | --- | ---: | ---: | ---: |
| HA seven-cycle smoke / `ha-smoke` | 0.2675115502574735 | 0.1361844893111111 / 0 | 0 | Battery 1.2923681180987658 | 0.684329 / 0.648011 | 0.103549 / 0.098050 | 672 |
| Full synthetic stress day / `stress-default` | 2.419425778850165 | 20.521347952118703 / 13.556763382296753 | 1 | house 0.7 / vehicle 5.0 | 8.777049 / 8.339921 | 0.111121 / 0.105576 | 9216 |

The HA data uses seven archived forecasts (Oct 8, Copenhagen 10:15â€“12:00),
step-held meter actuals and archived first-slot tariffs, with provenance in
`../ha-provenance.json`. Both reference planners had zero cost and final Battery
1.141052018864198 kWh. Local search imported more and ended with more stored
energy while planning against its full forecasts. **This smoke shows no algorithm
win** and is too short for economic conclusions. Its limited actuals provenance
remains explicit; it was not used for tuning.

The separately generated `--stress` day starts below reserve and requests an
unreachable exact house deadline: SoC 1.848 kWh versus 6 kWh, miss 4.152 kWh.
The vehicle deadline is met. This is a disclosed physical infeasibility, not a
silent target bypass. `stress-data` is separate from immutable supplied inputs.

Independent exhaustive review (`../initial-oracle-local-search.json`,
`../tiny_oracle.py`) checked six four-slot cases: five matched the global oracle.
The `at_most` case met its target but scored 0.15 versus oracle 0.09. The missed
preserve-before-deadline alternative illustrates bounded-search limitations;
no special seed was added for that fixture. Full-horizon evaluation correctness
does not guarantee complete search or optimal target satisfaction.

Initial source SHA256:
`3e1695c6f136198c5bbb39726f32e50ca638b947efc6d106cc0acd1ae0d8bfc4`.
Final source SHA256:
`fef4a291205727221567cfc0d312130272ae7d0c2171ba5b11036916947b5b00`.
Normal input SHA256:
`e34c2cb9c30fde03c396e9ffc2236e1b98b8ada62f99e2b672613dea0c2b00b9`.
Stress input SHA256:
`9cde0e4905dcc8d800985e7af2fddbc9c0fbddccb114bd964f81ac21770a8a20`.

## Reproduce and verification

Use the prepared WSL Python environment and the simulator workflow in
[simulator.md](../simulator.md). Serialize CPU-heavy runs with
`flock /tmp/wattplan-planner-compute.lock`. For simulator provenance on this
Windows-created worktree only, set
`GIT_DIR=/mnt/n/Personal/WattPlan/.git/worktrees/WattPlan-planner-local-search`
and `GIT_WORK_TREE=/mnt/n/Personal/WattPlan-planner-local-search`.
Do not apply those overrides to tests that create temporary repositories.

Configs: `initial-tuning.json`, `final-tuning.json`, `selected.json` in the external
artifact directory. Report directories: `initial-tuning`, `final-tuning`,
`ha-smoke`, `stress-default`. Output directories must be new or empty. Exact
commands are retained in `commands.json`; for example, from this worktree:

```bash
flock /tmp/wattplan-planner-compute.lock env \
  GIT_DIR=/mnt/n/Personal/WattPlan/.git/worktrees/WattPlan-planner-local-search \
  GIT_WORK_TREE=/mnt/n/Personal/WattPlan-planner-local-search \
  /tmp/wattplan-simulation-venv/bin/python scripts/simulate_planner.py \
  /mnt/n/Personal/WattPlan-planner-lab/synthetic \
  --config /mnt/n/Personal/WattPlan-planner-lab/local-search/final-tuning.json \
  --output /mnt/n/Personal/WattPlan-planner-lab/local-search/final-tuning
```

The dedicated tests cover shared conformance, economic dispatch, all target
modes/infeasibility, signed tariffs, curves/preferences, input isolation,
determinism, solver-free imports, counted baseline/final evaluations, cooperative
time behavior, duplicate attempts and the 672-slot/many-battery work cap.

Final verification: **921 tests passed in 66.87 seconds, 88.58% coverage**
(including 29 dedicated local-search cases). `full-tests.log` and `coverage.json`
retain the complete elevated run. Ruff F checks and staged diff checks pass.
