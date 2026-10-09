# Additional independent planner comparison

The frozen [nine-variant configuration](additional-benchmark.json) compares six
selected profiles from four new algorithms against self-consumption and beam
widths 2/16. Each algorithm was implemented and initially tuned by a separate
`gpt-6.1-sol/high` agent; a separate final agent ran this integrated comparison.
No profile, seed, dataset or runtime implementation was changed during the final
benchmark. Home Assistant continues to use HiGHS.

The new profiles match the per-algorithm files exactly:

| Algorithm | Selected profiles and fixed options | Configuration |
| --- | --- | --- |
| Dynamic programming | Fast: SoC cell 0.1 kWh, 8 states, 6000 expansions, valuation 24 slots. Strong: 0.25 kWh, 32 states, 20000 expansions, valuation 144 slots. Both: joint actions 81. | [DP](dynamic-programming-benchmark.json) |
| Charge window | 96 evaluations, 3 window candidates. | [Window](charge-window-comparison.json) |
| Genetic | Fast: 16 evaluations, population 6, guidance 24 slots. Standard: 48 evaluations, population 8, guidance 144 slots. Both: 8 generations, 2 elites, tournament 3, mutation 0.9, crossover 0.8, seed 0, no planner wall deadline. | [Genetic](genetic-benchmark.json) |
| Cross-entropy | 48 evaluations, population 12, elite fraction 0.25, smoothing 0.7, probability floor 0.02, blocks 4 slots, guidance 0.85, seed 0. | [CEM](cross-entropy-benchmark.json) |

Seed 0 was chosen before tuning. Seed 11 robustness results remain in the
algorithm ledgers; a lucky CEM seed was not promoted. Beam controls use guidance
24 slots, scale 1, 30000 expansions and 81 joint actions. All alternatives start
fresh each cycle and return and score the full supplied **144-slot / 36h**
forecast. Guidance windows are heuristic valuation choices, not output truncation.
Settings overrides are empty; HiGHS rolling-window/lookahead/solver preferences
are unused by these alternatives. Comfort is unsupported and optional appliance
recommendations are advisory, excluded from executed economics.

## Replay scope and timing

Both scenarios cover **2026-10-06 22:00 UTC to 2026-10-08 22:00 UTC**, end
exclusive: **October 7 00:00 to October 9 00:00 Europe/Copenhagen; 48h / 192
quarter-hour slots**. The primary preserves genuine archived forecasts. The
companion replaces only usage/PV forecasts with explicitly fabricated causal
persistence values available at each decision; it retains archived price
forecasts. Both use identical reconstructed measured actuals and equipment.

Strict validation found 192 decisions, 192 actual slots, no gaps or repairs and
one continuous physical segment. The initial capture delay is 81.812038 seconds,
so every replay executes **172718.187962 seconds / 47.977274h**. Full-range load
is 44.24 kWh and PV 15.37 kWh; executed load is 44.228182706 kWh. Actual tariffs
are archived first-slot effective tariffs, not independently verified bills.
The meter boundary has not been independently calibrated.

The battery has 10 kWh capacity, 1 kWh minimum, 5 kW nominal charge/discharge and
90% efficiency each way. Every run starts at **1.05 kWh**, carries simulated SoC
across midnight without resets, and ends at **1.0 kWh**. There are no battery
targets: zero misses reflects **zero target checks**.

Each scenario/configuration ran **once**, with its own strict **120 s wall cap
for the entire 192-cycle replay**, starting before worker startup. Input
validation and report rendering are outside the cap. CPU work was serialized
with the shared compute lock. Replay wall includes startup, IPC and simulation;
planner wall/CPU sum the timed calls only. p95 is the nearest-rank per-call wall
statistic. These WSL workstation timings are samples, not statistical guarantees
or measurements on the Home Assistant server.

Completed runs below are sorted by net cost, with replay time breaking ties.
`Earlier` rows add the comparable prior HiGHS, greedy and local-search runs;
`Latest` rows use the final benchmark above. Repeated configurations use
their latest result here; the original measurements remain in the historical
ledger. Timeouts are unscored and listed separately below.

## Genuine archived forecasts

| Profile | Net DKK | Replay wall s | Planner CPU s | Planner wall s | Call p95 s | End kWh | Self / preserve / grid cycles | Run |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| DP strong | 64.801996 | 28.808 | 28.457 | 26.344 | 0.160177 | 1.0 | 122 / 55 / 15 | Latest |
| Beam 16 | 65.149669 | 20.730 | 19.519 | 18.215 | 0.111407 | 1.0 | 93 / 88 / 11 | Latest |
| Beam 2 | 65.608359 | 4.908 | 2.837 | 2.575 | 0.017200 | 1.0 | 94 / 88 / 10 | Latest |
| DP fast | 65.877327 | 11.263 | 8.878 | 8.065 | 0.053741 | 1.0 | 125 / 56 / 11 | Latest |
| Genetic standard | 67.979279 | 13.963 | 12.277 | 11.324 | 0.091903 | 1.0 | 95 / 74 / 23 | Latest |
| HiGHS 4h / 16 slots | 68.444444 | 89.313 | 151.865 | 86.266 | 1.196113 | 1.0 | 98 / 82 / 12 | Earlier |
| Genetic fast | 68.981421 | 7.013 | 4.931 | 4.531 | 0.033509 | 1.0 | 160 / 14 / 18 | Latest |
| Local 96 | 69.677497 | 22.138 | 20.998 | 19.692 | 0.128076 | 1.0 | 150 / 30 / 12 | Earlier |
| Charge window 96 | 69.967282 | 17.728 | 16.644 | 15.431 | 0.103227 | 1.0 | 129 / 45 / 18 | Latest |
| HiGHS 2h / 8 slots | 71.695776 | 61.403 | 102.509 | 58.397 | 0.772593 | 1.0 | 41 / 149 / 2 | Earlier |
| CEM 48 | 71.746226 | 11.882 | 10.258 | 9.528 | 0.057433 | 1.0 | 161 / 25 / 6 | Latest |
| Self-consumption | 72.314373 | 2.451 | 0.218 | 0.201 | 0.001618 | 1.0 | 192 / 0 / 0 | Latest |
| Local 3 | 72.314373 | 2.861 | 0.601 | 0.560 | 0.003786 | 1.0 | 192 / 0 / 0 | Earlier |
| Greedy 8 | 72.314373 | 3.689 | 1.310 | 1.232 | 0.008456 | 1.0 | 192 / 0 / 0 | Earlier |
| Greedy 256 | 72.702428 | 42.565 | 43.000 | 40.114 | 0.266826 | 1.0 | 190 / 0 / 2 | Earlier |

## Fabricated causal usage/PV forecasts

| Profile | Net DKK | Replay wall s | Planner CPU s | Planner wall s | Call p95 s | End kWh | Self / preserve / grid cycles | Run |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| DP strong | 67.733586 | 28.039 | 27.562 | 25.468 | 0.164472 | 1.0 | 138 / 39 / 15 | Latest |
| Beam 16 | 68.701992 | 19.715 | 18.418 | 17.091 | 0.109283 | 1.0 | 125 / 57 / 10 | Latest |
| DP fast | 68.821431 | 10.364 | 8.533 | 7.834 | 0.050517 | 1.0 | 152 / 30 / 10 | Latest |
| Genetic standard | 69.111255 | 13.507 | 12.047 | 11.094 | 0.085057 | 1.0 | 74 / 114 / 4 | Latest |
| Beam 2 | 69.315129 | 4.867 | 2.737 | 2.514 | 0.016147 | 1.0 | 117 / 65 / 10 | Latest |
| HiGHS 4h / 16 slots | 69.467963 | 95.913 | 162.384 | 93.241 | 1.260167 | 1.0 | 67 / 118 / 7 | Earlier |
| Genetic fast | 69.476598 | 7.064 | 4.589 | 4.212 | 0.030246 | 1.0 | 146 / 33 / 13 | Latest |
| Charge window 96 | 70.655554 | 17.569 | 16.279 | 15.136 | 0.098335 | 1.0 | 139 / 49 / 4 | Latest |
| HiGHS 2h / 8 slots | 71.525105 | 55.463 | 94.392 | 52.680 | 0.673903 | 1.0 | 48 / 141 / 3 | Earlier |
| CEM 48 | 71.751245 | 12.388 | 10.577 | 9.849 | 0.064545 | 1.0 | 169 / 21 / 2 | Latest |
| Self-consumption | 72.314373 | 2.473 | 0.210 | 0.193 | 0.001288 | 1.0 | 192 / 0 / 0 | Latest |
| Local 3 | 72.314373 | 3.041 | 0.657 | 0.617 | 0.004357 | 1.0 | 192 / 0 / 0 | Earlier |
| Greedy 8 | 72.314373 | 3.770 | 1.488 | 1.400 | 0.010178 | 1.0 | 192 / 0 / 0 | Earlier |
| Local 96 | 73.444378 | 20.213 | 19.123 | 17.780 | 0.104623 | 1.0 | 176 / 11 / 5 | Earlier |

All **18/18 latest benchmark runs completed all 192 cycles** under the cap.
The added earlier rows also completed the full range. Every action-count
triplet sums to 192. Counts select the policy at each planning cycle's start,
using the interval whose start equals `cycle.at`; raw split-interval row counts
are not slot counts. Selected modes do not guarantee physical flow. Separately,
summing executed duration per action and dividing by 900 gives **191.909098
quarter-hour equivalents** per run, after the initial delay.

DP strong has this sample's lowest cost: 64.801996 / 67.733586 DKK,
0.347673 / 0.968406 DKK below beam 16, with more compute.
DP fast comes close to beam 16 on causal forecasts but costs more than beam 2
on genuine forecasts. Beam 2 remains the fastest tested alternative that makes
substantial savings on both scenarios. Charge window, genetic fast and CEM cost
more than both beam controls here. Genetic standard improves over beam 2 only
on causal forecasts, with more compute, and still loses to beam 16 and DP fast.
CEM approaches self-consumption despite
its compute; this seed does not establish a useful compute/cost tradeoff.

## Prior results retained

The previous two-day ledger is preserved. Fresh self-consumption and both beam
controls reproduce its realized costs and integer action counts. Historical
HiGHS rows below are prior once-run samples, not newly rerun timings. They retain
the genuine 20 s internal solver limit and full/repair cadence, while limiting
economic lookahead to 8 or 16 slots; alternatives score all 144 supplied slots.
These objective and horizon differences prevent attributing all economic
improvement solely to the algorithm.

| Historical profile | Genuine cost DKK / replay s | Causal cost DKK / replay s |
| --- | --- | --- |
| HiGHS 2h / 8 economic slots | 71.695776 / 61.403 | 71.525105 / 55.463 |
| HiGHS 4h / 16 economic slots | 68.444444 / 89.313 | 69.467963 / 95.913 |
| HiGHS 8h / 32 economic slots | 120 s timeout, unscored | 120 s timeout, unscored |
| Recorded HiGHS 12h / 48 economic slots | 120 s timeout, unscored | Not run |

Completed historical rows also end at 1 kWh. Timed-out runs retain `totals=null`;
partial diagnostics have no full-range economic score. No timed-out HiGHS run
was retried or granted extra time; 16h/24h variants were excluded.

## Synthetic cross-check and limitations

The separate 96-cycle synthetic target fixture uses TEST currency and two
heterogeneous batteries. Prior independently checked selected runs are retained
without rerunning them here:

| Selected profile | Synthetic cost TEST | End house / vehicle kWh | Target misses |
| --- | ---: | --- | ---: |
| DP fast | 1.967313 | 0.7 / 5.0 | 0 |
| DP strong | 5.585262 | 4.652458 / 5.0 | 0 |
| Charge window 96 | 2.006512 | 0.7 / 5.0 | 0 |
| Genetic fast | 2.092129 | 0.7 / 5.0 | 0 |
| Genetic standard | 3.279315 | 0.7 / 5.0 | 0 |
| CEM 48 | 1.967313 | 0.7 / 5.0 | 0 |

DP strong retains 3.952458 kWh more house energy on that fixture, so its cost is
not an equal-terminal-energy comparison. Genetic standard has worse synthetic
realized cost than fast despite improving the genuine two-day sample. More
search or longer guidance does not guarantee better realized economics. The
synthetic target result and conformance tests do not prove universal feasibility.

Independent checks recomputed all JSON/CSV interval costs, actual boundary
fractions, energy conservation, efficiency losses, SoC transitions, continuity,
per-call timing summaries and cycle-start actions. Maximum energy residual is
2.22e-16 kWh. Original input/provenance hashes,
frozen source/config hashes and before/after snapshots match; report checksums
are retained with private evidence. Integrated tests passed **1171 tests,
89.32% coverage** and HACS packaging included all nine planner modules.

These two days represent one installation and one season, with no target or
comfort execution and no terminal energy credit. They do not justify changing
production defaults or establish general economic superiority. Random-method
seed sensitivity, different equipment, broader weather/tariffs, degradation,
forecast errors and target feasibility need broader evidence.

## Reproduction

Use the prepared Linux/WSL environment, an immutable recording glob and a **new**
output directory. The quoted glob is expanded by the CLI. Run each scenario
once; the CLI applies an independent 120 s cap per variant. Recordings and raw
entity data remain private and are not committed.

```bash
flock /tmp/wattplan-planner-compute.lock \
  /tmp/wattplan-simulation-venv/bin/python scripts/simulate_planner.py \
  '/path/to/recordings/*.jsonl' \
  --config docs/planners/additional-benchmark.json --validate-only

flock /tmp/wattplan-planner-compute.lock \
  /tmp/wattplan-simulation-venv/bin/python scripts/simulate_planner.py \
  '/path/to/recordings/*.jsonl' \
  --config docs/planners/additional-benchmark.json --repeats 1 \
  --output /tmp/wattplan-additional-new
```

The benchmark source is frozen at `cd147d1d31c2708d6980629faf92e5539f09faf7`.
See [the original comparison](comparison.md), [simulator workflow](../simulator.md)
and the individual algorithm ledgers for implementation and tuning limitations.
