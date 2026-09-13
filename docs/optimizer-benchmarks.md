# Optimizer Benchmark Findings

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
