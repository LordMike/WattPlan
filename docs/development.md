# Development

## Repository workflow

Work from `WattPlan` as the source of truth.
The repository now uses the HACS-standard layout, so the integration lives at `custom_components/wattplan/` at the repo root and tests import `custom_components` directly from the project root.

## Setup

Install `uv`, then create a local virtualenv with the test
dependencies.

```bash
uv venv --python python3.14 .venv
. .venv/bin/activate
uv pip install --python .venv/bin/python '.[test]'
```

Use Python 3.14.2 or newer. The pinned Home Assistant test stack currently
requires that patch level.

On systems where the repo lives on a mounted or network-backed filesystem, a
repo-local `.venv` may be less reliable with `uv` than a virtualenv created on
the native local filesystem. If that affects your setup, create the venv in
your preferred local location and point the test wrapper at it.

Run tests directly from this repo:

```bash
python -m pytest
```

The default collection covers the full `tests/` tree, including packaging,
integration, and optimizer tests.

## Testing

Run the full suite from the repo:

```bash
./scripts/run_tests.sh
```

If your virtualenv is not at the wrapper's default location, point the wrapper
at it:

```bash
WATTPLAN_TEST_VENV=/path/to/venv ./scripts/run_tests.sh
```

Run only optimizer tests:

```bash
./scripts/run_tests.sh tests/optimizer
```

Run only integration tests:

```bash
./scripts/run_tests.sh tests/integration
```

## Optimizer benchmarks

Session 1 fixtures cover the asset shapes that should remain cheap as planner
behavior evolves:

- `no-assets-no-pv`
- `no-battery-comfort-no-pv`
- `no-battery-comfort-pv`
- `battery-zero-pv`
- `charge-only-battery`
- `mixed-batteries-pv`
- `comfort-flexible`
- `comfort-tight`

The fixtures live in `tests/optimizer/benchmark_cases.py`. They are synthetic,
and `comfort-tight` is explicitly labeled as a synthetic historical substitute
because repository history contains no recoverable named slow-comfort input.
The existing `low-pv` and `live-export` scenarios remain the recovered captured
Home Assistant inputs.

Measure one full-plan distribution and repeated five-tick prefix trajectories:

```bash
python scripts/benchmark_optimizer.py --scenario mixed-batteries-pv --slots 96 --lookahead 48 --repeats 3 --serial
```

Run the Session 1 matrix sequentially. Do not parallelize these processes; CPU
and thermal contention invalidates comparisons:

```bash
for scenario in no-assets-no-pv no-battery-comfort-no-pv no-battery-comfort-pv battery-zero-pv charge-only-battery mixed-batteries-pv comfort-flexible comfort-tight; do
  python scripts/benchmark_optimizer.py --scenario "$scenario" --slots 96 --lookahead 48 --repeats 3 --serial
done
```

The JSON report records revision/dirty state, platform, Python, NumPy, HiGHS,
command settings, provenance, quality checks, and these timing layers:

- End-to-end input validation through serialized planner response.
- Input validation and explicit full-plan normalization.
- Planner time.
- `_solve_mpc_step` time, native `_solve_lp` wrapper time, and HiGHS native time.
- Model construction plus solve-result extraction, measured as step time outside
  `_solve_lp`; production code is not changed solely to split those two pieces.
- Planner time outside solver steps.
- Explicit primary/probe native call counts and maximum model dimensions.

Reports safely represent zero native calls with zero counts and zero model
dimensions. Use `--include-call-details` only when per-call horizons, roles, and
sizes are needed; aggregate output is the default to keep reports manageable.
The three no-battery fixture groups should report `total_calls: 0` after the
direct-flow bypass. Treat a nonzero count there as a regression even when the
wall-clock result remains small.

Use `preserve-probe` when the measurement must exercise counterfactual solves:

```bash
python scripts/benchmark_optimizer.py --scenario preserve-probe --slots 24 --lookahead 8 --repeats 15
```

The `live-export` scenario uses another captured Home Assistant forecast. The
deterministic `stress` scenario adds three heterogeneous batteries, deadbands,
signed tariffs, nonlinear power curves, and targets; increase its workload
explicitly when solver timing is too short to distinguish:

```bash
python scripts/benchmark_optimizer.py --scenario stress --slots 288 --lookahead 96 --repeats 1
```

Use the same Python environment and machine for paired comparisons. Report the
full sample list, not only the median, and do not extrapolate x86 timings to ARM.
Pass `--disable-mip-starts` to measure the cold solver path under the identical
scenario, full/prefix cadence, and forecast workload.

Production submits MIP starts only when the effective initial lookahead is at
least 40 slots. Below that boundary, `--compare-mip-starts` intentionally
compares two cold executions so the benchmark reflects deployed behavior. The
historical forced warm/cold eligibility matrix and its exact pre-gate revision
are recorded in the findings document.

See [Optimizer benchmark findings](optimizer-benchmarks.md) for the recorded
September 2026 measurements and rejected experiments.

## Packaging

Build a local HACS artifact:

```bash
python scripts/build_hacs_zip.py
```

Build with an explicit label:

```bash
python scripts/build_hacs_zip.py --version-label local-dev
```

Artifacts are written to `dist/`.

## Practical rules

- Keep the optimizer pure; do not add `homeassistant` imports under `optimizer/`.
- If integration behavior changes, update integration tests in the same change.
- If workflows or release behavior change, update `README.md`, `docs/release.md`, and `AGENTS.md`.
