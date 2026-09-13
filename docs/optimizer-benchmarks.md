# Optimizer Benchmark Findings

These measurements were captured on September 14, 2026. They are local WSL2
x86-64 results, not release guarantees.

## Environment

- Platform: Linux 6.6.114.1 under WSL2 on x86-64
- Python: 3.14.3
- highspy: 1.15.1
- NumPy: 2.3.2
- Benchmark revision: `a954a509d9c46fb6c3f2a02e49e2c18ee6a6dcc8`
- MIP-start implementation: `d997f69`
- Sparse-row implementation: `821b727`
- Conditional hint extraction: `5140714`

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

Standalone full-plan samples:

| Variant | Median | Samples (seconds) |
| --- | ---: | --- |
| Warm | 2.0106 | 1.9757, 2.0106, 2.0218 |
| Cold | 2.5482 | 2.6208, 2.5482, 2.4941 |

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
| Warm | 1.9825 | 1.9902, 1.9543, 1.9385, 1.9961, 1.9856, 1.9793 | 0.1917 | 0.2234, 0.1605, 0.1594, 0.1956, 0.1945, 0.1539, 0.2044, 0.1917, 0.1683 |
| Cold | 2.4985 | 2.5173, 2.5212, 2.4798, 2.4584, 2.5605, 2.4687 | 0.1783 | 0.2096, 0.1652, 0.1529, 0.1856, 0.1963, 0.1562, 0.1902, 0.1783, 0.1728 |

Warm-minus-cold total trajectory differences were `-1.0784s`, `-0.9977s`, and
`-1.0411s`. The repair medians should be treated as run-order noise: both
variants execute the same cold eight-solve prefix path.

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
- Warm starts are workload-dependent. A small 24-slot, 8-slot-lookahead low-PV
  check was slower warm (`0.293s`) than cold (`0.180s`). Starts therefore remain
  limited to adjacent, full, deadband-enabled primary solves.

## Limitations

- No ARM runner was available. These x86-64 WSL2 timings must not be
  extrapolated to Home Assistant ARM hardware.
- The `stress` scenario is synthetic and does not guarantee preserve probes.
  Use `preserve-probe` and verify `probe_calls > 0` for that path.
- Wall-clock results depend on CPU, thermal state, HiGHS version, and concurrent
  load. Keep environment and workload fixed and retain complete sample arrays.
