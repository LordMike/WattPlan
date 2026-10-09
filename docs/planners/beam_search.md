# Bounded beam-search planner

Factory: `custom_components.wattplan.optimizer.planners.beam_search:factory`.
This stateless experimental planner uses only the supplied forecasts. Production
HiGHS selection is unchanged; there is no solver fallback.

The planner advances executable joint `self_consume`, `preserve`, and permitted
`grid_charge` policies through shared physical transitions. Each beam node stores
physical SoC, previous actual behavior, accumulated tariff/throughput/switching
cost, deadline misses, and a linked policy prefix. Identical physical levels and
previous modes are merged, retaining the lower exact prefix score. Floating point
states are compared exactly, without rounding away physical differences. Stable
iteration/sorting makes fixed work budgets deterministic.

Prefix ranking first minimizes past target misses plus optimistic unavoidable
future deadline misses. Reachability uses maximum curve limits, future forecast
surplus for PV-only charging, and forecast load for discharge. It is optimistic:
shared PV/load competition, SoC-dependent limits and grid headroom can make the
bounds unattainable. Deadline tolerance applies. At-least, at-most, exact and
infeasible targets use the same final shared target-miss evaluator.

The economic guidance subtracts the value of currently deliverable stored energy
(capped by maximum discharge rate times the remaining guidance window)
against the largest positive import prices in the next `guidance_slots` forecast
slots, capped by forecast household deficits. It adds a remaining target charging
cost estimate using the cheapest forecast import price before the deadline. The
estimate accounts for discharge efficiency, reserve and throughput preference;
it does not anticipate competing future cheap grid/PV charging, precise future
curves or future switching penalties. Target reachability spans its full deadline,
including deadlines beyond the economic guidance window. `guidance_scale=0`
disables economic guidance while retaining target reachability. Guidance is a
pruning heuristic, not the scored objective and not an optimality certificate.

Each retained complete candidate is replayed with `evaluate_schedule`; the best
exact tuple of total deadline miss and tariff plus preference costs wins. Prefix
accumulation is checked against that replay. Final objective has **zero terminal
energy credit**. Report cost together with final SoC and target misses.

## Options and bounds

| Option | Default | Meaning |
| --- | ---: | --- |
| `beam_width` | 16 | Maximum retained prefixes per slot |
| `max_expansions` | 30000 | Maximum physical transitions during beam search |
| `max_joint_actions` | 81 | Maximum joint actions considered per prefix |
| `guidance_slots` | 24 | Future slots used for economic guidance |
| `guidance_scale` | 1.0 | Nonnegative economic guidance multiplier |
| `max_seconds` | null | Optional cooperative search deadline |

Positive integer options and finite positive time limits are validated by the
factory. Unknown options fail. No random seed is needed. Wall deadlines are
hardware-dependent; integer work limits produce reproducible schedules.

Joint actions begin with coordinated self-consume/preserve/charge actions, then
single-battery changes and a lazy Cartesian tail, deduplicated and capped at
`min(max_joint_actions, max_expansions)` before
materialization. This avoids constructing the exponential action space. Capped
enumeration favors coordinated actions and earlier batteries; diagnostics report
the full action-space size and the number omitted per prefix. Two grid-capable
batteries enumerate all nine combinations at the default cap.

Search stops cooperatively between expansions. A partially expanded slot can
still select the best visited prefix. It then completes one full-horizon schedule
using one cheap deterministic forecast/target-aware joint action per slot.
Completion uses price comparisons and optimistic target reachability to choose
each battery policy; it does not enumerate alternatives or promise optimality.
`max_expansions`/`max_seconds` bound search, not this mandatory completion/replay.
Exactly one transition per remaining tail slot and at most `beam_width`
full-horizon evaluations are
performed (one evaluation after exhaustion). Diagnostics distinguish `expansions`,
`tail_transitions`, `evaluations`, beam pruning, searched slots and termination.
There are no uncounted rollout evaluations. Optional time limits exclude the
mandatory tail and final evaluation; the simulator independently limits whole calls.

The economic preprocessing costs O(horizon * guidance_slots log guidance_slots),
with target preprocessing O(horizon * batteries * curve_length). Search candidate
storage is bounded by width times the capped joint count; retained linked prefixes
cost O(horizon * width). A 672-slot horizon and many batteries remain bounded by
these explicit limits, with work linear in battery count per transition.

The complete supplied horizon is always scored and returned. The separate
`guidance_slots` option controls guidance; request `lookahead_slots`,
`rolling_window_slots`, `solver_time_limit_seconds`, and
`infer_battery_preserve_policy` are explicitly ignored. Throughput, action deadband,
and mode-switch costs are honored. Comfort inputs are rejected, optional loads
are advisory and ignored, input params and state are not mutated.

## Reproducible tuning ledger


Evidence is stored outside git in `N:\Personal\WattPlan-planner-lab\beam-search`.
`sweep.json`, `default.json`, `commands.txt`, source copies, input/source hashes,
`initial-and-tuned-metrics.json`, and full simulator reports preserve reproduction
details. Runs used WSL Ubuntu, Python 3.14.3, the shared `/tmp` venv and serialized
compute lock. Timings are single-run cycle p95s on this machine, not portability
claims. Immutable synthetic/HA data was not modified.

V1 SHA-256: `6abad83ea27faaff916bb8a448e74cc0447bc798c6df3927b6cf5821af355583`.
V2 SHA-256: `b800a6bdd141d2e20fecbe07187d088a05b2f5592c546fbc6cad43b70ca0b2ec`.
V2 corrects guidance for nondischargeable equipment, limits action preparation by
the expansion budget, handles numerical target guidance tolerance, and changes
budget completion from enumerated greedy search to one transition per slot.

Full synthetic day: 96 replans, 24-hour forecasts, heterogeneous house/vehicle
batteries, deadline, signed tariffs and forecast/actual errors. Every row below
has net cost **1.9673129061 TEST**, import **18.2375048639 kWh**, export
**13.9336567640 kWh**, terminal house/vehicle **0.7/5.0 kWh**, and **zero target
misses**. Thus this day alone cannot justify a wider beam.

| Source / configuration | Width / expansions / guidance scale | CPU p95 s | Wall p95 s | Total expansions | Tail transitions | Full evaluations |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| V1 immediate | 1 / 30000 / 0 | 0.014742 | 0.013977 | 82944 | 0 | 96 |
| V1 narrow | 4 / 6000 / 1 | 0.053195 | 0.050535 | 259785 | 0 | 304 |
| V1 default | 16 / 30000 / 1 | 0.172147 | 0.163545 | 940347 | 0 | 1116 |
| V1 tight budget | 16 / 1500 / 1 | 0.042442 | 0.040276 | 144000 | 66168 | 96 |
| V2 immediate | 1 / 30000 / 0 | 0.018277 | 0.017373 | 82944 | 0 | 96 |
| V2 narrow | 4 / 6000 / 1 | 0.056466 | 0.053559 | 259785 | 0 | 304 |
| V2 default | 16 / 30000 / 1 | 0.170839 | 0.161647 | 940770 | 0 | 1116 |
| V2 tight budget | 16 / 1500 / 1 | 0.029254 | 0.027779 | 144000 | 7357 | 96 |

Economic guidance uses 24 slots and the joint-action cap is 81 throughout. The
budget completion change removes 58811 unnecessary tail transitions on this
sample and reduces tight-budget wall p95 about 31%; ordinary width-16 timings
remain similar. This is evidence for the simpler completion, not a statistical
speed guarantee. HiGHS reference cost is 2.1164179193 and self-consumption cost
4.8912202168, with the same total terminal 5.7 kWh and respectively zero/one
covered target misses. These are realized simulation comparisons; the planners
have different internal objective/control semantics.

To avoid choosing defaults from that day alone, `diverse_sweep.py` generates
24 independent 12-slot forecast requests using fixed seed 35806. It varies target
modes, signed tariffs, efficiencies, PV-only ingress, nondischargeable equipment,
initial reserve shortfalls and forecast PV/load. These are direct forecast
objectives, not recorded closed-loop savings. The exact requests and hashes are
saved in `diverse-requests.json` and `diverse-sweep.json`.

The chosen default remains width **16**, expansion cap **30000**, guidance **24**
slots at scale **1**, joint cap **81**, with no wall deadline. Across the diverse
set, width 16 reduces total miss from **7.5524563257** (width 4) to
**7.0537503773 kWh**, compared with **8.7880062758** at width 1 without economic
guidance. Width 4 has lower aggregate tariff cost (-3.0119360969 versus
-2.4270014662), so the default prioritizes targets in accordance with the shared
objective. Final total stored energy differs across these requests and this is
not a like-for-like economic ranking. A 1500-expansion cap matches width 16 in
this small set, but cannot explore a full 96/672-slot beam; 30000 preserves room
for longer forecasts. No optimality or real-world savings claim follows.

HA smoke: seven genuine archived forecasts on 2026-10-08 Copenhagen
10:15–12:00, with explicitly reconstructed step-held cumulative-meter actuals
and archived effective first-slot tariffs. Provenance is in the lab's
`ha-provenance.json`. V2 default net cost **1.88833737**, import **0.92 kWh**,
export **0**, final battery **2.1632742411 kWh**, no covered targets, CPU/wall p95
**0.102236/0.097103 s**, **43029** expansions and **112** full evaluations. The
HiGHS/self-consumption plumbing references cost zero and finish at
**1.1410520189 kWh**. Beam bought additional energy using its supplied longer
forecast; this short window establishes plumbing and physical validity, and
provides no evidence of an economic improvement.

Focused tests cover complete/multiple/empty equipment, ingress, efficiencies,
curves, PV, negative prices, every target mode including infeasible deadlines,
tolerance, reserve observations, exact preference scoring and projected physics,
determinism, cooperative time expiry and option rejection. A 672-slot/twelve-
battery request with one expansion verifies one tail transition per remaining
slot and one complete evaluation. A twenty-battery request with a billion-action
requested cap verifies effective materialization is still one action. Independent
exhaustive four-slot checks matched all six small oracle cases at the default;
this is an objective sanity check, not a general optimality result.


| Diverse forecast configuration | Net cost | Import kWh | Export kWh | Total final SoC kWh | Miss kWh | CPU p95 s | Wall p95 s | Expansions |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Immediate width 1 | -1.167910 | 54.351675 | 5.479251 | 111.109464 | 8.788006 | 0.003153 | 0.002999 | 2232 |
| Guided width 4 | -3.011936 | 55.176086 | 5.829786 | 111.576479 | 7.552456 | 0.006947 | 0.006606 | 7987 |
| Guided width 16 | -2.427001 | 53.879263 | 5.730882 | 110.411397 | 7.053750 | 0.028237 | 0.026841 | 26940 |
| Guided width 16 / cap 1500 | -2.427001 | 53.879263 | 5.730882 | 110.411397 | 7.053750 | 0.025220 | 0.023975 | 26877 |

A separately generated `--stress` full day starts the house below reserve and
adds an unreachable exact deadline. Default V2 produces net **2.4194257789**,
import/export **20.5213479521/13.5567633823 kWh**, terminal house/vehicle
**0.7/5.0 kWh**, and one miss of **4.152 kWh** at the impossible house deadline.
The vehicle target is met. CPU/wall p95 is **0.165965/0.157463 s**, with **941688**
expansions, zero tail transitions and **1116** full evaluations. `stress-input`
and `stress-run` preserve explicitly marked generated data and reports; original
synthetic and HA inputs are unchanged.

Final elevated full-suite verification: **917 passed in 59.30 s**, coverage
**88.51%** overall and **92%** for the beam module. Ruff `F` checks and
`git diff --check` passed. Full test commands and environment are recorded in
`commands.txt`; the coverage database is `/tmp/wattplan-beam-search.coverage`.
