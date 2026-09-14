# Optimizer Benchmark Findings

## Session 2 No-Battery Bypass

Revision `f8638c035335671bb6268a6ba3c437d2a43c8e0e` bypasses HiGHS when
`battery_entities` is empty. It retains the deterministic comfort scheduler,
per-slot physical application, scoring, optional-load replay, state encoding,
and prefix cadence. It does not integrate or change comfort placement.

The comparison used the Session 1 Windows environment and exact 96-slot,
48-slot-lookahead, three-repeat serialized commands. Each after case passed full
and trajectory quality checks and reported zero primary, probe, and total native
solver calls.

| Case | Before full median | After full median | Reduction | Before repair median | After repair median |
| --- | ---: | ---: | ---: | ---: | ---: |
| No assets, zero PV | 0.0984s | 0.0015s | 98.4% | 0.0107s | 0.0023s |
| Comfort, no battery/PV | 0.0988s | 0.0027s | 97.3% | 0.0136s | 0.0042s |
| Comfort and PV, no battery | 0.1463s | 0.0033s | 97.8% | 0.0170s | 0.0046s |
| Flexible comfort only | 0.1052s | 0.0031s | 97.0% | 0.0143s | 0.0065s |
| Tight comfort substitute | 0.1019s | 0.0027s | 97.3% | 0.0142s | 0.0049s |

After full-plan samples:

| Case | Samples (seconds) |
| --- | --- |
| No assets, zero PV | 0.0020, 0.0015, 0.0015 |
| Comfort, no battery/PV | 0.0030, 0.0027, 0.0027 |
| Comfort and PV, no battery | 0.0040, 0.0033, 0.0032 |
| Flexible comfort only | 0.0051, 0.0031, 0.0029 |
| Tight comfort substitute | 0.0034, 0.0027, 0.0027 |

Representative battery cases remained on the solver path with 96 primary calls
per full run and eight per repair tick. Their before/after medians were 1.9992s
to 1.9095s for a bidirectional battery with zero PV, 0.2525s to 0.2942s for a
charge-only battery, and 4.2334s to 4.3660s for mixed batteries with PV. All
quality checks passed. These small mixed movements, including one 5.6147s mixed
case outlier, are ordinary solver/run noise; the new condition is unreachable
for nonempty battery lists.

Session 3 subsequently evaluated exact-zero-PV specialization for battery plans
and rejected it after the signed-target control fixture showed a repeatable
production-policy regression.

## Session 3 Zero-PV Stop Decision

Session 3 tested a per-solve exact-zero-PV specialization that omitted export,
PV-charge, site-direction, and PV-surplus variables and their associated rows.
The experiment preserved battery charge/discharge/SOC, targets, deadbands,
signed import tariffs, preserve counterfactuals, and the existing nonzero-PV
model. MIP starts were made topology-aware for rolling zero-to-nonzero and
nonzero-to-zero transitions.

The production change was rejected and removed. Model-size reduction did not
translate into a repeatable end-to-end improvement across the required cases,
and the reduced topology changed arbitrary choices among equal-cost receding
horizon schedules. All compared plans remained feasible and passed target and
battery-bound checks, but tariff-only full-plan projections could differ when
the complete local objectives were tied.

Measurements used the same September 14, 2026 WSL2 environment documented
below, with 96 slots, a 48-slot lookahead, and alternating specialized/general
execution order. The specialized code and comparison switch were exploratory
and are not retained.

| Workload | Variant | Median | Samples (seconds) | Projected cost | Max model (vars / integers / rows / nonzeros) |
| --- | --- | ---: | --- | ---: | --- |
| Existing zero-PV battery | Specialized warm | 0.9565s | 0.9847, 0.9565, 0.9705, 0.9239, 0.9499, 0.9926, 0.9542 | 9.1886 | 387 / 144 / 529 / 1,153 |
| Existing zero-PV battery | General warm | 1.0416s | 1.0655, 0.9959, 1.0832, 1.0008, 1.0416, 0.9896, 1.0525 | 8.9283 | 579 / 240 / 721 / 1,825 |
| Signed tariffs, target, deadband | Specialized warm | 2.2063s | 2.2048, 2.1935, 2.2085, 2.1891, 2.2063, 2.2639, 2.2076 | -0.9388 | 387 / 144 / 530 / 1,155 |
| Signed tariffs, target, deadband | General warm | 2.0564s | 2.0503, 2.0146, 2.1271, 2.0564, 2.1540, 2.0404, 2.0949 | -1.1404 | 579 / 240 / 722 / 1,827 |
| Signed tariffs, target, deadband | Specialized cold | 2.2397s | 2.2516, 2.2397, 2.2830, 2.1862, 2.2378 | -1.3684 | 387 / 144 / 530 / 1,155 |
| Signed tariffs, target, deadband | General cold | 2.3631s | 2.3163, 2.3631, 2.6024, 2.2886, 2.4902 | -1.3350 | 579 / 240 / 722 / 1,827 |

The signed-target specialization was slower in all seven production-policy
pairs. Disabling MIP starts favored the reduced model in all five cold pairs,
but its 2.2397s median was still slower than the retained general model's
2.0564s warm median. This confirms that zero-PV topology and MIP-start policy
cannot be evaluated independently.

The useful retained artifact is the deterministic
`battery-zero-pv-signed-target` benchmark fixture. It combines exact-zero PV,
positive and negative import/export tariffs, a command deadband, and an SOC
target so future planner work is not accepted on the easy plateau case alone.

Reproduce the retained control workload with:

```bash
python scripts/benchmark_optimizer.py --scenario battery-zero-pv-signed-target --slots 96 --lookahead 48 --repeats 5
```

Recommended next step: profile or specialize charge-only battery structure,
which remains mathematically simpler and avoids the zero-PV topology's adverse
interaction with warm starts. Do not retry zero-PV pruning without a stable
secondary objective or another way to preserve receding-horizon behavior and a
paired end-to-end win on the signed-target fixture.

## Session 1 Planner Baseline

These clean-revision measurements were captured on September 14, 2026. They
establish the cases and instrumentation for the planner performance effort; this
session deliberately did not edit production optimizer, model, cadence, or the
separately owned comfort-placement module.

Environment and settings:

- Revision: `a438e50138c7f714c401a9f04fe8fe17c1e32949`, clean worktree
- Platform: Windows 11 `10.0.26200`, AMD64, 16 logical CPUs
- Python: 3.14.4
- highspy: 1.15.1
- NumPy: 2.3.2
- Shape: 96 slots, 48-slot lookahead
- Sampling: three standalone full plans plus three serialized five-tick
  trajectories per case
- Execution: one benchmark process at a time

Command template:

```bash
python scripts/benchmark_optimizer.py --scenario <case> --slots 96 --lookahead 48 --repeats 3 --serial
```

All full and trajectory quality checks passed. Full-plan solver counts below are
per run: every case made 96 primary native calls and zero probes. Each repair
tick made eight primary calls. The report itself aggregates counts across
repeats, while retaining per-run timing records.

| Case | Full median | Full samples (seconds) | Wrapper median | Native median | Build/result median | Outside steps median | Trajectory full / repair |
| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: |
| No assets, zero PV | 0.0984s | 0.1165, 0.0874, 0.0984 | 0.0747s | 0.0316s | 0.0194s | 0.0038s | 0.0968s / 0.0107s |
| Comfort, no battery/PV | 0.0988s | 0.1116, 0.0894, 0.0988 | 0.0737s | 0.0311s | 0.0175s | 0.0058s | 0.0943s / 0.0136s |
| Comfort and PV, no battery | 0.1463s | 0.1463, 0.1526, 0.1119 | 0.1137s | 0.0435s | 0.0218s | 0.0077s | 0.1094s / 0.0170s |
| Bidirectional battery, zero PV | 1.9992s | 1.9992, 1.9769, 2.1332 | 1.8897s | 1.7529s | 0.0810s | 0.0187s | 1.9618s / 0.2106s |
| Charge-only battery | 0.2525s | 0.2525, 0.2214, 0.2549 | 0.1957s | 0.0959s | 0.0417s | 0.0121s | 0.2279s / 0.0314s |
| Mixed batteries and PV | 4.2334s | 4.2334, 4.1394, 4.2866 | 4.0606s | 3.8919s | 0.1291s | 0.0219s | 4.1465s / 0.2277s |
| Flexible comfort only | 0.1052s | 0.1292, 0.0973, 0.1052 | 0.0803s | 0.0367s | 0.0181s | 0.0063s | 0.1037s / 0.0143s |
| Tight comfort substitute | 0.1019s | 0.1250, 0.1019, 0.1003 | 0.0771s | 0.0361s | 0.0183s | 0.0060s | 0.1039s / 0.0142s |

Maximum full-plan model dimensions:

| Case group | Variables | Integer variables | Rows | Nonzeros |
| --- | ---: | ---: | ---: | ---: |
| No battery, zero PV | 192 | 96 | 336 | 432 |
| No battery, comfort plus PV | 192 | 96 | 336 | 528 |
| Bidirectional battery, zero PV | 579 | 240 | 721 | 1,825 |
| Charge-only battery | 483 | 144 | 529 | 1,393 |
| Mixed batteries and PV | 966 | 384 | 1,106 | 3,218 |

The recovered `low-pv` scenario, extended to the same 96 x 48 shape, had a
2.3409s full median from 2.3409s, 2.2391s, and 2.6979s samples. The repository
contains no named historical slow-comfort fixture. `comfort-tight` is therefore
a clearly labeled synthetic substitute, not reconstructed production data.

The dedicated 24 x 8 `preserve-probe` check had a 0.0656s full median and
recorded 72 primary plus six probe calls across three runs. This confirms that
the new role instrumentation distinguishes probes without inferring them from a
successful-solve counter. No current production path in these fixtures made
zero native calls, but the report schema and tests support that future fast
path.

### Bottlenecks and next path

The highest-confidence avoidable work is the empty-battery path. A 96-slot plan
with no controllable assets still builds and solves 96 integer grid-direction
models. Comfort-only cases cost approximately the same, showing that recurring
native solves, not deterministic comfort scheduling, dominate this shape.

Native HiGHS time was about 88% of the bidirectional zero-PV full median and 92%
of the mixed-battery/PV median. By contrast, the charge-only case was much
cheaper despite the same 96-call cadence. This makes topology-specific model
reduction more promising than general Python micro-optimization.

Recommended order:

1. Bypass HiGHS entirely when there are no batteries or other solver-controlled
   assets; calculate grid flow, cost, comfort schedule, and response directly.
2. Specialize exact-zero-PV requests so PV variables, modes, and constraints are
   not constructed.
3. Reduce charge-only battery models by omitting impossible discharge variables
   and bidirectional mode machinery.
4. Keep bounded cost-aware comfort placement outside recurring battery MILPs:
   place comfort once for a full planning decision, fold fixed demand into usage,
   and reuse that placement through the prefix battery solves.

The first item is the safest next fast path because it can target zero native
calls with simple equivalence tests and no battery policy behavior to preserve.

These measurements were captured on September 14, 2026. They are local WSL2
x86-64 results, not release guarantees.

## Environment

- Platform: Linux 6.6.114.1 under WSL2 on x86-64
- Python: 3.14.3
- highspy: 1.15.1
- NumPy: 2.3.2
- Final benchmark revision: `516b44adeb934a832b412e794dd218651aa9a0a8`
- Pre-gate matrix revision: `b5d2f3f`
- Alternating benchmark harness: `bf11cea`
- MIP-start implementation: `d997f69`
- Sparse-row implementation: `821b727`
- Conditional hint extraction: `5140714`
- Effective-lookahead gate: `516b44a`

Linux Git cannot resolve this Windows-created worktree's `.git` pointer, so the
benchmark JSON reports null `git_revision` and `git_dirty` fields in this setup.
The revision above was verified with Windows Git immediately beside the run.

## Production-Shape Comparison

Command:

```bash
python scripts/benchmark_optimizer.py --scenario low-pv --slots 144 --lookahead 48 --repeats 3 --serial --compare-mip-starts
```

The `low-pv` input is derived from a captured Home Assistant planner export.
Warm and cold projected costs matched within floating-point tolerance.

Standalone full-plan samples on the final revision:

| Variant | Median | Samples (seconds) |
| --- | ---: | --- |
| Warm | 2.1676 | 2.2146, 2.1676, 2.1038 |
| Cold | 2.7543 | 2.8526, 2.7543, 2.6731 |

The warm median was about 21% lower. All 426 attempted starts across the three
warm runs returned `HighsStatus.kOk`. The benchmark names this
`successful_start_submissions`: `kOk` confirms that HiGHS accepted the API call,
not that a complete incumbent was accepted or that the start caused the final
solution.

Three paired serial trajectories produced two full and three repair calls each.
Full-call samples and repair-call samples are reported separately because the
repair path intentionally does not submit starts.

| Variant | Full median | Full samples (seconds) | Repair median | Repair samples (seconds) |
| --- | ---: | --- | ---: | --- |
| Warm | 1.9696 | 2.0451, 1.9984, 1.9745, 1.9177, 1.9647, 1.9302 | 0.1747 | 0.1944, 0.1747, 0.1599, 0.1953, 0.1597, 0.1748, 0.1741, 0.1843, 0.1597 |
| Cold | 2.4600 | 2.5628, 2.4609, 2.4566, 2.4501, 2.5012, 2.4591 | 0.1791 | 0.1906, 0.1791, 0.1530, 0.2063, 0.1572, 0.1509, 0.1853, 0.1819, 0.1483 |

Warm-minus-cold total trajectory differences were `-0.9740s`, `-0.9991s`, and
`-1.0629s`. Every trajectory reported exactly two `full` and three `repair`
calls; there were no fallback full refreshes. Warm and cold projected costs
matched at every tick within floating-point tolerance, and every plan passed the
benchmark's finite-value, schedule-bound, balance, and target checks. The repair
medians should be treated as run-order noise: both
variants execute the same cold eight-solve prefix path.

## MIP-Start Eligibility Gate

Short workloads consistently spent more time completing partial starts than
they saved in the primary solve. Raw integer-variable count did not provide a
safe threshold: the three-battery stress case improved at 220 integers but
regressed at 264. Configured/effective lookahead showed the clearest conservative
boundary across the captured and heterogeneous layouts.

The table below used the alternating warm-first/cold-first harness at the
pre-gate revision `b5d2f3f`. The 40-slot and 48-slot rows use the same solver and
harness behavior as the final revision because those cases remain eligible. A
negative warm-minus-cold result favors warm starts.

| Scenario | Slots x lookahead | Warm median | Cold median | Paired result |
| --- | ---: | ---: | ---: | --- |
| Captured low-PV | 24 x 8 | 0.2762s | 0.1870s | Warm slower in 7/7 |
| Captured low-PV | 48 x 24 | 0.5744s | 0.5813s | Mixed / neutral |
| Captured low-PV | 144 x 32 | 2.2939s | 2.3843s | Mixed |
| Captured low-PV | 144 x 36 | 1.9921s | 2.1615s | Warm faster in 5/5 |
| Stress | 48 x 32 | 2.4354s | 2.4914s | Warm faster in 3/5 |
| Stress | 48 x 36 | 2.2145s | 2.0548s | Warm slower in 5/5 |
| Captured low-PV | 48 x 40 | 0.5903s | 0.7737s | Warm faster in 5/5 |
| Captured low-PV | 144 x 40 | 2.2422s | 2.5671s | Warm faster in 5/5 |
| Stress | 48 x 40 | 2.6440s | 2.8224s | Warm faster in 4/5 |
| Stress | 48 x 48 | 2.7477s | 2.7826s | Mixed, warm median lower |

Production now submits starts only when the effective initial solve horizon is
at least 40 slots. This disables the repeatable short-horizon regressions while
retaining the measured benefit at the production 144 x 48 shape and larger hard
cases. It is a workload-derived guard, not a claim that every 40-plus-slot solve
will be faster. Deadband, full-plan, adjacent-primary-solve, and no-probe rules
still apply.

Post-gate checks on `516b44a` confirmed the behavior:

- Low-PV 24 x 8, seven pairs: both labels submitted zero starts, returned the
  identical `1.3803171958` projected cost, and passed physical checks. Pair
  timing was mixed because both labels executed the same cold path.
- Low-PV 144 x 48, three pairs: warm submitted 426 starts, costs matched within
  floating-point tolerance, all plans passed physical checks, and warm was
  faster in all three pairs.

Matrix command template:

```bash
python scripts/benchmark_optimizer.py --scenario <low-pv-or-stress> --slots <slots> --lookahead <slots> --repeats 5 --compare-mip-starts
```

Reproduce the below-gate warm/cold rows from the recorded pre-gate revision;
on the final revision the warm label intentionally follows production policy
and therefore remains cold below 40 effective slots.

## Hardest Relevant Scenarios

No named historical worst-case fixture exists in the repository. Of the two
captured fixtures, `low-pv` is the hardest relevant MIP-start case because it
uses a deadband and exercises integer mode decisions; `live-export` has no
deadband and therefore cannot consume starts. The production-shape comparison
above is the retained normal cadence case.

The deterministic `stress` scenario is the harder synthetic complement: three
heterogeneous batteries, nonlinear curves, deadbands, signed tariffs, and fixed
targets. It was measured at 144 x 96 with two alternating standalone pairs and
two complete alternating five-tick trajectories:

```bash
python scripts/benchmark_optimizer.py --scenario stress --slots 144 --lookahead 96 --repeats 2 --serial --compare-mip-starts
```

| Variant | Standalone median | Samples (seconds) | Serial full median | Serial full samples (seconds) | Repair median |
| --- | ---: | --- | ---: | --- | ---: |
| Warm | 14.6192 | 14.6457, 14.5927 | 16.2142 | 14.4442, 17.1547, 15.2737, 18.5685 | 3.4143 |
| Cold | 26.7440 | 26.8436, 26.6444 | 28.7474 | 26.5632, 28.3985, 32.0786, 29.0964 | 3.4451 |

Warm-minus-cold standalone differences were `-12.1980s` and `-12.0518s`.
Trajectory differences were `-23.0389s` and `-28.0495s`. Both variants recorded
exactly four `full` and six `repair` calls across the two trajectories, with no
fallback full refreshes, and all ten plans per variant passed physical-quality
checks.

The stress tariff-only projection differed by about `0.0115` cost units on full
ticks (`-58.2477` warm versus `-58.2362` cold). Both solves were optimal for the
complete objective, which also contains throughput and mode terms, so different
optimal schedules can have slightly different tariff-only projections. Repair
ticks matched, and neither start constrained the model or weakened physical
validity.

## Conditional Hint Extraction

Before `5140714`, every MPC solve copied its integer solution into a hint
dictionary even when no later solve consumed it. Baseline runs used production
revision `5645cbf`; after runs used `5140714`. Both used the same harness logic
later committed as `a954a50`.

| Workload | Before median | After median | Before samples (seconds) | After samples (seconds) |
| --- | ---: | ---: | --- | --- |
| Synthetic preserve probe, 24x8, 15 runs | 0.0976 | 0.0807 | 0.1198, 0.0807, 0.0923, 0.0937, 0.1070, 0.1053, 0.1143, 0.1024, 0.0976, 0.1022, 0.0871, 0.0964, 0.1015, 0.0904, 0.0835 | 0.0807, 0.0799, 0.0763, 0.0840, 0.0996, 0.0931, 0.0985, 0.0795, 0.0797, 0.0839, 0.0780, 0.0856, 0.0799, 0.0672, 0.0891 |
| Captured live-export, 144x48, 5 runs | 0.5821 | 0.4934 | 0.5821, 0.6044, 0.5733, 0.5397, 0.5972 | 0.5097, 0.5083, 0.4934, 0.4369, 0.4897 |

The probe workload recorded 135 real counterfactual probe calls across each
15-run batch. The live-export workload recorded 95 probes across each five-run
batch and has no deadband, so none of its 815 solver calls could consume a hint.
Prefix-only timing was effectively unchanged: its median moved from `0.1728s`
to `0.1714s`, as expected for eight small solves.

## Rejected Leads

- Reusing a published plan across separate calculations was slower in two
  exploratory runs: about `25.1ms` to `27.9ms` for a shifted first solve, and
  `23.5ms` to `46.3ms` when expanding a 22-slot hint into a 48-slot solve.
- Passing starts into preserve probes was rejected. A 15-run live-export median
  moved from about `0.707s` to `0.825s`; 285 submissions returned `kOk`, while
  total node count did not improve.
- Warm starts are workload-dependent. The eligibility matrix above led to the
  40-slot effective-lookahead gate in addition to the adjacent, full,
  deadband-enabled primary-solve restrictions.

## Limitations

- No ARM runner was available. These x86-64 WSL2 timings must not be
  extrapolated to Home Assistant ARM hardware.
- The `stress` scenario is synthetic and does not guarantee preserve probes.
  Use `preserve-probe` and verify `probe_calls > 0` for that path.
- Wall-clock results depend on CPU, thermal state, HiGHS version, and concurrent
  load. Keep environment and workload fixed and retain complete sample arrays.
