# Cross-entropy battery planner

`custom_components.wattplan.optimizer.planners.cross_entropy:factory` is a
stateless experimental categorical cross-entropy method (CEM). It samples
complete battery-major schedules, ranks them by the shared lexicographic target
miss / tariff-and-preferences objective, retains elite schedules, and fits a
smoothed categorical distribution to their actions. Every accepted result is a
complete shared-physics replay; no continuous solver, other planner, export
control, or terminal energy credit is used.

Each cycle starts afresh with a local fixed-seed random generator. Forecast tariff
thresholds seed economically coherent policies; target seeds preserve and charge
before an inclusive deadline or discharge toward an upper bound. These are
constructions rather than separate search algorithms, and their evaluations count
against the same budget. Seeds can miss feasible targets when curves or PV make
the simple charging estimate inaccurate. CEM optimizes the exact shared target
checks; it does not guarantee target feasibility or a global optimum.

Categorical probabilities are learned per battery and slot. One uniform variate
is shared across adjacent slots in randomly offset blocks, encouraging coherent
runs while allowing different slot distributions. The smoothing update combines
old probabilities and elite frequencies, then applies an affine probability floor.
The floor also applies to initialization, including `guidance=1`. Forbidden grid
charging has probability zero throughout initialization, fitting and sampling.
The retained best schedule can never worsen relative to the physical
self-consumption baseline under the shared objective.

All supplied forecast slots are returned and scored, including targets beyond
legacy `lookahead_slots`. The legacy rolling window, lookahead, solver time limit,
and inferred preserve preferences are unused and disclosed through capabilities.
Comfort inputs are unsupported and optional recommendations are advisory.

| Keyword-only option | Default | Meaning |
| --- | ---: | --- |
| `max_evaluations` | 64 | Positive integer; includes baseline and unique seeds/samples |
| `population_size` | 12 | Integer 2–4096; new unique samples per generation |
| `elite_fraction` | 0.25 | Fraction in (0, 1]; rounded up to at least one elite |
| `smoothing` | 0.7 | Fraction in (0, 1]; weight of elite action frequencies |
| `probability_floor` | 0.02 | Per-allowed-action floor in (0, 1/3] |
| `block_slots` | 4 | Integer 1–672; shared-uniform temporal block length |
| `guidance` | 0.85 | Fraction in (0, 1]; initial preference for the best seed |
| `seed` | 0 | Integer; fresh deterministic generator on every request |
| `max_seconds` | null | Optional positive finite cooperative search deadline |

The evaluation cap is additionally bounded by
`max(1, 196608 // max(1, batteries * forecast_slots))`. Candidate attempts,
including duplicates, are capped at eight times that effective evaluation cap
(at least eight attempts). Collapsed distributions therefore terminate even when
no novel schedule can be sampled. Forecast summaries are prepared once per seed,
with linear horizon work rather than repeated horizon-squared sample scans.
The full baseline always runs once, even with a tiny elapsed time budget; the
accepted evaluation is reused when building projections. Diagnostics report the
actual evaluation count, seed candidates, generations, candidate attempts, full
forecast horizon, effective cap, target miss and termination reason. Wall deadlines
are cooperative and hardware dependent; fixed counts and seeds are reproducible.

Conformance and algorithm tests live in
`tests/optimizer/test_cross_entropy_planner.py`, covering shared physics,
projection agreement, efficiencies/taper curves, multiple batteries, PV-only
masks, negative tariffs, targets and inclusive deadlines, request isolation,
determinism, floors, rounding, collapsed distributions and tiny/large budgets.

## Tuning evidence and selected configuration

The [selected configuration](cross-entropy-benchmark.json) fixes seed 0 and a
48-evaluation budget. One configuration is retained: increasing the budget did
not establish a useful speed/economics tradeoff, and the compact distribution
showed large seed sensitivity. Seed 11 was prespecified as a robustness check;
its best individual outcome is not used to choose the published seed.

These are **tuning results**, before independent integrated-source verification.
The modest predefined grid covered evaluations 48/96 with population 12,
block length 4 and guidance 0.85, followed by evaluations 64 with population 8,
block length 8 and guidance 0.95. Each shape used both seeds 0 and 11. All 18
whole-dataset replays completed inside the unchanged 120-second run deadline.
Wall times below include worker startup and simulation overhead.

| Evaluations / population / block / guidance | Seed | Archived real cost (DKK) / wall (s) | Causal cost (DKK) / wall (s) | Synthetic wall (s) |
| --- | ---: | --- | --- | ---: |
| 48 / 12 / 4 / 0.85 | 0 | 71.746226 / 12.737 | 71.751245 / 12.164 | 6.942 |
| 48 / 12 / 4 / 0.85 | 11 | 70.897471 / 12.732 | 74.221517 / 12.205 | 7.434 |
| 96 / 12 / 4 / 0.85 | 0 | 71.839900 / 22.226 | 71.751245 / 21.955 | 10.476 |
| 96 / 12 / 4 / 0.85 | 11 | 69.834621 / 23.074 | 72.758212 / 21.413 | 10.861 |
| 64 / 8 / 8 / 0.95 | 0 | 72.984602 / 16.247 | 72.314373 / 17.118 | 8.960 |
| 64 / 8 / 8 / 0.95 | 11 | 66.170004 / 16.360 | 72.064401 / 15.094 | 9.155 |

Archived real and causal fixtures each cover 48 hours / 192 planning cycles,
one battery, and 144 forecast slots per cycle. Every trial started at 1.05 kWh
and ended at 1.0 kWh, with no target checks. The synthetic fixture covers
24 hours / 96 cycles and two batteries with a deadline target. Every trial cost
1.967313 **TEST** currency units, met the target, started at house 2.8 / vehicle
1.2 kWh, and ended at house 0.7 / vehicle 5.0 kWh. The vehicle deadline level was
4.879937 kWh in every trial. Costs across these different fixtures are not directly
comparable.

For the selected seed-0 configuration, cycle-start action counts were:

| Fixture / battery | Self-consume | Preserve | Grid-charge |
| --- | ---: | ---: | ---: |
| Archived real / Battery | 161 | 25 | 6 |
| Causal / Battery | 169 | 21 | 2 |
| Synthetic / house | 86 | 0 | 10 |
| Synthetic / vehicle | 86 | 0 | 10 |

The selected configuration modestly improved the self-consumption reference
72.314373 DKK on these seed-0 replays, while previously measured beam search
was substantially better and faster (beam-2: real 65.608359 DKK / 5.333 s;
causal 69.315129 DKK / 4.900 s, also ending at 1.0 kWh). CEM's complete-schedule
search has high variance on long forecasts with small populations. The
forecast-objective baseline guarantee does not guarantee better realized
closed-loop cost, as the seed-11 causal results demonstrate. Two days and one
synthetic target fixture establish implementation behavior, not general economic
superiority.

Raw private inputs and reports remain outside git. The local aggregate ledger
`WattPlan-planner-lab/cem2-tuning-ledger.json` records every trial, failure status,
initial/final levels, target checks and cycle-start actions. Reports under
`cem2-grid-*` and `cem2-tight-*` contain source/input hashes and full simulator
provenance. The tuning source received readability-only formatting afterward;
independent validation must use the final integrated source hash.
